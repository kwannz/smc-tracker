"""聪明钱事件模型。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..models import Side
from ..util import fmt_px as _fmt_px


class EventType(str, Enum):
    OPEN = "OPEN"        # 从空仓建仓
    ADD = "ADD"          # 同向加仓
    REDUCE = "REDUCE"    # 同向减仓（未平完）
    CLOSE = "CLOSE"      # 平至空仓
    FLIP = "FLIP"        # 多空反手


@dataclass(slots=True)
class SmartMoneyEvent:
    type: EventType
    address: str
    label: str
    coin: str
    side: Side            # 本笔成交方向（买/卖）
    sz: float
    px: float
    notional: float       # 本笔名义价值 USD
    position_before: float
    position_after: float
    closed_pnl: float
    time_ms: int
    is_taker: bool        # 是否吃单（taker，主动成交，更具信息含量）

    @property
    def direction_label(self) -> str:
        """人类可读方向：建多/加空/平多 …"""
        long_short = "多" if self.position_after > 0 or (self.type == EventType.CLOSE and self.position_before > 0) else "空"
        verb = {
            EventType.OPEN: "建", EventType.ADD: "加",
            EventType.REDUCE: "减", EventType.CLOSE: "平", EventType.FLIP: "反手→",
        }[self.type]
        if self.type == EventType.FLIP:
            return f"反手→{'多' if self.position_after > 0 else '空'}"
        return f"{verb}{long_short}"

    def fmt(self) -> str:
        taker = "T" if self.is_taker else "M"
        pnl = f" pnl={self.closed_pnl:+.0f}" if self.closed_pnl else ""
        return (f"[聪明钱] {self.label or self.address[:8]} {self.direction_label} "
                f"{self.coin} sz={_fmt_px(self.sz)} @ {_fmt_px(self.px)} "
                f"名义=${self.notional:,.0f} ({taker}){pnl} "
                f"仓位 {_fmt_px(self.position_before)}→{_fmt_px(self.position_after)}")
