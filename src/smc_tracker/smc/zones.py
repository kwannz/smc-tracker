"""SMC 区域识别：FVG（公允价值缺口）+ Order Block（订单块）+ 回补检测 + 溢价折价区。

纯计算、增量、可测。逐根已收盘 K 线喂入。

- FVG（Fair Value Gap / 失衡）：三根 K 线 (a=i-2, b=i-1, c=i)，b 为位移根。
    看涨 FVG：low[c] > high[a]  → 缺口区 [high[a], low[c]]（价格回落到此区常获支撑）。
    看跌 FVG：high[c] < low[a]  → 缺口区 [high[c], low[a]]。
- Order Block（订单块）：位移（形成 FVG）前最后一根反向 K 线。
    看涨 OB：看涨位移前最后一根阴线（c<o），区 [low, high]。
    看跌 OB：看跌位移前最后一根阳线（c>o）。
- 回补（mitigation）：价格随后重新触及区间 → 标记已回补。
- 溢价/折价：相对 dealing range，>50% 为溢价(倾向做空)，<50% 为折价(倾向做多)；
    0.62–0.79 回撤为 OTE 最优入场区。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import Candle


@dataclass(slots=True)
class Zone:
    kind: str          # 'FVG' / 'OB'
    direction: str     # 'bull' / 'bear'
    top: float
    bottom: float
    index: int         # 定义该区的 K 线索引（OB 为 OB 根；FVG 为位移根 b）
    time_ms: int
    created_at: int    # 检测到该区时的当前 K 线索引
    mitigated: bool = False
    mitigated_at: int = -1

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0


from ..util import to_float as _f  # 统一安全数值解析


class ZoneEngine:
    """逐根识别 FVG / OB 并跟踪回补。"""

    # 裁剪参数：价序保留尾窗(OB 回溯通常仅几根，1000 远超所需)；区列表按时效修剪
    _KEEP = 1000
    _MAX = 2000
    _ZONE_KEEP = 1000   # 仅保留近 _ZONE_KEEP 根内形成的区(更老的已失效/不再用作入场)

    def __init__(self, min_gap_pct: float = 0.0) -> None:
        self.min_gap_pct = min_gap_pct
        self._h: list[float] = []
        self._l: list[float] = []
        self._o: list[float] = []
        self._c: list[float] = []
        self._t: list[int] = []
        self._i = -1
        self._base = 0      # 已裁剪的价序头部根数(绝对索引 → 数组下标 = idx - _base)
        self.fvgs: list[Zone] = []
        self.obs: list[Zone] = []

    def update(self, candle: Candle) -> list[Zone]:
        """喂入一根 K 线，返回本根新形成的区（FVG/OB）。"""
        self._h.append(candle.h)
        self._l.append(candle.l)
        self._o.append(candle.o)
        self._c.append(candle.c)
        self._t.append(candle.close_time_ms)
        self._i += 1
        i = self._i
        b = self._base
        new: list[Zone] = []

        # 1) 先用本根更新既有未回补区的回补状态。
        self._update_mitigation(i)

        # 2) FVG（需 ≥3 根）。
        if i >= 2:
            a, c = i - 2, i
            disp = i - 1
            # 看涨 FVG
            if self._l[c - b] > self._h[a - b]:
                gap = self._l[c - b] - self._h[a - b]
                if gap >= self.min_gap_pct * self._h[a - b]:
                    z = Zone("FVG", "bull", top=self._l[c - b], bottom=self._h[a - b],
                             index=disp, time_ms=self._t[disp - b], created_at=i)
                    self.fvgs.append(z)
                    new.append(z)
                    ob = self._find_ob("bull", disp, i)
                    if ob:
                        self.obs.append(ob)
                        new.append(ob)
            # 看跌 FVG
            elif self._h[c - b] < self._l[a - b]:
                gap = self._l[a - b] - self._h[c - b]
                if gap >= self.min_gap_pct * self._l[a - b]:
                    z = Zone("FVG", "bear", top=self._l[a - b], bottom=self._h[c - b],
                             index=disp, time_ms=self._t[disp - b], created_at=i)
                    self.fvgs.append(z)
                    new.append(z)
                    ob = self._find_ob("bear", disp, i)
                    if ob:
                        self.obs.append(ob)
                        new.append(ob)

        # 3) 裁剪价序尾窗 + 修剪陈旧区，防 24/7 流式无界增长与 O(bars²) 遍历。
        if len(self._h) > self._MAX:
            k = len(self._h) - self._KEEP
            for arr in (self._h, self._l, self._o, self._c, self._t):
                del arr[:k]
            self._base += k
        if len(self.fvgs) + len(self.obs) > 4 * self._ZONE_KEEP:
            cutoff = i - self._ZONE_KEEP
            self.fvgs = [z for z in self.fvgs if z.created_at > cutoff]
            self.obs = [z for z in self.obs if z.created_at > cutoff]
        return new

    def _find_ob(self, direction: str, disp_idx: int, created_at: int) -> Zone | None:
        """位移根 disp_idx 之前（含）最后一根反向 K 线作为订单块（限保留尾窗内回溯）。"""
        b = self._base
        for j in range(disp_idx, b - 1, -1):       # 下界 b：不回溯到已裁剪区
            is_bear = self._c[j - b] < self._o[j - b]
            is_bull = self._c[j - b] > self._o[j - b]
            if direction == "bull" and is_bear:
                return Zone("OB", "bull", top=self._h[j - b], bottom=self._l[j - b],
                            index=j, time_ms=self._t[j - b], created_at=created_at)
            if direction == "bear" and is_bull:
                return Zone("OB", "bear", top=self._h[j - b], bottom=self._l[j - b],
                            index=j, time_ms=self._t[j - b], created_at=created_at)
        return None

    def _update_mitigation(self, i: int) -> None:
        """本根 i 是否触及既有未回补区。看涨区被回落触及、看跌区被反弹触及即回补。"""
        b = self._base
        lo, hi = self._l[i - b], self._h[i - b]
        for z in (*self.fvgs, *self.obs):
            if z.mitigated or z.created_at >= i:
                continue
            if z.direction == "bull" and lo <= z.top:
                z.mitigated = True
                z.mitigated_at = i
            elif z.direction == "bear" and hi >= z.bottom:
                z.mitigated = True
                z.mitigated_at = i

    def active_zones(self, direction: str | None = None) -> list[Zone]:
        """未回补的区（可选按方向过滤）。"""
        out = [z for z in (*self.fvgs, *self.obs) if not z.mitigated]
        if direction:
            out = [z for z in out if z.direction == direction]
        return out

    def zone_at(self, price: float, direction: str | None = None) -> Zone | None:
        """价格当前落在哪个未回补区内（返回最近形成的一个）。"""
        cands = [z for z in self.active_zones(direction)
                 if z.bottom <= price <= z.top]
        return max(cands, key=lambda z: z.created_at) if cands else None


def premium_discount(price: float, range_high: float, range_low: float) -> str:
    """价格在 dealing range 中的位置：premium(溢价>50%) / discount(折价<50%) / equilibrium。"""
    if range_high <= range_low:
        return "equilibrium"
    pos = (price - range_low) / (range_high - range_low)
    if pos > 0.55:
        return "premium"
    if pos < 0.45:
        return "discount"
    return "equilibrium"


def in_ote(price: float, range_high: float, range_low: float, direction: str) -> bool:
    """是否处于 OTE 最优入场区（0.62–0.79 回撤）。
    direction='bull'：从低到高的回撤；'bear'：从高到低的回撤。"""
    if range_high <= range_low:
        return False
    rng = range_high - range_low
    if direction == "bull":
        lo = range_high - 0.79 * rng
        hi = range_high - 0.62 * rng
        return lo <= price <= hi
    lo = range_low + 0.62 * rng
    hi = range_low + 0.79 * rng
    return lo <= price <= hi
