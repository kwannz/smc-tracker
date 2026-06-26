"""MemeTradeMonitor：System 1 (Hyperliquid) 的 meme 成交地址监控。

数据源：HL trades（公开成交流，每条含买卖双方地址）。
职责：
  1. 订阅每个 meme 永续的 trades；
  2. 从每条成交提取买方 / 卖方 / 主动方（taker）+ 名义金额（notional=px*sz）；
  3. 缓冲批量落 SQLite（hl_meme_trades，executemany）；
  4. 维护内存中 per-address 与 per-coin 的「净主动流向」（taker 买为正卖为负）；
  5. 大单（notional≥阈值）触发可选回调并打印。

HL trades 语义（已实证）：
  每条 = {coin, side, px, sz, time(ms), hash, tid, users:[买方, 卖方]}
  users[0]=买方，users[1]=卖方；side='B' 表示 taker 主动买、'A' 表示 taker 主动卖。
  故 taker = users[0] if side=='B' else users[1]。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

from ..hyperliquid.ws_client import HyperliquidWSClient, Subscription
from ..storage import Store
from ..util import to_float as _f  # 统一安全数值解析（顶层 import，惯例）

log = logging.getLogger("monitor.meme")

# trade 回调签名：on_trade(record: dict) -> None；record 见 _parse 返回结构
TradeCallback = Callable[[dict[str, Any]], Any]


class MemeTradeMonitor:
    def __init__(
        self,
        meme_coins: list[str],
        ws: HyperliquidWSClient,
        store: Store,
        large_notional_usd: float = 10_000.0,
        on_trade: TradeCallback | None = None,
        flush_threshold: int = 200,
        on_suspicious: Callable[[dict[str, Any]], Any] | None = None,
        suspicious_notional: float = 50_000.0,
        suspicious_window_ms: int = 180_000,
    ) -> None:
        self.meme_coins = list(meme_coins)
        self.ws = ws
        self.store = store
        self.large_notional_usd = large_notional_usd
        self.on_trade = on_trade
        self.flush_threshold = flush_threshold
        # 可疑地址检测：任意地址在窗口内对某 coin 的净主动建仓越阈值即上报
        self.on_suspicious = on_suspicious
        self.suspicious_notional = suspicious_notional
        self.suspicious_window_ms = suspicious_window_ms
        # (taker,coin) -> [窗口内净额, 窗口起始ts, 上次上报ts]
        self._susp: dict[tuple[str, str], list[float]] = {}

        # 待落库缓冲：row 顺序 = (coin,px,sz,notional,taker_side,buyer,seller,taker,hash,tid,time_ms)
        self._buffer: list[tuple] = []
        # coin → 净主动流向名义 USD（按 coin 键，有界）
        self._coin_net: dict[str, float] = defaultdict(float)
        # 统计
        self.trades_seen = 0
        self.large_trades_seen = 0

    # ---- 挂载 ----
    def attach(self) -> None:
        """为每个 meme 币注册 trades 订阅。需在 ws.run() 前或后调用均可。"""
        for coin in self.meme_coins:
            self.ws.subscribe(Subscription(type="trades", coin=coin), self._on_trades)
        log.info("MemeTradeMonitor 已挂载 %d 个 meme（trades）", len(self.meme_coins))

    # ---- WS 回调 ----
    def _on_trades(self, data: Any, recv_ns: int) -> None:
        """data 为 list[trade dict]。"""
        if not data:
            return
        for t in data:
            rec = self._parse(t)
            if rec is None:
                continue
            self._ingest(rec)

    def _parse(self, t: dict[str, Any]) -> dict[str, Any] | None:
        """把一条 HL trade 解析为标准 record；非法数据返回 None。"""
        coin = t.get("coin", "")
        if not coin:
            return None
        side = t.get("side", "")
        px = _f(t.get("px"))
        sz = _f(t.get("sz"))
        if px <= 0 or sz <= 0:
            return None
        users = t.get("users") or []
        buyer = users[0] if len(users) > 0 else ""
        seller = users[1] if len(users) > 1 else ""
        # taker = 主动方：'B'→买方主动，'A'→卖方主动
        taker = buyer if side == "B" else seller
        notional = px * sz
        return {
            "coin": coin,
            "px": px,
            "sz": sz,
            "notional": notional,
            "taker_side": side,
            "buyer": buyer,
            "seller": seller,
            "taker": taker,
            "hash": t.get("hash"),
            "tid": t.get("tid"),
            "time_ms": int(_f(t.get("time"), 0)),    # 安全解析：脏值/空串兜底 0，不抛中断整批
        }

    def _ingest(self, rec: dict[str, Any]) -> None:
        """累计净流向、入缓冲、处理大单回调，并按阈值触发 flush。"""
        self.trades_seen += 1
        # 净主动流向：taker 买为正、卖为负
        signed = rec["notional"] if rec["taker_side"] == "B" else -rec["notional"]
        self._coin_net[rec["coin"]] += signed

        # 可疑地址检测：窗口内累积某 taker 对某 coin 的净主动建仓
        if self.on_suspicious is not None and rec["taker"]:
            self._detect_suspicious(rec, signed)

        self._buffer.append((
            rec["coin"], rec["px"], rec["sz"], rec["notional"], rec["taker_side"],
            rec["buyer"], rec["seller"], rec["taker"], rec["hash"], rec["tid"], rec["time_ms"],
        ))

        # 大单：回调 + 打印
        if rec["notional"] >= self.large_notional_usd:
            self.large_trades_seen += 1
            dir_txt = "买入" if rec["taker_side"] == "B" else "卖出"
            log.info(
                "大单 %s %s taker=%s notional=$%.0f buyer=%s seller=%s",
                rec["coin"], dir_txt, rec["taker"], rec["notional"], rec["buyer"], rec["seller"],
            )
            if self.on_trade is not None:
                try:
                    self.on_trade(rec)
                except Exception:  # noqa: BLE001 — 回调异常不影响接收
                    log.exception("on_trade 回调出错")

        # 注意：不在热路径内调用 maybe_flush()；落库由 app._periodic_flush(every=5s) 周期驱动

    def _detect_suspicious(self, rec: dict[str, Any], signed: float) -> None:
        """窗口内累积某 taker 对某 coin 的净主动建仓，越阈值上报（带冷却）。"""
        taker, coin, now = rec["taker"], rec["coin"], rec["time_ms"]
        # 顺手清理过期键，防 (taker,coin) 无界增长(长跑大量短命 meme 地址)
        if len(self._susp) > 20_000:
            ttl = self.suspicious_window_ms * 4
            for k in [k for k, v in self._susp.items()
                      if now - max(v[1], v[2]) > ttl]:
                del self._susp[k]
        key = (taker, coin)
        acc = self._susp.get(key)
        if acc is None or now - acc[1] > self.suspicious_window_ms:
            acc = [0.0, now, acc[2] if acc else 0]
        acc[0] += signed
        self._susp[key] = acc
        cooled = acc[2] == 0 or now - acc[2] >= self.suspicious_window_ms
        if abs(acc[0]) >= self.suspicious_notional and cooled:
            net = acc[0]
            acc[2] = now
            acc[0] = 0.0
            try:
                self.on_suspicious({
                    "address": taker, "coin": coin, "net_usd": net,
                    "direction": "buy" if net > 0 else "sell",
                    "px": rec["px"], "time_ms": now,
                })
            except Exception:  # noqa: BLE001
                log.exception("on_suspicious 回调出错")

    # ---- 落库 ----
    def maybe_flush(self) -> int:
        """缓冲达阈值时落库；返回落库行数（0 表示未触发）。"""
        if len(self._buffer) >= self.flush_threshold:
            return self.flush()
        return 0

    def flush(self) -> int:
        """把缓冲批量 executemany 落库，清空缓冲；返回落库行数。"""
        if not self._buffer:
            return 0
        rows = self._buffer
        self._buffer = []
        self.store.insert_hl_meme_trades(rows)
        return len(rows)

    # ---- 查询 ----
    def coin_net(self, coin: str) -> float:
        """某 coin 自启动累计的净主动流向。"""
        return self._coin_net.get(coin, 0.0)

    def all_coin_net(self) -> dict[str, float]:
        return dict(self._coin_net)
