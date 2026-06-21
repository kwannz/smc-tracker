"""SMC 市场结构引擎：增量识别摆动高低点 + BOS + CHoCH。

纯计算，无网络、无外部状态。逐根 K 线喂入，输出结构事件。

术语：
- swing high / swing low：分形高/低点（中心 K 线高/低于左右各 lookback 根）。
- BOS（Break of Structure）：顺势突破结构，趋势延续。
- CHoCH（Change of Character）：逆势突破结构，趋势反转的首个信号。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from smc_tracker.models import Candle


@dataclass(slots=True)
class Swing:
    """已确认的摆动点。

    index      —— 摆动点中心 K 线在喂入序列中的索引（从 0 起）。
    price      —— 摆动点价格（high 用最高价 h，low 用最低价 l）。
    kind       —— 'high' 或 'low'。
    time_ms    —— 该中心 K 线的收盘时间戳（close_time_ms）。
    """
    index: int
    price: float
    kind: str
    time_ms: int


@dataclass(slots=True)
class StructureEvent:
    """结构突破事件（BOS 或 CHoCH）。

    type        —— 'BOS' 或 'CHoCH'。
    direction   —— 'bull'（向上突破）或 'bear'（向下跌破）。
    level       —— 被突破的参考价（被破 swing 的 price）。
    swing_index —— 被破 swing 的中心 K 线索引。
    break_index —— 产生突破的当根 K 线索引。
    time_ms     —— 突破当根 K 线的收盘时间戳。
    """
    type: str
    direction: str
    level: float
    swing_index: int
    break_index: int
    time_ms: int


@dataclass(slots=True)
class MarketStructure:
    """增量市场结构引擎。

    用法：每收到一根已收盘 K 线调用一次 update(candle)，返回该根触发的事件列表
    （通常 0 或 1 个；同根理论上可先破 high 再破 low，故用列表）。

    参数 lookback：分形参数（默认 3）。索引 c 处为 swing high ⟺ h[c] > h[j] 对窗口
    [c-lb, c+lb] 内所有 j≠c 成立；swing low 用 l[c] < l[j]。swing 在收到第 c+lb 根后
    才确认（滞后 lb 根）。
    """
    lookback: int = 3

    # —— 内部状态 ——
    # 仅保留尾窗(滑动)：swing 判定只需 [i-2lb, i]，故价序可定期裁剪 + base 偏移校正，
    # 避免 24/7 流式下 _highs/_lows/_times 无界增长。close 不参与逻辑(突破用 candle.c)，故不缓存。
    _highs: list[float] = field(default_factory=list, repr=False)
    _lows: list[float] = field(default_factory=list, repr=False)
    _times: list[int] = field(default_factory=list, repr=False)
    _i: int = -1   # 当前已喂入 K 线的最大绝对索引（从 0 起，永不重置）
    _base: int = 0  # 已从价序头部裁剪的根数(绝对索引 → 数组下标 = idx - _base)

    swings: list[Swing] = field(default_factory=list)
    trend: str | None = None
    ref_high: Swing | None = None
    ref_low: Swing | None = None

    _KEEP = 256        # 裁剪后保留的尾窗根数(远大于 2*lb+1)
    _MAX = 512         # 触发裁剪的长度阈值
    _SWINGS_MAX = 500  # swings 列表上限(仅保留最近)

    def update(self, candle: Candle) -> list[StructureEvent]:
        """喂入一根 K 线，返回本根触发的结构事件列表。"""
        self._highs.append(candle.h)
        self._lows.append(candle.l)
        self._times.append(candle.close_time_ms)
        self._i += 1
        i = self._i
        lb = self.lookback
        b = self._base

        # 1) 确认中心索引 c=i-lb 处的 swing（滞后 lb 根，作用于已收盘 K 线）。
        c = i - lb
        if c >= 0:
            if self._is_swing_high(c):
                sw = Swing(index=c, price=self._highs[c - b], kind="high",
                           time_ms=self._times[c - b])
                self.swings.append(sw)
                self.ref_high = sw
            if self._is_swing_low(c):
                sw = Swing(index=c, price=self._lows[c - b], kind="low",
                           time_ms=self._times[c - b])
                self.swings.append(sw)
                self.ref_low = sw

        # 2) 用当根 close 判突破。
        events: list[StructureEvent] = []
        close = candle.c

        # 2a) 向上突破最近未破 swing high。
        if self.ref_high is not None and close > self.ref_high.price:
            ev_type = "CHoCH" if self.trend == "bear" else "BOS"
            events.append(StructureEvent(
                type=ev_type, direction="bull", level=self.ref_high.price,
                swing_index=self.ref_high.index, break_index=i,
                time_ms=candle.close_time_ms,
            ))
            self.trend = "bull"
            self.ref_high = None  # 消费掉，直到新 swing high 形成

        # 2b) 向下跌破最近未破 swing low。
        if self.ref_low is not None and close < self.ref_low.price:
            ev_type = "CHoCH" if self.trend == "bull" else "BOS"
            events.append(StructureEvent(
                type=ev_type, direction="bear", level=self.ref_low.price,
                swing_index=self.ref_low.index, break_index=i,
                time_ms=candle.close_time_ms,
            ))
            self.trend = "bear"
            self.ref_low = None

        # 3) 裁剪尾窗 + 限制 swings，防长跑无界增长（绝对索引经 _base 校正不受影响）。
        if len(self._highs) > self._MAX:
            k = len(self._highs) - self._KEEP
            del self._highs[:k]
            del self._lows[:k]
            del self._times[:k]
            self._base += k
        if len(self.swings) > self._SWINGS_MAX:
            del self.swings[:len(self.swings) - self._SWINGS_MAX]

        return events

    def _is_swing_high(self, c: int) -> bool:
        """索引 c 是否为 swing high：h[c] 严格大于窗口 [c-lb, c+lb] 内其余所有 high。"""
        lb = self.lookback
        if c - lb < self._base or c + lb > self._i:   # 下界为已保留尾窗的物理边界
            return False
        b = self._base
        hc = self._highs[c - b]
        for j in range(c - lb, c + lb + 1):
            if j == c:
                continue
            if not (hc > self._highs[j - b]):
                return False
        return True

    def _is_swing_low(self, c: int) -> bool:
        """索引 c 是否为 swing low：l[c] 严格小于窗口 [c-lb, c+lb] 内其余所有 low。"""
        lb = self.lookback
        if c - lb < self._base or c + lb > self._i:
            return False
        b = self._base
        lc = self._lows[c - b]
        for j in range(c - lb, c + lb + 1):
            if j == c:
                continue
            if not (lc < self._lows[j - b]):
                return False
        return True


def analyze(candles: list[Candle], lookback: int = 3) -> list[StructureEvent]:
    """便捷函数：对一批 K 线顺序运行引擎，汇总所有结构事件。"""
    ms = MarketStructure(lookback=lookback)
    out: list[StructureEvent] = []
    for c in candles:
        out.extend(ms.update(c))
    return out
