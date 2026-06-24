"""SQLite 存储（第一性原理 + 低延迟）。

设计：
- 本地 SQLite，开启 WAL（写不阻塞读）+ NORMAL 同步（低延迟，断电最多丢最后事务）。
- 热路径（成交/OI）走批量缓冲 executemany，由调用方周期 flush，避免逐行 commit 拖慢。
- sqlite3 写本地是微秒级；如需完全不阻塞事件循环，调用方可用 asyncio.to_thread 包 flush。

表（覆盖两套系统）：
  meme_contracts   各 meme 的链上合约地址（Bitget 币种 → 链 → 合约）
  bitget_oi        Bitget USDT-M 永续 OI/资金费/标记价 时间序列
  hl_meme_trades   Hyperliquid meme 成交（含买卖双方地址 + taker）
  sm_events        聪明钱地址事件（开/加/减/平/反手）
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meme_contracts (
    coin     TEXT NOT NULL,
    chain    TEXT NOT NULL,
    contract TEXT NOT NULL,
    updated_ms INTEGER NOT NULL,
    PRIMARY KEY (coin, chain)
);

CREATE TABLE IF NOT EXISTS bitget_oi (
    symbol  TEXT    NOT NULL,
    coin    TEXT    NOT NULL,
    oi_size REAL    NOT NULL,
    oi_usd  REAL,
    mark_px REAL,
    funding REAL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS ix_bitget_oi_coin_ts ON bitget_oi(coin, ts);

CREATE TABLE IF NOT EXISTS okx_perp (
    inst_id  TEXT    NOT NULL,
    coin     TEXT    NOT NULL,
    oi_ccy   REAL,
    oi_usd   REAL,
    mark_px  REAL,
    funding  REAL,
    net_flow REAL,
    ts       INTEGER NOT NULL,
    PRIMARY KEY (inst_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_okx_perp_coin_ts ON okx_perp(coin, ts);

CREATE TABLE IF NOT EXISTS okx_liquidations (
    ts           INTEGER NOT NULL,
    coin         TEXT    NOT NULL,
    pos_side     TEXT    NOT NULL,   -- 'long'(多头被平=抛压级联) / 'short'(空头被平=逼空)
    side         TEXT    NOT NULL,   -- 'sell'(多头平) / 'buy'(空头平)
    notional_usd REAL,
    bk_px        REAL
);
CREATE INDEX IF NOT EXISTS ix_okx_liq_coin_ts ON okx_liquidations(coin, ts);

CREATE TABLE IF NOT EXISTS hl_meme_trades (
    coin       TEXT    NOT NULL,
    px         REAL    NOT NULL,
    sz         REAL    NOT NULL,
    notional   REAL    NOT NULL,
    taker_side TEXT    NOT NULL,   -- 'B'=taker买 / 'A'=taker卖
    buyer      TEXT    NOT NULL,   -- users[0]
    seller     TEXT    NOT NULL,   -- users[1]
    taker      TEXT    NOT NULL,   -- 主动方地址
    hash       TEXT,
    tid        INTEGER,
    time_ms    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hlmt_coin_time ON hl_meme_trades(coin, time_ms);
CREATE INDEX IF NOT EXISTS ix_hlmt_taker ON hl_meme_trades(taker, time_ms);

CREATE TABLE IF NOT EXISTS sm_events (
    ts         INTEGER NOT NULL,
    type       TEXT    NOT NULL,
    address    TEXT    NOT NULL,
    label      TEXT,
    coin       TEXT    NOT NULL,
    side       TEXT    NOT NULL,
    sz         REAL,
    px         REAL,
    notional   REAL,
    pos_before REAL,
    pos_after  REAL,
    closed_pnl REAL,
    taker      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_sm_coin_ts ON sm_events(coin, ts);
CREATE INDEX IF NOT EXISTS ix_sm_addr_ts ON sm_events(address, ts);

CREATE TABLE IF NOT EXISTS signals (
    ts             INTEGER NOT NULL,
    coin           TEXT    NOT NULL,
    direction      TEXT    NOT NULL,   -- 'long' / 'short'
    score          REAL    NOT NULL,   -- 带符号共振分（正多负空）
    structure_bias REAL,               -- SMC 结构偏向 [-1,1]
    flow_bias      REAL,               -- 聪明钱流向偏向 [-1,1]
    flow_net_usd   REAL,
    oi_change_pct  REAL,
    onchain_usd    REAL,
    entry          REAL,               -- 入场价
    stop           REAL,               -- 止损价（SMC 结构位）
    target         REAL,               -- 目标价
    rr             REAL,               -- 盈亏比
    reason         TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_coin_ts ON signals(coin, ts);

CREATE TABLE IF NOT EXISTS divergence (
    ts            INTEGER NOT NULL,
    coin          TEXT    NOT NULL,
    direction     TEXT    NOT NULL,   -- 'bullish'(吸筹) / 'bearish'(分销)
    score         REAL    NOT NULL,
    funding       REAL,               -- CEX 资金费（拥挤方向代理）
    oi_change_pct REAL,
    dex_flow_usd  REAL,               -- DEX 聪明钱净流向
    reason        TEXT
);
CREATE INDEX IF NOT EXISTS ix_divergence_coin_ts ON divergence(coin, ts);

CREATE TABLE IF NOT EXISTS whale_signals (
    ts        INTEGER NOT NULL,
    address   TEXT    NOT NULL,
    label     TEXT,
    coin      TEXT    NOT NULL,
    action    TEXT    NOT NULL,   -- OPEN / ADD / FLIP
    direction TEXT    NOT NULL,   -- 'long' / 'short'
    notional  REAL,
    px        REAL,
    pos_after REAL,
    taker     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_whale_coin_ts ON whale_signals(coin, ts);
CREATE INDEX IF NOT EXISTS ix_whale_addr_ts ON whale_signals(address, ts);

CREATE TABLE IF NOT EXISTS whale_positions (
    address  TEXT    NOT NULL,
    coin     TEXT    NOT NULL,
    szi      REAL    NOT NULL,
    notional REAL,
    label    TEXT,
    ts       INTEGER NOT NULL,
    PRIMARY KEY (address, coin)
);

CREATE TABLE IF NOT EXISTS position_changes (
    ts         INTEGER NOT NULL,
    address    TEXT    NOT NULL,
    label      TEXT,
    coin       TEXT    NOT NULL,
    kind       TEXT    NOT NULL,   -- exit(平仓) / reversal(反手) / reduce(减仓)
    direction  TEXT,               -- 涉及方向 long/short
    prev_notional REAL,
    new_notional  REAL
);
CREATE INDEX IF NOT EXISTS ix_poschg_coin_ts ON position_changes(coin, ts);
CREATE INDEX IF NOT EXISTS ix_poschg_addr_ts ON position_changes(address, ts);

CREATE TABLE IF NOT EXISTS consensus (
    ts           INTEGER NOT NULL,
    coin         TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    n_agree      INTEGER NOT NULL,
    n_oppose     INTEGER NOT NULL,
    net_notional REAL,
    score        REAL,
    labels       TEXT
);
CREATE INDEX IF NOT EXISTS ix_consensus_coin_ts ON consensus(coin, ts);

CREATE TABLE IF NOT EXISTS confluence_signals (
    ts        INTEGER NOT NULL,
    coin      TEXT    NOT NULL,
    direction TEXT    NOT NULL,
    n_sources INTEGER NOT NULL,
    sources   TEXT,
    opposing  INTEGER,
    score     REAL
);
CREATE INDEX IF NOT EXISTS ix_confl_coin_ts ON confluence_signals(coin, ts);

CREATE TABLE IF NOT EXISTS flagged_addresses (
    address       TEXT    PRIMARY KEY,
    first_seen_ms INTEGER NOT NULL,
    coin          TEXT,               -- 首次触发的 coin
    reason        TEXT,
    net_usd       REAL,               -- 触发时的窗口净建仓
    promoted      INTEGER DEFAULT 0,  -- 是否已升级为全量跟踪(订阅 userFills)
    last_seen_ms  INTEGER
);

CREATE TABLE IF NOT EXISTS address_profiles (
    address      TEXT    PRIMARY KEY,
    score        REAL,               -- 聪明钱综合评分 0-100
    account_value REAL,              -- 账户净值 USD
    alltime_pnl  REAL,               -- 全期 PnL
    month_pnl    REAL,               -- 近月 PnL
    win_rate     REAL,               -- 近期成交胜率
    realized_pnl REAL,               -- 近期已实现盈亏
    n_trades     INTEGER,            -- 近期成交笔数
    net_bias     TEXT,               -- 净敞口偏向 多/空
    fav_coins    TEXT,               -- 偏好币(逗号分隔)
    ts           INTEGER
);

CREATE TABLE IF NOT EXISTS whale_pnl_snapshots (
    address       TEXT    NOT NULL,
    label         TEXT,
    day_pnl       REAL,
    week_pnl      REAL,
    month_pnl     REAL,
    alltime_pnl   REAL,
    account_value REAL,
    ts            INTEGER NOT NULL,
    PRIMARY KEY (address, ts)
);
CREATE INDEX IF NOT EXISTS ix_wpnl_addr_ts ON whale_pnl_snapshots(address, ts);

CREATE TABLE IF NOT EXISTS watched_wallets (
    address       TEXT PRIMARY KEY,
    label         TEXT,
    source        TEXT,              -- 'discover'(排行榜发现) / 'suspicious'(可疑升级) / 'manual'
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER,
    account_value REAL,              -- 最近账户净值 USD
    total_ntl_pos REAL,              -- 最近总持仓名义 USD
    n_positions   INTEGER            -- 最近非空持仓数
);

CREATE TABLE IF NOT EXISTS wallet_positions_full (
    address        TEXT NOT NULL,
    coin           TEXT NOT NULL,
    direction      TEXT NOT NULL,    -- 'long' / 'short'
    szi            REAL NOT NULL,
    entry_px       REAL,
    position_value REAL,             -- 名义 USD
    unrealized_pnl REAL,
    leverage       REAL,
    liquidation_px REAL,
    ts             INTEGER NOT NULL,
    PRIMARY KEY (address, coin, ts)
);
CREATE INDEX IF NOT EXISTS ix_wpf_addr_ts ON wallet_positions_full(address, ts);

CREATE TABLE IF NOT EXISTS flow_predictions (
    ts        INTEGER NOT NULL,
    coin      TEXT    NOT NULL,
    direction TEXT    NOT NULL,   -- 'long' / 'short'
    score     REAL,
    vel       REAL,               -- 资金流速度 $/min
    accel     REAL,               -- 资金流加速度(2阶导,领先信号)
    book_imb  REAL                -- 订单簿失衡(挂单意图,先于成交)
);
CREATE INDEX IF NOT EXISTS ix_flowpred_coin_ts ON flow_predictions(coin, ts);

CREATE TABLE IF NOT EXISTS okx_signals (
    ts        INTEGER NOT NULL,
    coin      TEXT    NOT NULL,
    direction TEXT    NOT NULL,   -- 'long' / 'short'
    kind      TEXT,               -- 'accumulation' / 'distribution'
    funding   REAL,               -- OKX 资金费率（触发时快照）
    net_flow  REAL                -- taker 净流向 USD
);
CREATE INDEX IF NOT EXISTS ix_okx_signals_coin_ts ON okx_signals(coin, ts);

CREATE TABLE IF NOT EXISTS hl_orderbook_walls (
    ts       INTEGER NOT NULL,
    coin     TEXT    NOT NULL,
    side     TEXT    NOT NULL,    -- 'bid'(支撑/吸筹意图) / 'ask'(压制/分销意图)
    kind     TEXT    NOT NULL,    -- 'build'(墙出现) / 'pull'(抽单)
    px       REAL,
    notional REAL                 -- 墙名义 USD = px × sz
);
CREATE INDEX IF NOT EXISTS ix_hl_obwalls_coin_ts ON hl_orderbook_walls(coin, ts);

CREATE TABLE IF NOT EXISTS harmonic_setups (
    ts         INTEGER,
    coin       TEXT,
    tf         TEXT,
    kind       TEXT,       -- 'completed' / 'forming'
    pattern    TEXT,
    direction  TEXT,       -- 'long' / 'short'
    price      REAL,
    entry_lo   REAL,       -- forming 无精确值时为 PRZ 下沿
    entry_hi   REAL,       -- forming 无精确值时为 PRZ 上沿
    stop       REAL,       -- forming 存 NULL
    target1    REAL,       -- forming 存 NULL
    target2    REAL,       -- forming 存 NULL
    rr         REAL,       -- forming 存 NULL
    confidence REAL,
    knn        TEXT,       -- '✓' / '✗' / '?'
    orderflow  TEXT,       -- '✓bidXXX' / '✗' / ''（无数据）
    fib_note   TEXT,
    prz_lo     REAL,
    prz_hi     REAL,
    -- XABCD 点坐标（供图表叠加，forming 未完成时为 NULL）
    x_idx      INTEGER,
    x_px       REAL,
    a_idx      INTEGER,
    a_px       REAL,
    b_idx      INTEGER,
    b_px       REAL,
    c_idx      INTEGER,
    c_px       REAL,
    d_idx      INTEGER,
    d_px       REAL
);
CREATE INDEX IF NOT EXISTS ix_harmonic_ts      ON harmonic_setups(ts);
CREATE INDEX IF NOT EXISTS ix_harmonic_coin_ts ON harmonic_setups(coin, ts);

-- 用户「发现搜集」的币（dashboard 按钮触发扫描发现 → 监控进程并入谐波宇宙持续监控）
CREATE TABLE IF NOT EXISTS harmonic_collected (
    coin     TEXT    NOT NULL PRIMARY KEY,
    symbol   TEXT    NOT NULL,
    added_ts INTEGER NOT NULL
);

-- 7 周期布林带压力/支撑层（S/R 前瞻层，供详情页多周期叠加）
CREATE TABLE IF NOT EXISTS bb_levels (
    coin     TEXT    NOT NULL,
    tf       TEXT    NOT NULL,
    ts       INTEGER NOT NULL,
    upper    REAL,
    mid      REAL,
    lower    REAL,
    pct_b    REAL,
    squeeze  INTEGER,
    PRIMARY KEY (coin, tf, ts)
);

CREATE TABLE IF NOT EXISTS bitget_candles (
    coin     TEXT    NOT NULL,
    tf       TEXT    NOT NULL,
    open_ms  INTEGER NOT NULL,
    o        REAL    NOT NULL,
    h        REAL    NOT NULL,
    l        REAL    NOT NULL,
    c        REAL    NOT NULL,
    v        REAL    NOT NULL,
    PRIMARY KEY (coin, tf, open_ms)
);
CREATE INDEX IF NOT EXISTS ix_bitget_candles_coin_tf_ms ON bitget_candles(coin, tf, open_ms);
"""


