"""庄换仓预警：用持仓快照 diff 检测平仓/反手/大幅减仓。

跟庄信号(成交累积)抓的是「建仓/加仓」；本模块抓互补的「退出」动作 ——
顶级交易员清掉/反转大仓位，是「行情可能结束/止盈/反转」的强预警。

每轮快照所有监控庄的带符号持仓名义(szi×px)，与上一轮 diff：
- exit(平仓)：上轮大仓位 → 本轮归零。
- reversal(反手)：上轮与本轮反向且至少一边够大。
- reduce(减仓)：仍持仓但名义大幅下降。
首轮仅建基线(庄的存量持仓不算「新动作」)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class PositionChange:
    address: str
    label: str
    coin: str
    kind: str            # exit / reversal / reduce
    direction: str       # 涉及方向 long/short
    prev_notional: float
    new_notional: float
    ts: int

    def fmt(self) -> str:
        a = self.label or self.address[:8]
        if self.kind == "exit":
            d = "平多" if self.direction == "long" else "平空"
            return f"🏁庄退场 {a} {d} {self.coin} ${abs(self.prev_notional):,.0f} → 0"
        if self.kind == "reversal":
            return (f"🔄庄反手 {a} {self.coin} "
                    f"{'多→空' if self.prev_notional > 0 else '空→多'} "
                    f"${abs(self.new_notional):,.0f}")
        d = "减多" if self.direction == "long" else "减空"
        return (f"📉庄减仓 {a} {d} {self.coin} "
                f"${abs(self.prev_notional):,.0f}→${abs(self.new_notional):,.0f}")


PosChangeCallback = Callable[[PositionChange], Any]


class WhalePositionTracker:
    def __init__(self, store: Any | None = None, min_notional: float = 1_000_000.0,
                 on_change: PosChangeCallback | None = None) -> None:
        self.store = store
        self.min_notional = min_notional
        self.on_change = on_change
        self._prev: dict[tuple[str, str], float] = {}   # (addr,coin) -> 带符号名义
        self._initialized = False
        self.changes_seen = 0

    def seed_prev(self, prev_notional: dict[tuple[str, str], float]) -> None:
        """用持久化的上次快照(名义)做基线（轮询模式：跨运行 diff，不走首轮基线）。"""
        self._prev = dict(prev_notional)
        self._initialized = True

    def scan(self, positions: dict[tuple[str, str], float], prices: dict[str, float],
             labels: dict[str, str], now_ms: int) -> list[PositionChange]:
        current: dict[tuple[str, str], float] = {}
        for (addr, coin), szi in positions.items():
            px = prices.get(coin)
            if px and szi != 0:
                current[(addr, coin)] = szi * px

        if not self._initialized:
            self._prev = current
            self._initialized = True
            return []

        out: list[PositionChange] = []
        for key in set(self._prev) | set(current):
            prev = self._prev.get(key, 0.0)
            new = current.get(key, 0.0)
            chg = self._classify(prev, new)
            if chg is None:
                continue
            addr, coin = key
            pc = PositionChange(
                address=addr, label=labels.get(addr, addr[:8]), coin=coin,
                kind=chg, direction="long" if prev > 0 else "short",
                prev_notional=prev, new_notional=new, ts=now_ms)
            self.changes_seen += 1
            if self.store is not None:
                self.store.insert_position_change((
                    pc.ts, pc.address, pc.label, pc.coin, pc.kind, pc.direction,
                    pc.prev_notional, pc.new_notional))
            if self.on_change is not None:
                self.on_change(pc)
            out.append(pc)
        self._prev = current
        return out

    def _classify(self, prev: float, new: float) -> str | None:
        m = self.min_notional
        if abs(prev) < m:                       # 上轮非大仓位 → 不关注退出
            return None
        if abs(new) < m * 0.1:                  # 归零 → 平仓
            return "exit"
        if new != 0 and (prev > 0) != (new > 0):  # 反向 → 反手
            return "reversal"
        if abs(prev) - abs(new) >= m:           # 大幅缩水 → 减仓
            return "reduce"
        return None
