"""AddressMonitor：实时监控 watchlist 聪明钱地址。

数据源：WS userFills（每笔成交回报，含 startPosition / dir / closedPnl）。
职责：
  1. 把每笔成交分类为 OPEN/ADD/REDUCE/CLOSE/FLIP（基于成交前后带符号仓位）；
  2. 维护每个 (address, coin) 的带符号仓位缓存；
  3. 聚合每个 coin 的「聪明钱净流向」（净名义 USD，买为正卖为负）；
  4. 通过回调把事件抛给上层（信号引擎/输出）。

注：首条 userFills 为 isSnapshot=true 的历史回放，仅用于播种仓位，不触发告警。
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any, Callable

from ..config import WatchAddress
from ..hyperliquid.ws_client import HyperliquidWSClient, Subscription
from ..models import Side
from .events import EventType, SmartMoneyEvent

log = logging.getLogger("monitor")

EventCallback = Callable[[SmartMoneyEvent], Any]


class AddressMonitor:
    def __init__(
        self,
        watchlist: list[WatchAddress],
        ws: HyperliquidWSClient,
        on_event: EventCallback,
        large_fill_notional_usd: float = 50_000.0,
    ) -> None:
        self.ws = ws
        self.on_event = on_event
        self.large_fill_notional_usd = large_fill_notional_usd
        self._labels: dict[str, str] = {w.address.lower(): w.label for w in watchlist}
        self._addrs: list[WatchAddress] = watchlist
        # (addr_lower, coin) -> 带符号仓位
        self._pos: dict[tuple[str, str], float] = defaultdict(float)
        # coin -> 净流向名义 USD（自启动累计；买正卖负）
        self._net_flow: dict[str, float] = defaultdict(float)
        self._seeded: set[str] = set()   # 已收到 snapshot 的地址

    def attach(self) -> None:
        """为每个地址注册 userFills + webData2 订阅。需在 ws.run() 前或后调用均可。"""
        for w in self._addrs:
            self.ws.subscribe(
                Subscription(type="userFills", user=w.address),
                self._on_fills,
            )
            # webData2：权威实时持仓快照，用于校正 userFills 增量推算的仓位漂移
            self.ws.subscribe(
                Subscription(type="webData2", user=w.address),
                self._on_web_data2,
            )
        log.info("AddressMonitor 已挂载 %d 个地址（userFills + webData2）", len(self._addrs))

    def add_addresses(self, watchlist: list[WatchAddress]) -> None:
        """注入新监控地址（去重）。须在 attach() 前调用以纳入订阅。"""
        for w in watchlist:
            a = w.address.lower()
            if a not in self._labels:
                self._labels[a] = w.label
                self._addrs.append(w)

    def subscribe_address(self, w: WatchAddress) -> bool:
        """运行中动态新增一个监控地址并立即订阅其 userFills/webData2（用于升级可疑地址为全量跟踪）。
        返回 True 表示新增，False 表示已在监控中。"""
        a = w.address.lower()
        if a in self._labels:
            return False
        self._labels[a] = w.label
        self._addrs.append(w)
        self.ws.subscribe(Subscription(type="userFills", user=w.address), self._on_fills)
        self.ws.subscribe(Subscription(type="webData2", user=w.address), self._on_web_data2)
        return True

    def seed_positions(self, address: str, coin_to_szi: dict[str, float]) -> None:
        """用 REST clearinghouseState 的真实持仓播种（比 snapshot 更准）。"""
        a = address.lower()
        for coin, szi in coin_to_szi.items():
            self._pos[(a, coin)] = szi
        self._seeded.add(a)

    # ---- WS 回调 ----
    def _on_fills(self, data: dict[str, Any], recv_ns: int) -> None:
        user = (data.get("user") or "").lower()
        is_snapshot = bool(data.get("isSnapshot"))
        fills = data.get("fills") or []
        for f in fills:
            self._handle_fill(user, f, is_snapshot)

    def _on_web_data2(self, data: dict[str, Any], recv_ns: int) -> None:
        """用 webData2 的 clearinghouseState 权威校正持仓缓存（含归零已平仓 coin）。"""
        user = (data.get("user") or "").lower()
        ch = data.get("clearinghouseState") or {}
        present: set[str] = set()
        for ap in ch.get("assetPositions", []):
            p = ap.get("position", {})
            coin = p.get("coin")
            if not coin:
                continue
            present.add(coin)
            self._pos[(user, coin)] = _f(p.get("szi"))
        # 缓存中该地址不在本次快照里的 coin → 已平仓，归零
        for (u, coin) in list(self._pos):
            if u == user and coin not in present:
                self._pos[(u, coin)] = 0.0
        self._seeded.add(user)

    def _handle_fill(self, user: str, f: dict[str, Any], is_snapshot: bool) -> None:
        coin = f.get("coin", "")
        if not coin:
            return
        # 现货成交在 Hyperliquid 中 coin 形如 "@107"（spot index），无多空概念，
        # 不参与永续的开/平/加/减/反手分类与净流向；如需现货监控须单独处理。
        if coin.startswith("@"):
            return
        side = Side.from_hl(f.get("side", "B"))
        sz = _f(f.get("sz"))
        px = _f(f.get("px"))
        if sz <= 0 or px <= 0:
            return
        signed = sz if side is Side.BUY else -sz
        key = (user, coin)

        # snapshot：仅在未被 REST 播种时，用 startPosition+本笔推算仓位，不告警
        if is_snapshot:
            if user not in self._seeded:
                self._pos[key] = _f(f.get("startPosition")) + signed
            return

        # before 优先用每笔权威 startPosition(HL 自带成交前仓位)，仅字段缺失时回退缓存；
        # 这样丢包/乱序造成的累计漂移可逐笔自愈，与 webData2 周期校正互补。
        sp = f.get("startPosition")
        before = _f(sp) if sp is not None else self._pos.get(key, 0.0)
        after = before + signed
        # 浮点抖动归零
        if abs(after) < 1e-12:
            after = 0.0
        if after == 0.0:
            self._pos.pop(key, None)     # 归零即删除，防大量短命 meme 持仓字典无界膨胀
        else:
            self._pos[key] = after

        etype = _classify(before, after)
        notional = sz * px
        # 净流向累计（吃单方向即资金流向）
        self._net_flow[coin] += notional if side is Side.BUY else -notional

        evt = SmartMoneyEvent(
            type=etype,
            address=user,
            label=self._labels.get(user, ""),
            coin=coin,
            side=side,
            sz=sz,
            px=px,
            notional=notional,
            position_before=before,
            position_after=after,
            closed_pnl=_f(f.get("closedPnl")),
            time_ms=int(f.get("time", 0)),
            is_taker=bool(f.get("crossed", False)),
        )
        try:
            self.on_event(evt)
        except Exception:  # noqa: BLE001
            log.exception("on_event 回调出错")

    # ---- 查询 ----
    def net_flow(self, coin: str) -> float:
        return self._net_flow.get(coin, 0.0)

    def all_net_flows(self) -> dict[str, float]:
        return dict(self._net_flow)

    def position(self, address: str, coin: str) -> float:
        return self._pos.get((address.lower(), coin), 0.0)

    def all_positions(self) -> dict[tuple[str, str], float]:
        """所有监控地址的当前带符号持仓快照 {(addr_lower, coin): szi}（非零）。"""
        return {k: v for k, v in self._pos.items() if v != 0.0}

    def label_of(self, address: str) -> str:
        return self._labels.get(address.lower(), address[:8])


def _classify(before: float, after: float) -> EventType:
    """基于成交前后带符号仓位判定事件类型。"""
    if before == 0.0:
        return EventType.OPEN
    if after == 0.0:
        return EventType.CLOSE
    if (before > 0) != (after > 0):       # 符号翻转 → 反手
        return EventType.FLIP
    if abs(after) > abs(before):          # 同向且变大 → 加仓
        return EventType.ADD
    return EventType.REDUCE               # 同向变小 → 减仓


from ..util import to_float as _f  # 统一安全数值解析
