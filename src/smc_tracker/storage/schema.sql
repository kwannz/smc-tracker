
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

-- 监控币种清单（watchlist-multi-tf）：主开关 monitored_coins.enabled 打开时驱动采集/谐波/BB 选币
CREATE TABLE IF NOT EXISTS monitored_coins (
    coin     TEXT    NOT NULL PRIMARY KEY,
    symbol   TEXT    NOT NULL,
    added_ts INTEGER NOT NULL,
    note     TEXT    NOT NULL DEFAULT ''
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