class Store:
    def __init__(self, path: str | Path = "data/smc.db") -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：允许 asyncio.to_thread 跨线程 flush(WAL 下安全)
        self.conn = sqlite3.connect(str(p), isolation_level=None,   # autocommit；显式控制事务
                                    check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        # 写锁等待最多 5s，根治多进程并发写 database is locked（dashboard 进程 + 监控进程）
        self.conn.execute("PRAGMA busy_timeout=5000;")
        # 页缓存 16MB（负值=KB），谐波/OI 历史扫描读多受益；内存可控
        self.conn.execute("PRAGMA cache_size=-16000;")
        # 临时 B-tree/排序走内存，ORDER BY confidence DESC / MAX(ts) 子查询受益
        self.conn.execute("PRAGMA temp_store=MEMORY;")
        self.conn.executescript(SCHEMA)
        # 旧库迁移：补齐 signals 风险字段 + 成交跟踪字段（SQLite 无 ADD COLUMN IF NOT EXISTS）
        self._ensure_columns("signals", {"entry": "REAL", "stop": "REAL",
                                         "target": "REAL", "rr": "REAL",
                                         "status": "TEXT DEFAULT 'open'", "exit_price": "REAL",
                                         "exit_ts": "INTEGER", "realized_r": "REAL"})
        # 旧库迁移：wallet_positions_full 增加开仓时间/平仓时间/持仓时长列
        self._ensure_columns("wallet_positions_full", {
            "open_ms": "INTEGER",
            "last_close_ms": "INTEGER",
            "hold_sec": "INTEGER",
        })
        # 旧库迁移：harmonic_setups 追加 XABCD 点坐标列（v2 新增，旧库缺失）
        self._ensure_columns("harmonic_setups", {
            "x_idx": "INTEGER",
            "x_px":  "REAL",
            "a_idx": "INTEGER",
            "a_px":  "REAL",
            "b_idx": "INTEGER",
            "b_px":  "REAL",
            "c_idx": "INTEGER",
            "c_px":  "REAL",
            "d_idx": "INTEGER",
            "d_px":  "REAL",
        })

    def _ensure_columns(self, table: str, cols: dict[str, str]) -> None:
        existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    def pragma(self, name: str) -> int | str:
        """读取指定 PRAGMA 的当前值（供测试断言 PRAGMA 生效；纯读，零孤儿——被 test 引用即接入）。"""
        return self.conn.execute(f"PRAGMA {name}").fetchone()[0]

    # ---- 合约地址 ----
    def upsert_contract(self, coin: str, chain: str, contract: str, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO meme_contracts(coin,chain,contract,updated_ms) VALUES(?,?,?,?) "
            "ON CONFLICT(coin,chain) DO UPDATE SET contract=excluded.contract, updated_ms=excluded.updated_ms",
            (coin, chain, contract, ts),
        )

    def contracts(self, coin: str | None = None) -> list[tuple]:
        if coin:
            return self.conn.execute(
                "SELECT coin,chain,contract FROM meme_contracts WHERE coin=? ORDER BY chain", (coin,)
            ).fetchall()
        return self.conn.execute("SELECT coin,chain,contract FROM meme_contracts ORDER BY coin,chain").fetchall()

    # ---- Bitget OI ----
    def insert_oi(self, rows: Iterable[tuple]) -> None:
        """rows: (symbol, coin, oi_size, oi_usd, mark_px, funding, ts)"""
        self.conn.executemany(
            "INSERT OR REPLACE INTO bitget_oi(symbol,coin,oi_size,oi_usd,mark_px,funding,ts) "
            "VALUES(?,?,?,?,?,?,?)", rows)

    def latest_oi(self, symbol: str) -> tuple | None:
        return self.conn.execute(
            "SELECT symbol,coin,oi_size,oi_usd,mark_px,funding,ts FROM bitget_oi "
            "WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)).fetchone()

    # ---- OKX 永续 ----
    def insert_okx_perp(self, rows: Iterable[tuple]) -> None:
        """rows: (inst_id, coin, oi_ccy, oi_usd, mark_px, funding, net_flow, ts)"""
        self.conn.executemany(
            "INSERT OR REPLACE INTO okx_perp"
            "(inst_id,coin,oi_ccy,oi_usd,mark_px,funding,net_flow,ts) "
            "VALUES(?,?,?,?,?,?,?,?)", rows)

    def latest_okx_perp(self, inst_id: str) -> tuple | None:
        return self.conn.execute(
            "SELECT inst_id,coin,oi_ccy,oi_usd,mark_px,funding,net_flow,ts FROM okx_perp "
            "WHERE inst_id=? ORDER BY ts DESC LIMIT 1", (inst_id,)).fetchone()

    # ---- OKX 强平 ----
    def insert_okx_liquidations(self, rows: Iterable[tuple]) -> None:
        """rows: (coin, pos_side, side, notional_usd, bk_px, ts)"""
        self.conn.executemany(
            "INSERT INTO okx_liquidations(coin,pos_side,side,notional_usd,bk_px,ts) "
            "VALUES(?,?,?,?,?,?)", rows)

    def recent_okx_liquidations(self, since_ms: int) -> list[tuple]:
        """查询 since_ms 后所有强平行，按 ts ASC。
        返回列：(ts, coin, pos_side, side, notional_usd, bk_px)。
        """
        return self.conn.execute(
            "SELECT ts,coin,pos_side,side,notional_usd,bk_px FROM okx_liquidations "
            "WHERE ts>=? ORDER BY ts ASC", (since_ms,)).fetchall()

    def oi_change(self, symbol: str, window_ms: int, now_ms: int) -> tuple | None:
        """返回 (最新oi, window 前最近一条 oi)，用于算 OI 变化。"""
        latest = self.latest_oi(symbol)
        if not latest:
            return None
        past = self.conn.execute(
            "SELECT oi_size FROM bitget_oi WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (symbol, now_ms - window_ms)).fetchone()
        return (latest[2], past[0] if past else None)

    # ---- HL meme 成交 ----
    def insert_hl_meme_trades(self, rows: Iterable[tuple]) -> None:
        """rows: (coin,px,sz,notional,taker_side,buyer,seller,taker,hash,tid,time_ms)"""
        self.conn.executemany(
            "INSERT INTO hl_meme_trades(coin,px,sz,notional,taker_side,buyer,seller,taker,hash,tid,time_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)

    def top_meme_takers(self, coin: str, since_ms: int, limit: int = 10) -> list[tuple]:
        """某 meme 近期净主动买入名义最大的地址（买为正卖为负）。"""
        return self.conn.execute(
            "SELECT taker, "
            "SUM(CASE WHEN taker_side='B' THEN notional ELSE -notional END) AS net "
            "FROM hl_meme_trades WHERE coin=? AND time_ms>=? GROUP BY taker "
            "ORDER BY ABS(net) DESC LIMIT ?", (coin, since_ms, limit)).fetchall()

    # ---- 聪明钱事件 ----
    def insert_sm_event(self, row: tuple) -> None:
        """row: (ts,type,address,label,coin,side,sz,px,notional,pos_before,pos_after,closed_pnl,taker)"""
        self.conn.execute(
            "INSERT INTO sm_events(ts,type,address,label,coin,side,sz,px,notional,"
            "pos_before,pos_after,closed_pnl,taker) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", row)

    def insert_sm_events_batch(self, rows: Iterable[tuple]) -> int:
        """批量写入 sm_events，复用 insert_sm_event SQL；空 rows 安全返回 0。

        A2a：热路径缓冲（WS 回调 _sm_buffer.append），由 _periodic_flush 调 asyncio.to_thread 批量落。
        列顺序与 insert_sm_event 完全一致（13 列）：
          ts, type, address, label, coin, side, sz, px, notional,
          pos_before, pos_after, closed_pnl, taker
        事务模式复用 BEGIN/COMMIT/ROLLBACK（与 insert_harmonic_setups 一致）。
        """
        rows_list = list(rows)
        if not rows_list:
            return 0
        try:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT INTO sm_events(ts,type,address,label,coin,side,sz,px,notional,"
                "pos_before,pos_after,closed_pnl,taker) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows_list,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return len(rows_list)

    # ---- 信号 ----
    def insert_signal(self, row: tuple) -> None:
        """row: (ts,coin,direction,score,structure_bias,flow_bias,flow_net_usd,
        oi_change_pct,onchain_usd,entry,stop,target,rr,reason)"""
        self.conn.execute(
            "INSERT INTO signals(ts,coin,direction,score,structure_bias,flow_bias,"
            "flow_net_usd,oi_change_pct,onchain_usd,entry,stop,target,rr,reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)

    # ---- 庄持仓快照（轮询模式跨运行接力）----
    def load_whale_positions(self) -> dict[tuple[str, str], float]:
        """读上次快照，返回 {(addr,coin): 带符号名义}。"""
        rows = self.conn.execute(
            "SELECT address, coin, notional FROM whale_positions").fetchall()
        return {(a, c): n for a, c, n in rows}

    def save_whale_positions(self, rows: list[tuple]) -> None:
        """覆盖快照。rows: (address, coin, szi, notional, label, ts)。

        空输入直接返回(本轮无持仓/抓取失败时不抹掉上轮快照，否则跨运行漏检庄退场)；
        DELETE+INSERT 用显式事务保证原子(中途崩溃不留空表)。
        """
        if not rows:
            return
        try:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM whale_positions")
            self.conn.executemany(
                "INSERT INTO whale_positions(address,coin,szi,notional,label,ts) "
                "VALUES(?,?,?,?,?,?)", rows)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def insert_position_change(self, row: tuple) -> None:
        """row: (ts,address,label,coin,kind,direction,prev_notional,new_notional)"""
        self.conn.execute(
            "INSERT INTO position_changes(ts,address,label,coin,kind,direction,"
            "prev_notional,new_notional) VALUES(?,?,?,?,?,?,?,?)", row)

    def insert_confluence(self, row: tuple) -> None:
        """row: (ts,coin,direction,n_sources,sources,opposing,score)"""
        self.conn.execute(
            "INSERT INTO confluence_signals(ts,coin,direction,n_sources,sources,"
            "opposing,score) VALUES(?,?,?,?,?,?,?)", row)

    def insert_consensus(self, row: tuple) -> None:
        """row: (ts,coin,direction,n_agree,n_oppose,net_notional,score,labels)"""
        self.conn.execute(
            "INSERT INTO consensus(ts,coin,direction,n_agree,n_oppose,net_notional,"
            "score,labels) VALUES(?,?,?,?,?,?,?,?)", row)

    # ---- 可疑地址标记 + 轨迹 ----
    def flag_address(self, address: str, ts: int, coin: str, reason: str,
                     net_usd: float, promoted: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO flagged_addresses(address,first_seen_ms,coin,reason,net_usd,"
            "promoted,last_seen_ms) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET last_seen_ms=excluded.last_seen_ms, "
            "net_usd=excluded.net_usd, promoted=MAX(flagged_addresses.promoted,excluded.promoted)",
            (address, ts, coin, reason, net_usd, promoted, ts))

    def is_flagged(self, address: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM flagged_addresses WHERE address=?", (address,)).fetchone() is not None

    def flagged_addresses(self, limit: int = 50) -> list[tuple]:
        return self.conn.execute(
            "SELECT address,coin,reason,net_usd,promoted,first_seen_ms,last_seen_ms "
            "FROM flagged_addresses ORDER BY last_seen_ms DESC LIMIT ?", (limit,)).fetchall()

    def address_trajectory(self, address: str, since_ms: int = 0,
                           limit: int = 200) -> list[tuple]:
        """某地址的 meme 成交轨迹（时间线：时间/coin/方向/名义/价/是否主动）。"""
        return self.conn.execute(
            "SELECT time_ms, coin, CASE WHEN buyer=? THEN 'BUY' ELSE 'SELL' END, "
            "notional, px, CASE WHEN taker=? THEN 1 ELSE 0 END "
            "FROM hl_meme_trades WHERE (buyer=? OR seller=?) AND time_ms>=? "
            "ORDER BY time_ms DESC LIMIT ?",
            (address, address, address, address, since_ms, limit)).fetchall()

    def insert_whale_signal(self, row: tuple) -> None:
        """row: (ts,address,label,coin,action,direction,notional,px,pos_after,taker)"""
        self.conn.execute(
            "INSERT INTO whale_signals(ts,address,label,coin,action,direction,"
            "notional,px,pos_after,taker) VALUES(?,?,?,?,?,?,?,?,?,?)", row)

    def insert_divergence(self, row: tuple) -> None:
        """row: (ts,coin,direction,score,funding,oi_change_pct,dex_flow_usd,reason)"""
        self.conn.execute(
            "INSERT INTO divergence(ts,coin,direction,score,funding,oi_change_pct,"
            "dex_flow_usd,reason) VALUES(?,?,?,?,?,?,?,?)", row)

    def insert_flow_prediction(self, row: tuple) -> None:
        """落库前瞻资金流预测（领先维度：挂单意图 + 流加速度，先于已成交信号）。
        row: (ts, coin, direction, score, vel, accel, book_imb)
        """
        self.conn.execute(
            "INSERT INTO flow_predictions(ts,coin,direction,score,vel,accel,book_imb) "
            "VALUES(?,?,?,?,?,?,?)", row)

    # ---- OKX 资金费×净流向背离信号 ----
    def insert_okx_signal(
        self,
        ts: int,
        coin: str,
        direction: str,
        kind: str,
        funding: float,
        net_flow: float,
    ) -> None:
        """落库 OKX 资金费×净流向背离信号。direction='long'|'short'。"""
        self.conn.execute(
            "INSERT INTO okx_signals(ts,coin,direction,kind,funding,net_flow) "
            "VALUES(?,?,?,?,?,?)",
            (ts, coin, direction, kind, funding, net_flow),
        )

    def recent_okx_signals(self, since_ms: int) -> list:
        """查询 since_ms 后所有 OKX 信号行，按 ts ASC。"""
        return self.conn.execute(
            "SELECT ts,coin,direction,kind,funding,net_flow FROM okx_signals "
            "WHERE ts>=? ORDER BY ts ASC",
            (since_ms,),
        ).fetchall()

    # ---- HL 挂单墙动态（领先信号：未成交意图，先于成交）----
    def insert_orderbook_walls(self, rows: Iterable[tuple]) -> None:
        """批量落库挂单墙事件。rows: (ts, coin, side, kind, px, notional)。
        side='bid'|'ask'；kind='build'(出现)|'pull'(抽单)。空输入安全（executemany 自处理）。
        """
        self.conn.executemany(
            "INSERT INTO hl_orderbook_walls(ts,coin,side,kind,px,notional) "
            "VALUES(?,?,?,?,?,?)", rows)

    def recent_orderbook_walls(self, since_ms: int) -> list[tuple]:
        """查询 since_ms 后所有挂单墙事件，按 ts ASC。
        返回列：(ts, coin, side, kind, px, notional)。
        """
        return self.conn.execute(
            "SELECT ts,coin,side,kind,px,notional FROM hl_orderbook_walls "
            "WHERE ts>=? ORDER BY ts ASC", (since_ms,)).fetchall()

    # ---- 谐波形态历史（历史保留，按 ts 追加；v2 含 XABCD 点坐标列）----
    def insert_harmonic_setups(self, rows: Iterable[tuple]) -> None:
        """追加谐波形态行（带 ts 保留历史，不再 DELETE-then-insert）。

        rows: Iterable[tuple], 每行 19 列（旧格式向后兼容）或 29 列（含 XABCD 点）。
          19 列顺序：ts, coin, tf, kind, pattern, direction, price,
                     entry_lo, entry_hi, stop, target1, target2,
                     rr, confidence, knn, orderflow, fib_note, prz_lo, prz_hi
          29 列 = 19 列 + x_idx, x_px, a_idx, a_px, b_idx, b_px,
                           c_idx, c_px, d_idx, d_px

        空 rows 安全返回（不写任何行，不清空历史）。
        向后兼容：19 列行自动补 10 个 None 扩为 29 列再写入。
        """
        rows_list = list(rows)
        if not rows_list:
            return

        # 向后兼容：旧 19 列补 10 个 NULL 扩成 29 列
        def _normalize(r: tuple) -> tuple:
            if len(r) == 19:
                return r + (None,) * 10
            return r

        normalized = [_normalize(r) for r in rows_list]
        try:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT INTO harmonic_setups("
                "ts,coin,tf,kind,pattern,direction,price,"
                "entry_lo,entry_hi,stop,target1,target2,"
                "rr,confidence,knn,orderflow,fib_note,"
                "prz_lo,prz_hi,"
                "x_idx,x_px,a_idx,a_px,b_idx,b_px,c_idx,c_px,d_idx,d_px"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?,?,?)",
                normalized,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def recent_harmonic_setups(self) -> list[tuple]:
        """返回每币每周期最新行（per-coin per-tf latest），按 confidence DESC。

        B2 变更：原「全局 MAX(ts) 快照」改为「每币每 tf 各自取最新 ts」，
        使实时层可按单币落库而不会导致其他币从列表消失（per-coin latest 语义）。

        利用 ix_harmonic_coin_ts 索引（CREATE INDEX IF NOT EXISTS ix_harmonic_coin_ts
        ON harmonic_setups(coin, ts DESC)）提升 GROUP BY 子查询效率。

        返回列（29 列）：
          ts, coin, tf, kind, pattern, direction, price,
          entry_lo, entry_hi, stop, target1, target2,
          rr, confidence, knn, orderflow, fib_note, prz_lo, prz_hi,
          x_idx, x_px, a_idx, a_px, b_idx, b_px, c_idx, c_px, d_idx, d_px
        """
        return self.conn.execute(
            "SELECT ts,coin,tf,kind,pattern,direction,price,"
            "entry_lo,entry_hi,stop,target1,target2,"
            "rr,confidence,knn,orderflow,fib_note,"
            "prz_lo,prz_hi,"
            "x_idx,x_px,a_idx,a_px,b_idx,b_px,c_idx,c_px,d_idx,d_px "
            "FROM harmonic_setups "
            "WHERE (coin,tf,ts) IN ("
            "  SELECT coin,tf,MAX(ts) FROM harmonic_setups GROUP BY coin,tf"
            ") "
            "ORDER BY confidence DESC"
        ).fetchall()

    def delete_harmonic_coin_tf(self, coin: str, tf: str) -> None:
        """删除指定 (coin, tf) 的全部历史行，供实时单币落库前去重。

        实时层（B2）在写入新行前调用，防止每次 K 线收盘都追加旧行造成膨胀。
        7 天 prune_before 是长期防线；此方法是短期「按币清旧」机制。
        事务提交由调用方保证（autocommit 连接，execute 立即生效）。
        """
        try:
            self.conn.execute(
                "DELETE FROM harmonic_setups WHERE coin=? AND tf=?",
                (coin, tf),
            )
        except Exception:  # noqa: BLE001
            log.warning("delete_harmonic_coin_tf 失败 %s/%s", coin, tf)
            raise

    # ---- 「发现搜集」的币（用户按钮触发，监控进程并入谐波宇宙）----
    def add_harmonic_collected(self, items: Iterable[tuple]) -> None:
        """加入收集币（幂等 upsert）。items: [(coin, symbol, added_ts), ...]。空安全返回。"""
        rows = list(items)
        if not rows:
            return
        try:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT INTO harmonic_collected(coin,symbol,added_ts) VALUES(?,?,?) "
                "ON CONFLICT(coin) DO UPDATE SET symbol=excluded.symbol",
                rows,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def get_harmonic_collected(self) -> dict[str, str]:
        """返回收集币 {coin: symbol}。"""
        return {
            coin: sym
            for coin, sym in self.conn.execute(
                "SELECT coin, symbol FROM harmonic_collected"
            ).fetchall()
        }

    def harmonic_history(self, coin: str, limit: int = 50) -> list[tuple]:
        """返回指定 coin 的历史谐波形态，按 ts 降序（最新在前），最多 limit 行。

        返回列（29 列，与 recent_harmonic_setups 一致）。
        该币无历史时返回 []，不抛。
        """
        return self.conn.execute(
            "SELECT ts,coin,tf,kind,pattern,direction,price,"
            "entry_lo,entry_hi,stop,target1,target2,"
            "rr,confidence,knn,orderflow,fib_note,"
            "prz_lo,prz_hi,"
            "x_idx,x_px,a_idx,a_px,b_idx,b_px,c_idx,c_px,d_idx,d_px "
            "FROM harmonic_setups WHERE coin=? "
            "ORDER BY ts DESC LIMIT ?",
            (coin, limit),
        ).fetchall()

    # ---- 布林带压力/支撑层（7 周期 S/R，供详情页多周期叠加）----

    def insert_bb_levels(self, rows: Iterable[tuple]) -> None:
        """批量写入 bb_levels，同 (coin,tf,ts) 覆盖旧值（PK REPLACE）。

        rows: Iterable[(coin, tf, ts, upper, mid, lower, pct_b, squeeze)]
        空 rows 安全返回（不写任何行）。
        """
        rows_list = list(rows)
        if not rows_list:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO bb_levels"
            "(coin,tf,ts,upper,mid,lower,pct_b,squeeze) "
            "VALUES(?,?,?,?,?,?,?,?)",
            rows_list,
        )

    def recent_bb_levels(self, coin: str) -> list[tuple]:
        """返回指定 coin 各 tf 最新一条 bb_levels（ts 最大那条）。

        返回列：(coin, tf, ts, upper, mid, lower, pct_b, squeeze)
        该 coin 无数据时返回 []，不抛。
        """
        return self.conn.execute(
            "SELECT coin,tf,ts,upper,mid,lower,pct_b,squeeze "
            "FROM bb_levels "
            "WHERE coin=? AND ts=("
            "  SELECT MAX(ts) FROM bb_levels b2 "
            "  WHERE b2.coin=bb_levels.coin AND b2.tf=bb_levels.tf"
            ")",
            (coin,),
        ).fetchall()

    # ---- Bitget 永续 K 线缓存 ----

    def upsert_candles(self, rows: Iterable[tuple]) -> None:
        """批量写入 K 线，同 (coin, tf, open_ms) 覆盖旧值（去重）。

        rows: Iterable[(coin, tf, open_ms, o, h, l, c, v)]
        空 rows 安全返回（executemany 处理 0 行，不 commit 无事务）。
        """
        rows_list = list(rows)
        if not rows_list:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO bitget_candles(coin,tf,open_ms,o,h,l,c,v) "
            "VALUES(?,?,?,?,?,?,?,?)",
            rows_list,
        )

    def get_candles(self, coin: str, tf: str, limit: int = 1000) -> list:
        """读取最近 limit 根 K 线，升序返回 list[Candle]。

        DB 以 DESC 取最新 limit 根，Python 侧再升序排列，确保调用方拿到正确时间顺序。
        tf 不在 GRANULARITY_MS 中时 close_time_ms 偏移量为 0（兜底，不抛）。
        空结果返回 []。
        """
        from ..models import Candle
        from ..bitget.rest import GRANULARITY_MS

        gran_ms = GRANULARITY_MS.get(tf, 0)
        raw = self.conn.execute(
            "SELECT open_ms,o,h,l,c,v FROM bitget_candles "
            "WHERE coin=? AND tf=? ORDER BY open_ms DESC LIMIT ?",
            (coin, tf, limit),
        ).fetchall()
        if not raw:
            return []
        # 升序排列（DB 取最新 N 根后反转）
        raw_asc = list(reversed(raw))
        return [
            Candle(
                coin=coin,
                interval=tf,
                open_time_ms=row[0],
                close_time_ms=row[0] + gran_ms,
                o=row[1],
                h=row[2],
                l=row[3],
                c=row[4],
                v=row[5],
                n=0,
            )
            for row in raw_asc
        ]

    def count_candles(self, coin: str, tf: str) -> int:
        """返回指定 coin/tf 的 K 线行数。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM bitget_candles WHERE coin=? AND tf=?",
            (coin, tf),
        ).fetchone()
        return row[0] if row else 0

    # ---- 聪明钱地址画像 ----
    def upsert_address_profile(self, p: dict[str, Any]) -> None:
        """落库/更新地址画像。fav_coins 以逗号拼接存储。"""
        self.conn.execute(
            "INSERT INTO address_profiles(address,score,account_value,alltime_pnl,"
            "month_pnl,win_rate,realized_pnl,n_trades,net_bias,fav_coins,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET "
            "score=excluded.score, account_value=excluded.account_value, "
            "alltime_pnl=excluded.alltime_pnl, month_pnl=excluded.month_pnl, "
            "win_rate=excluded.win_rate, realized_pnl=excluded.realized_pnl, "
            "n_trades=excluded.n_trades, net_bias=excluded.net_bias, "
            "fav_coins=excluded.fav_coins, ts=excluded.ts",
            (p["address"], p.get("score"), p.get("account_value"), p.get("alltime_pnl"),
             p.get("month_pnl"), p.get("win_rate"), p.get("realized_pnl"),
             p.get("n_trades"), p.get("net_bias"), ",".join(p["fav_coins"]), p.get("ts")))

    def top_profiles(self, limit: int = 20) -> list[tuple]:
        """按评分降序返回地址画像。"""
        return self.conn.execute(
            "SELECT address,score,account_value,alltime_pnl,month_pnl,win_rate,"
            "realized_pnl,n_trades,net_bias,fav_coins,ts FROM address_profiles "
            "ORDER BY score DESC LIMIT ?", (limit,)).fetchall()

    def recent_signals(self, coin: str | None = None, limit: int = 20) -> list[tuple]:
        if coin:
            return self.conn.execute(
                "SELECT ts,coin,direction,score,reason FROM signals WHERE coin=? "
                "ORDER BY ts DESC LIMIT ?", (coin, limit)).fetchall()
        return self.conn.execute(
            "SELECT ts,coin,direction,score,reason FROM signals ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()

    # ---- 庄 PnL 动量快照 ----
    def insert_whale_pnl(self, row: tuple) -> None:
        """row: (address,label,day_pnl,week_pnl,month_pnl,alltime_pnl,account_value,ts)"""
        self.conn.execute(
            "INSERT OR REPLACE INTO whale_pnl_snapshots(address,label,day_pnl,week_pnl,"
            "month_pnl,alltime_pnl,account_value,ts) VALUES(?,?,?,?,?,?,?,?)", row)

    def whale_pnl_latest(self, address: str) -> tuple | None:
        return self.conn.execute(
            "SELECT address,alltime_pnl,account_value,ts FROM whale_pnl_snapshots "
            "WHERE address=? ORDER BY ts DESC LIMIT 1", (address,)).fetchone()

    def whale_pnl_before(self, address: str, cutoff_ms: int) -> tuple | None:
        return self.conn.execute(
            "SELECT address,alltime_pnl,account_value,ts FROM whale_pnl_snapshots "
            "WHERE address=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (address, cutoff_ms)).fetchone()

    # ---- 观察钱包注册表 ----
    def upsert_wallet(
        self,
        address: str,
        label: str,
        source: str,
        ts: int,
        account_value: float | None = None,
        total_ntl_pos: float | None = None,
        n_positions: int | None = None,
    ) -> None:
        """INSERT OR IGNORE 建首见记录，再 UPDATE last_seen_ms 和摘要字段。
        label 仅在非空时更新（保留人工标注）。空地址守卫直接 return。
        """
        if not address:
            return
        # 建首见记录（first_seen_ms 只在首次写入时赋值，后续 IGNORE）
        self.conn.execute(
            "INSERT OR IGNORE INTO watched_wallets"
            "(address,label,source,first_seen_ms) VALUES(?,?,?,?)",
            (address, label, source, ts),
        )
        # UPDATE last_seen_ms + 摘要字段；label 仅在非空时覆盖
        if label:
            self.conn.execute(
                "UPDATE watched_wallets SET last_seen_ms=?, account_value=?, "
                "total_ntl_pos=?, n_positions=?, label=?, source=? WHERE address=?",
                (ts, account_value, total_ntl_pos, n_positions, label, source, address),
            )
        else:
            self.conn.execute(
                "UPDATE watched_wallets SET last_seen_ms=?, account_value=?, "
                "total_ntl_pos=?, n_positions=? WHERE address=?",
                (ts, account_value, total_ntl_pos, n_positions, address),
            )

    def load_wallets(self) -> list[tuple]:
        """返回 (address,label,source,first_seen_ms,last_seen_ms,account_value,
        total_ntl_pos,n_positions)，按 account_value DESC NULLS LAST。
        """
        return self.conn.execute(
            "SELECT address,label,source,first_seen_ms,last_seen_ms,"
            "account_value,total_ntl_pos,n_positions "
            "FROM watched_wallets ORDER BY account_value DESC NULLS LAST"
        ).fetchall()

    def save_wallet_positions(self, rows: list[tuple]) -> None:
        """批量插入 wallet_positions_full，原子事务。向后兼容：接受 10 元组（旧）或 13 元组（新，含 open_ms/last_close_ms/hold_sec）。

        10 元组：(address,coin,direction,szi,entry_px,position_value,
                   unrealized_pnl,leverage,liquidation_px,ts)
        13 元组：… + (open_ms,last_close_ms,hold_sec)

        空 rows 直接 return。
        """
        if not rows:
            return
        # 统一扩展为 13 元组（旧 10 元组补 None/0）
        def _normalize(r: tuple) -> tuple:
            if len(r) == 10:
                return r + (None, None, None)
            return r

        normalized = [_normalize(r) for r in rows]
        try:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT OR REPLACE INTO wallet_positions_full"
                "(address,coin,direction,szi,entry_px,position_value,"
                "unrealized_pnl,leverage,liquidation_px,ts,"
                "open_ms,last_close_ms,hold_sec) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                normalized,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def latest_wallet_positions(self, address: str, limit: int = 100) -> list[tuple]:
        """取该地址最新一个 ts 的所有持仓行，按 abs(position_value) DESC。

        返回列（13 列）：
          address,coin,direction,szi,entry_px,position_value,
          unrealized_pnl,leverage,liquidation_px,ts,
          open_ms,last_close_ms,hold_sec
        """
        return self.conn.execute(
            "SELECT address,coin,direction,szi,entry_px,position_value,"
            "unrealized_pnl,leverage,liquidation_px,ts,"
            "open_ms,last_close_ms,hold_sec "
            "FROM wallet_positions_full "
            "WHERE address=? AND ts=(SELECT MAX(ts) FROM wallet_positions_full WHERE address=?) "
            "ORDER BY ABS(position_value) DESC LIMIT ?",
            (address, address, limit),
        ).fetchall()

    def prune_before(self, table: str, ts_col: str, cutoff_ms: int) -> int:
        """按时间列裁剪旧数据，保留 cutoff_ms 之后的所有行。

        table/ts_col 来自调用方固定常量（非外部输入）。
        表不存在或列名错误时 log.warning 返回 0，不抛异常（autocommit conn，无需显式 commit）。
        返回删除行数（cursor.rowcount）。
        """
        try:
            cur = self.conn.execute(
                f"DELETE FROM {table} WHERE {ts_col} < ?",  # noqa: S608
                (cutoff_ms,),
            )
            return cur.rowcount
        except Exception as exc:  # noqa: BLE001
            log.warning("prune_before 失败 table=%s col=%s: %s", table, ts_col, exc)
            return 0

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def close(self) -> None:
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
