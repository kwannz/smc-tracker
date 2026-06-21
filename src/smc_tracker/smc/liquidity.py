"""SMC 流动性：等高/等低 + buy-side/sell-side liquidity + 流动性扫荡(stop hunt)。

聪明钱常在「流动性池」处猎杀止损：
- BSL (buy-side liquidity)：摆动高点上方（买入止损/突破单聚集）。
- SSL (sell-side liquidity)：摆动低点下方（卖出止损聚集）。
- 等高/等低 (EQH/EQL)：多个摆动点价位接近 → 流动性更密集。
- 扫荡 (sweep / stop hunt)：价格用上影线刺破 BSL 后**收回其下**（或下影线刺破 SSL 后收回其上）→
  流动性被夺取且失败 → 常预示反转。区别于「突破(BOS)」：突破是收在外侧（接受），扫荡是收回内侧（拒绝）。

纯计算、增量、可测。内部复用与 MarketStructure 相同的分形摆动点逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Candle


@dataclass(slots=True)
class Liquidity:
    side: str          # 'BSL'(上方) / 'SSL'(下方)
    price: float
    index: int         # 形成该流动性的摆动点 K 线索引
    time_ms: int
    equal: bool = False        # 是否等高/等低（≥2 个摆动点聚集）
    swept: bool = False
    swept_at: int = -1


@dataclass(slots=True)
class SweepEvent:
    direction: str     # 'bullish'(扫 SSL 后反弹) / 'bearish'(扫 BSL 后回落)
    price: float       # 被扫的流动性价位
    index: int         # 扫荡发生的 K 线索引
    time_ms: int
    equal: bool        # 被扫的是否为等高/等低（更强）


@dataclass(slots=True)
class LiquidityEngine:
    lookback: int = 2
    eq_tol_pct: float = 0.0015        # 等高/等低价位容差
    sweep_min_pct: float = 0.0        # 刺破最小幅度（过滤噪声）

    _h: list[float] = field(default_factory=list, repr=False)
    _l: list[float] = field(default_factory=list, repr=False)
    _c: list[float] = field(default_factory=list, repr=False)
    _t: list[int] = field(default_factory=list, repr=False)
    _i: int = -1
    _base: int = 0     # 已裁剪的价序头部根数(绝对索引 → 数组下标 = idx - _base)
    bsl: list[Liquidity] = field(default_factory=list)
    ssl: list[Liquidity] = field(default_factory=list)

    def update(self, candle: Candle) -> list[SweepEvent]:
        self._h.append(candle.h)
        self._l.append(candle.l)
        self._c.append(candle.c)
        self._t.append(candle.close_time_ms)
        self._i += 1
        i = self._i
        lb = self.lookback
        b = self._base

        # 1) 确认 i-lb 处的摆动点 → 建/合并流动性位。
        c = i - lb
        if c - lb >= b:
            if self._is_swing_high(c):
                self._add_liquidity(self.bsl, self._h[c - b], c)
            if self._is_swing_low(c):
                self._add_liquidity(self.ssl, self._l[c - b], c)

        # 2) 扫荡检测（当根刺破未被扫的流动性并收回内侧）。
        events: list[SweepEvent] = []
        hi, lo, close = self._h[i - b], self._l[i - b], self._c[i - b]
        for lv in self.bsl:
            if lv.swept or lv.index >= i:
                continue
            if hi > lv.price * (1 + self.sweep_min_pct) and close < lv.price:
                lv.swept = True
                lv.swept_at = i
                events.append(SweepEvent("bearish", lv.price, i, self._t[i - b], lv.equal))
        for lv in self.ssl:
            if lv.swept or lv.index >= i:
                continue
            if lo < lv.price * (1 - self.sweep_min_pct) and close > lv.price:
                lv.swept = True
                lv.swept_at = i
                events.append(SweepEvent("bullish", lv.price, i, self._t[i - b], lv.equal))

        # 3) 裁剪价序尾窗 + 修剪陈旧已扫流动性，防 24/7 无界增长与 O(bars²) 遍历。
        if len(self._h) > 2000:
            k = len(self._h) - 1000
            for arr in (self._h, self._l, self._c, self._t):
                del arr[:k]
            self._base += k
        if len(self.bsl) + len(self.ssl) > 4000:
            cutoff = i - 1000
            self.bsl = [lv for lv in self.bsl if not lv.swept or lv.index > cutoff]
            self.ssl = [lv for lv in self.ssl if not lv.swept or lv.index > cutoff]
        return events

    def _add_liquidity(self, pool: list[Liquidity], price: float, idx: int) -> None:
        """新增流动性位；若与既有未被扫的同侧位价位接近 → 标记等高/等低。"""
        for lv in pool:
            if not lv.swept and abs(lv.price - price) <= self.eq_tol_pct * lv.price:
                lv.equal = True
                lv.index = idx                 # 更新到最近一次
                lv.price = price               # 同步价位到最近摆动点(否则扫荡用陈旧 price)
                lv.time_ms = self._t[idx - self._base]
                return
        side = "BSL" if pool is self.bsl else "SSL"
        pool.append(Liquidity(side=side, price=price, index=idx,
                              time_ms=self._t[idx - self._base]))

    def _is_swing_high(self, c: int) -> bool:
        lb = self.lookback
        if c - lb < self._base or c + lb > self._i:
            return False
        b = self._base
        hc = self._h[c - b]
        return all(hc > self._h[j - b] for j in range(c - lb, c + lb + 1) if j != c)

    def _is_swing_low(self, c: int) -> bool:
        lb = self.lookback
        if c - lb < self._base or c + lb > self._i:
            return False
        b = self._base
        lc = self._l[c - b]
        return all(lc < self._l[j - b] for j in range(c - lb, c + lb + 1) if j != c)

    def unswept(self, side: str | None = None) -> list[Liquidity]:
        pools = (self.bsl if side == "BSL" else self.ssl if side == "SSL"
                 else [*self.bsl, *self.ssl])
        return [lv for lv in pools if not lv.swept]
