"""SMC 实时 K 线接入：把 HL candle WS 推送喂给 MarketStructure。

HL candle 推送是「当前未收盘 K 线」的持续更新（同一开盘时间 t 多次刷新 o/h/l/c/v）。
SMC 结构应基于**已收盘** K 线，故：当某 coin 的开盘时间 t 发生变化，说明上一根已收盘，
此时把缓存的上一根 forming 快照喂入 MarketStructure。可先用 candleSnapshot 历史播种。
"""
from __future__ import annotations

from typing import Any, Callable

from ..models import Candle
from .structure import MarketStructure, StructureEvent


from ..util import to_float as _f  # 统一安全数值解析


def candle_from_ws(d: dict[str, Any]) -> Candle:
    """HL candle WS data dict → Candle 模型。"""
    return Candle(
        coin=d.get("s", ""), interval=d.get("i", ""),
        open_time_ms=int(d.get("t", 0)), close_time_ms=int(d.get("T", 0)),
        o=_f(d.get("o")), h=_f(d.get("h")), l=_f(d.get("l")),
        c=_f(d.get("c")), v=_f(d.get("v")), n=int(d.get("n", 0)),
    )


StructureCallback = Callable[[str, StructureEvent], Any]


class StructureFeed:
    """逐 coin 维护 MarketStructure，按 K 线收盘驱动结构事件。"""

    def __init__(self, lookback: int = 3, on_event: StructureCallback | None = None,
                 on_closed: Callable[[str, Candle], Any] | None = None) -> None:
        self.lookback = lookback
        self.on_event = on_event
        self.on_closed = on_closed          # 每根 K 线收盘回调（coin, 已收盘 Candle），驱动 ZoneEngine 等
        self._ms: dict[str, MarketStructure] = {}
        self._last_open: dict[str, int] = {}
        self._prev: dict[str, dict[str, Any]] = {}   # 各 coin 最近一次 forming 快照

    def _struct(self, coin: str) -> MarketStructure:
        ms = self._ms.get(coin)
        if ms is None:
            ms = MarketStructure(self.lookback)
            self._ms[coin] = ms
        return ms

    def seed(self, coin: str, candles: list[Candle]) -> None:
        """用历史已收盘 K 线播种（candleSnapshot）。"""
        ms = self._struct(coin)
        for c in candles:
            ms.update(c)

    def on_candle_ws(self, data: dict[str, Any], recv_ns: int = 0) -> list[StructureEvent]:
        """处理一条 candle WS 推送；仅在检测到收盘时推进结构。"""
        coin = data.get("s", "")
        if not coin:
            return []
        t = int(data.get("t", 0))
        prev_open = self._last_open.get(coin)
        events: list[StructureEvent] = []
        if prev_open is not None and t != prev_open:
            closed = self._prev.get(coin)
            if closed is not None:
                candle = candle_from_ws(closed)
                if self.on_closed:
                    self.on_closed(coin, candle)        # 先驱动 ZoneEngine 等
                events = self._struct(coin).update(candle)
                if self.on_event:
                    for e in events:
                        self.on_event(coin, e)
        self._last_open[coin] = t
        self._prev[coin] = data
        return events

    def structure(self, coin: str) -> MarketStructure | None:
        return self._ms.get(coin)
