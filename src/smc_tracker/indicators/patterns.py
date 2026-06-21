"""经典图表形态：双顶/双底 + 道氏理论趋势。

基于分形摆动点(swing highs/lows)：中心 K 线高/低于左右各 lookback 根。
注意摆动点需要右侧 lookback 根确认，故识别相对实时滞后 lookback 根。
"""
from __future__ import annotations

from typing import Any


def swing_highs(candles: list[Any], lookback: int = 3) -> list[tuple[int, float]]:
    """分形摆动高点：中心高严格高于左右各 lookback 根。返回 [(下标, 价格)]（按下标升序）。"""
    out: list[tuple[int, float]] = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        hi = candles[i].h
        if all(hi > candles[j].h for j in range(i - lookback, i + lookback + 1) if j != i):
            out.append((i, hi))
    return out


def swing_lows(candles: list[Any], lookback: int = 3) -> list[tuple[int, float]]:
    """分形摆动低点：中心低严格低于左右各 lookback 根。返回 [(下标, 价格)]（按下标升序）。"""
    out: list[tuple[int, float]] = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        lo = candles[i].l
        if all(lo < candles[j].l for j in range(i - lookback, i + lookback + 1) if j != i):
            out.append((i, lo))
    return out


def detect_double_top(candles: list[Any], lookback: int = 3, tol_pct: float = 0.01
                      ) -> dict | None:
    """双顶形态：相邻两个摆动高点价位接近 + 中间有明显回落谷(neckline)。

    返回 {peak1, peak2, neckline, target}，无则 None。
    - peak1/peak2: (下标, 价格)
    - neckline: 两峰之间最低的摆动低点价格(颈线)；若无摆动低点则取区间最低 low
    - target: neckline - (peak - neckline)（向下测幅，peak 取两峰均值）
    """
    highs = swing_highs(candles, lookback)
    if len(highs) < 2:
        return None
    lows = swing_lows(candles, lookback)
    # 从最近的相邻两高往前找第一组满足条件的（优先最近）
    for k in range(len(highs) - 1, 0, -1):
        i1, p1 = highs[k - 1]
        i2, p2 = highs[k]
        peak = max(p1, p2)
        # 两峰价位接近（用 abs 缩放，兼容非正价合成数据）
        if abs(p1 - p2) > tol_pct * abs(peak):
            continue
        # 两峰之间的回落谷：优先用区间内摆动低点，否则用区间最低 low
        mids = [lo for idx, lo in lows if i1 < idx < i2]
        if mids:
            neckline = min(mids)
        else:
            seg = [candles[j].l for j in range(i1 + 1, i2)]
            if not seg:
                continue
            neckline = min(seg)
        # 颈线需明显低于峰（构成有效回落谷）
        if neckline >= min(p1, p2):
            continue
        target = neckline - (peak - neckline)
        return {"peak1": (i1, p1), "peak2": (i2, p2),
                "neckline": neckline, "target": target}
    return None


def detect_double_bottom(candles: list[Any], lookback: int = 3, tol_pct: float = 0.01
                         ) -> dict | None:
    """双底形态：相邻两个摆动低点价位接近 + 中间有明显反弹峰(neckline)。

    返回 {bottom1, bottom2, neckline, target}，无则 None。
    - bottom1/bottom2: (下标, 价格)
    - neckline: 两底之间最高的摆动高点价格(颈线)；若无摆动高点则取区间最高 high
    - target: neckline + (neckline - trough)（向上测幅，trough 取两底均值）
    """
    lows = swing_lows(candles, lookback)
    if len(lows) < 2:
        return None
    highs = swing_highs(candles, lookback)
    for k in range(len(lows) - 1, 0, -1):
        i1, p1 = lows[k - 1]
        i2, p2 = lows[k]
        trough = min(p1, p2)
        # 两底价位接近（用 abs 缩放，兼容非正价合成数据）
        if abs(p1 - p2) > tol_pct * abs(trough):
            continue
        mids = [hi for idx, hi in highs if i1 < idx < i2]
        if mids:
            neckline = max(mids)
        else:
            seg = [candles[j].h for j in range(i1 + 1, i2)]
            if not seg:
                continue
            neckline = max(seg)
        if neckline <= max(p1, p2):
            continue
        target = neckline + (neckline - trough)
        return {"bottom1": (i1, p1), "bottom2": (i2, p2),
                "neckline": neckline, "target": target}
    return None


def dow_trend(candles: list[Any], lookback: int = 3) -> dict:
    """道氏理论趋势：用摆动高低点序列判趋势。

    - 最近两个摆动高更高 且 最近两个摆动低更高 → uptrend(更高高、更高低)
    - 最近两个摆动高更低 且 最近两个摆动低更低 → downtrend
    - 其它 → range
    返回 {trend, last_high, last_low}（last_high/last_low 为最近摆动点价格，可能为 None）。
    """
    highs = swing_highs(candles, lookback)
    lows = swing_lows(candles, lookback)
    last_high = highs[-1][1] if highs else None
    last_low = lows[-1][1] if lows else None

    trend = "range"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]   # higher high
        hl = lows[-1][1] > lows[-2][1]     # higher low
        lh = highs[-1][1] < highs[-2][1]   # lower high
        ll = lows[-1][1] < lows[-2][1]     # lower low
        if hh and hl:
            trend = "uptrend"
        elif lh and ll:
            trend = "downtrend"
    return {"trend": trend, "last_high": last_high, "last_low": last_low}
