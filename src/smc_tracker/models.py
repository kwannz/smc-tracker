"""核心数据模型。使用 slots dataclass 降低内存与属性访问开销（低延迟）。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "B"
    SELL = "A"   # Hyperliquid 中 "A"=Ask=卖, "B"=Bid=买

    @classmethod
    def from_hl(cls, s: str) -> "Side":
        return cls.BUY if s == "B" else cls.SELL


@dataclass(slots=True)
class Trade:
    """市场公开成交（trades 频道）。"""
    coin: str
    side: Side
    px: float
    sz: float
    time_ms: int
    recv_ns: int          # 本地接收单调时间，用于延迟统计


@dataclass(slots=True)
class Fill:
    """某地址的成交回报（userFills 频道）。"""
    coin: str
    side: Side
    px: float
    sz: float
    time_ms: int
    start_position: float    # 成交前持仓
    dir: str                 # "Open Long" / "Close Short" 等 Hyperliquid 语义
    closed_pnl: float
    hash: str
    oid: int
    crossed: bool            # 是否吃单（taker）
    address: str = ""
    label: str = ""

    @property
    def notional(self) -> float:
        return self.px * self.sz


@dataclass(slots=True)
class Position:
    """地址在某 coin 的当前持仓（来自 clearinghouseState / webData2）。"""
    coin: str
    szi: float               # 带符号仓位：>0 多, <0 空
    entry_px: float
    position_value: float    # 名义价值 USD
    unrealized_pnl: float
    leverage: float
    liquidation_px: float | None = None

    @property
    def is_long(self) -> bool:
        return self.szi > 0

    @property
    def is_flat(self) -> bool:
        return self.szi == 0


@dataclass(slots=True)
class Candle:
    """K 线（candle 频道 / candleSnapshot）。"""
    coin: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    o: float
    h: float
    l: float
    c: float
    v: float
    n: int                   # 成交笔数

    @property
    def is_bullish(self) -> bool:
        return self.c >= self.o
