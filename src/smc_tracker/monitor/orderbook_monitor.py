"""HLOrderbookMonitor：Hyperliquid l2Book WS 大额挂单墙动态监控（领先信号）。

第一性原理（前瞻 > 回看，CLAUDE.md #1）：
- 挂单是「尚未成交的意图」——大额挂单墙出现(build) = 资金已就位但还没动，**先于成交**。
- 抽单(pull) = 意图撤销/诱多诱空收网，亦是动态信号。

数据源：HL l2Book WS（wss://api.hyperliquid.xyz/ws）。
推送 data = {coin, time(ms), levels: [bids, asks]}，每档 = {px(str), sz(str), n(int 订单数)}，
每侧 20 档，sz 逐推变化（可追踪动态）。

诚实定位（不夸大）：
- 挂单墙 = **意图告警**，可能是 spoof（虚挂诱导）/冰山，**非确定方向**。
- bid 墙 = 支撑/吸筹意图；ask 墙 = 压制/分销意图。仅供前瞻参考，须与成交/OI 交叉验证。

职责：
  1. detect_walls：纯函数，从单侧档位识别 notional 远超均值的大墙；
  2. _on_l2book：与上一帧对比，识别墙的出现(build)/抽单(pull) → 回调 + 缓冲落库；
  3. book_imbalance：复用 orderbook_imbalance 维护每币最新挂单失衡；
  4. flush()：批量 executemany 落 SQLite(hl_orderbook_walls)。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

from ..hyperliquid.ws_client import HyperliquidWSClient, Subscription
from ..signals.flow_predictor import orderbook_imbalance
from ..util import to_float as _f

log = logging.getLogger("monitor.orderbook")

# 挂单墙信号回调签名：on_wall_signal(event: dict) -> None
WallCallback = Callable[[dict[str, Any]], Any]


def detect_walls(
    levels: list[dict], mult: float = 3.0, depth: int = 20
) -> list[tuple[float, float, int]]:
    """从单侧档位识别大额挂单墙（纯函数）。

    - 每档 notional = px × sz（名义 USD）；取前 depth 档算均值；
    - 仅保留 notional ≥ mult × 均值 的档（远超周围 = 墙）；
    - 返回 [(px_float, notional, n), ...]，按 notional 降序。
    - 空/全零安全返回 []。

    参数:
      levels: [{'px','sz','n'}, ...]（HL l2Book 单侧档位，px/sz 为字符串）。
      mult:   墙阈值倍数（相对前 depth 档均值）。
      depth:  参与均值与扫描的档数。
    """
    if not levels:
        return []
    top = levels[:depth]
    # 每档名义 USD（safe 解析，拒 NaN/inf）
    notionals = [_f(lv.get("px")) * _f(lv.get("sz")) for lv in top]
    total = sum(notionals)
    if total <= 0.0:
        return []  # 全零/无效，无墙
    mean = total / len(notionals)
    if mean <= 0.0:
        return []
    thresh = mult * mean
    walls: list[tuple[float, float, int]] = []
    for lv, ntl in zip(top, notionals):
        if ntl >= thresh:
            px = _f(lv.get("px"))
            n = int(_f(lv.get("n")))  # 订单数（safe，缺失/非数→0）
            walls.append((px, ntl, n))
    walls.sort(key=lambda w: w[1], reverse=True)
    return walls


class HLOrderbookMonitor:
    """通过 HL l2Book WS 实时跟踪大额挂单墙的出现(build)/抽单(pull) 动态。"""

    def __init__(
        self,
        coins: list[str],
        ws: HyperliquidWSClient,
        store: Any = None,                       # Store（duck-typed；None 时不落库）
        on_wall_signal: WallCallback | None = None,
        wall_mult: float = 3.0,
        min_wall_usd: float = 200_000.0,
    ) -> None:
        self.coins = list(coins)
        self.ws = ws
        self.store = store
        self.on_wall_signal = on_wall_signal
        self.wall_mult = wall_mult
        self.min_wall_usd = min_wall_usd

        # coin → side("bid"/"ask") → {px_float: (notional, n)}（上一帧墙集，用于对比 build/pull）
        self._walls: dict[str, dict[str, dict[float, tuple[float, int]]]] = defaultdict(
            lambda: {"bid": {}, "ask": {}}
        )
        # coin → 最新挂单失衡 dict（imbalance/bid_usd/ask_usd）
        self._imbalance: dict[str, dict[str, float]] = {}
        # 待落库墙事件缓冲：row = (ts, coin, side, kind, px, notional)
        self._buffer: list[tuple] = []
        # 统计
        self.frames_seen = 0
        self.walls_seen = 0

    # ---- 挂载 ----
    def attach(self) -> None:
        """为每个 coin 订阅 l2Book → _on_l2book。ws.run() 前后调用均可。"""
        for c in self.coins:
            self.ws.subscribe(Subscription(type="l2Book", coin=c), self._on_l2book)
        log.info("HLOrderbookMonitor 已挂载 %d 个币（l2Book 挂单墙动态）", len(self.coins))

    # ---- WS 回调 ----
    def _on_l2book(self, data: dict[str, Any], recv_ns: int) -> None:
        """l2Book 推送 → 两侧 detect_walls → 与上一帧对比识别 build/pull。

        签名与现有 HL handler 一致（data, recv_ns）。
        data = {coin, time(ms), levels: [bids, asks]}。
        """
        coin = data.get("coin")
        if not coin:
            return
        levels = data.get("levels") or [[], []]
        if len(levels) < 2:
            return
        bids = levels[0] or []
        asks = levels[1] or []
        ts = int(_f(data.get("time")))
        self.frames_seen += 1

        # 维护最新挂单失衡（复用 flow_predictor.orderbook_imbalance）
        try:
            self._imbalance[coin] = orderbook_imbalance(bids, asks)
        except Exception:  # noqa: BLE001 — 失衡计算异常不影响墙检测
            pass

        # 两侧分别检测墙，过滤 notional ≥ min_wall_usd
        for side, side_levels in (("bid", bids), ("ask", asks)):
            cur: dict[float, tuple[float, int]] = {}
            for px, ntl, n in detect_walls(side_levels, self.wall_mult):
                if ntl >= self.min_wall_usd:
                    cur[px] = (ntl, n)
            prev = self._walls[coin][side]
            # build：当前有、上一帧无的 px（新出现的墙 = 资金就位意图）
            for px, (ntl, n) in cur.items():
                if px not in prev:
                    self.walls_seen += 1
                    self._emit(coin, side, "build", px, ntl, ts)
            # pull：上一帧有、当前无的 px（抽单 = 意图撤销/收网）
            for px, (ntl, _n) in prev.items():
                if px not in cur:
                    self._emit(coin, side, "pull", px, ntl, ts)
            # 更新状态（on_wall_signal=None 时不触发回调但仍更新状态）
            self._walls[coin][side] = cur

    def _emit(self, coin: str, side: str, kind: str, px: float,
              notional: float, ts: int) -> None:
        """缓冲墙事件落库 + 触发回调（回调 try/except，异常不影响接收）。"""
        self._buffer.append((ts, coin, side, kind, px, notional))
        if self.on_wall_signal is not None:
            try:
                self.on_wall_signal({
                    "coin": coin,
                    "side": side,         # "bid"(支撑/吸筹意图) / "ask"(压制/分销意图)
                    "kind": kind,         # "build"(出现) / "pull"(抽单)
                    "px": px,
                    "notional": notional,
                    "ts": ts,
                })
            except Exception:  # noqa: BLE001 — 回调异常不影响接收循环
                log.exception("on_wall_signal 回调出错")

    # ---- 查询 ----
    def book_imbalance(self, coin: str) -> dict[str, float]:
        """返回该 coin 最新挂单失衡 {imbalance, bid_usd, ask_usd}（无数据返回零）。"""
        return self._imbalance.get(coin, {"imbalance": 0.0, "bid_usd": 0.0, "ask_usd": 0.0})

    def all_walls(self) -> dict[str, dict[str, dict[float, tuple[float, int]]]]:
        """返回当前各币两侧墙集的深拷贝 {coin: {side: {px: (notional, n)}}}。"""
        return {
            coin: {side: dict(pxs) for side, pxs in sides.items()}
            for coin, sides in self._walls.items()
        }

    def confirming_wall(
        self, coin: str, price: float, side: str, tol_pct: float = 0.015
    ) -> dict | None:
        """返回 coin 在 side 侧、距 price 不超过 tol_pct 的**最大**挂单墙（确认 PRZ 的领先意图）。

        side: "bid"（看多 setup 找支撑墙）/ "ask"（看空找压制墙）。
        无符合墙 → None。返回 {"px":float, "notional":float, "n":int, "dist_pct":float}。

        诚实定位：墙可能 spoof（虚挂）/吸收 ≠ 必反转，仅作 PRZ 确认层，非独立信号。
        price <= 0 → None（防止除零/负价格无意义查询）。
        """
        if price <= 0:
            return None

        side_walls = self._walls.get(coin, {}).get(side, {})
        if not side_walls:
            return None

        # 筛选距 price 不超过 tol_pct 的档位，按 notional 降序取最大
        best: dict | None = None
        best_notional = -1.0
        for px, (notional, n) in side_walls.items():
            dist_pct = abs(px - price) / price
            if dist_pct <= tol_pct and notional > best_notional:
                best_notional = notional
                best = {"px": px, "notional": notional, "n": n, "dist_pct": dist_pct}

        return best

    # ---- 落库 ----
    def flush(self) -> int:
        """墙事件缓冲批量落库，清空缓冲；返回落库行数。store=None 时仅清空缓冲。"""
        if not self._buffer:
            return 0
        rows = self._buffer
        self._buffer = []
        if self.store is not None:
            self.store.insert_orderbook_walls(rows)
        return len(rows)
