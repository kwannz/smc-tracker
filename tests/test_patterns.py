"""经典图表形态单测：双顶/双底 + 道氏理论趋势（合成数据，无网络）。

注意：摆动点(lookback=3)需中心 K 线高/低于左右各 3 根，
故每个摆动点两侧都需留出 ≥3 根填充 K 线作为确认。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle
from smc_tracker.indicators.patterns import (
    swing_highs, swing_lows, detect_double_top, detect_double_bottom, dow_trend,
)


def _c(i: int, h: float, l: float) -> Candle:
    """造一根 K 线，只关心高低点（o/c 取区间中点，不影响摆动判定）。"""
    mid = (h + l) / 2
    return Candle(coin="X", interval="1m", open_time_ms=i * 60000,
                  close_time_ms=i * 60000 + 59999, o=mid, h=h, l=l, c=mid, v=1, n=0)


def _build(highs_lows: list[tuple[float, float]]) -> list[Candle]:
    """(h, l) 序列 → Candle 列表。"""
    return [_c(i, h, l) for i, (h, l) in enumerate(highs_lows)]


# 各根 K 线的 (high, low)。基线高 1.0、低 0.0，凸起/凹陷处单独抬高/压低。
# lookback=3 → 需左右各 3 根更低(对高点)/更高(对低点)填充。


def test_swing_detection_lag():
    # 单个孤立高峰在 idx=4，两侧各 4 根基线
    bars = _build([(1, 0)] * 4 + [(5, 0)] + [(1, 0)] * 4)
    hs = swing_highs(bars, lookback=3)
    assert hs == [(4, 5.0)]
    # 单个孤立低谷在 idx=4
    bars2 = _build([(1, 0)] * 4 + [(1, -5)] + [(1, 0)] * 4)
    ls = swing_lows(bars2, lookback=3)
    assert ls == [(4, -5.0)]


def test_double_top():
    # 两个近似等高的峰(10.0 与 10.05)，中间回落谷(idx8 low=5.0)。
    # 关键：谷的 low 必须严格低于左右填充根的 low，且填充根 high 低于峰，
    # 这样谷 idx8 才会被确认为摆动低点(neckline)，峰 idx4/12 被确认为摆动高点。
    # 填充根用 (high=8, low=8)；峰 (high=10, low=9)；谷 (high=8, low=5)。
    bars = _build(
        [(8, 8)] * 4                      # 0..3 填充
        + [(10.0, 9)]                     # 4  峰1
        + [(8, 8)] * 3                    # 5..7 填充
        + [(8, 5.0)]                      # 8  回落谷(neckline) low=5.0
        + [(8, 8)] * 3                    # 9..11 填充
        + [(10.05, 9)]                    # 12 峰2 ≈峰1
        + [(8, 8)] * 4                    # 13..16 填充
    )
    assert [i for i, _ in swing_highs(bars, lookback=3)] == [4, 12]
    assert [i for i, _ in swing_lows(bars, lookback=3)] == [8]   # 谷被确认
    dt = detect_double_top(bars, lookback=3, tol_pct=0.01)
    assert dt is not None
    assert dt["peak1"] == (4, 10.0)
    assert dt["peak2"] == (12, 10.05)
    assert dt["neckline"] == 5.0
    # target = neckline - (peak - neckline)，peak=max(10.0,10.05)=10.05
    assert dt["target"] == 5.0 - (10.05 - 5.0)


def test_double_top_rejected_when_peaks_far():
    # 两峰差距过大(10 与 8)，超 tol → None
    bars = _build(
        [(8, 8)] * 4 + [(10.0, 9)] + [(8, 8)] * 3 + [(8, 5.0)]
        + [(8, 8)] * 3 + [(8.0, 7)] + [(8, 8)] * 4
    )
    assert detect_double_top(bars, lookback=3, tol_pct=0.01) is None


def test_double_bottom():
    # 镜像：两个近似等低的底(idx4/12)，中间反弹峰(idx8 high=5.0)。
    # 填充根 (high=-8, low=-8)；底 (high=-9, low=-10)；峰 (high=5, low=-8)。
    bars = _build(
        [(-8, -8)] * 4                    # 0..3 填充
        + [(-9, -10.0)]                   # 4  底1 low=-10.0
        + [(-8, -8)] * 3                  # 5..7 填充
        + [(5.0, -8)]                     # 8  反弹峰(neckline) high=5.0
        + [(-8, -8)] * 3                  # 9..11 填充
        + [(-9, -10.05)]                  # 12 底2 ≈底1
        + [(-8, -8)] * 4                  # 13..16 填充
    )
    assert [i for i, _ in swing_lows(bars, lookback=3)] == [4, 12]
    assert [i for i, _ in swing_highs(bars, lookback=3)] == [8]   # 峰被确认
    db = detect_double_bottom(bars, lookback=3, tol_pct=0.01)
    assert db is not None
    assert db["bottom1"] == (4, -10.0)
    assert db["bottom2"] == (12, -10.05)
    assert db["neckline"] == 5.0
    # target = neckline + (neckline - trough)，trough=min(-10.0,-10.05)=-10.05
    assert db["target"] == 5.0 + (5.0 - (-10.05))


def test_dow_uptrend():
    # 更高高 + 更高低：两个递增峰 + 两个递增谷交替排布。
    # 顺序: 谷低1 -> 峰高1 -> 谷低2(更高) -> 峰高2(更高)
    bars = _build(
        [(2, 1)] * 3                      # 0..2 填充基线
        + [(2, -5.0)]                     # 3  低1 = -5
        + [(2, 1)] * 3                    # 4..6
        + [(8.0, 1)]                      # 7  高1 = 8
        + [(2, 1)] * 3                    # 8..10
        + [(2, -3.0)]                     # 11 低2 = -3 (> 低1，更高低)
        + [(2, 1)] * 3                    # 12..14
        + [(10.0, 1)]                     # 15 高2 = 10 (> 高1，更高高)
        + [(2, 1)] * 3                    # 16..18 确认
    )
    hs = swing_highs(bars, lookback=3)
    ls = swing_lows(bars, lookback=3)
    assert [p for _, p in hs] == [8.0, 10.0]
    assert [p for _, p in ls] == [-5.0, -3.0]
    res = dow_trend(bars, lookback=3)
    assert res["trend"] == "uptrend"
    assert res["last_high"] == 10.0
    assert res["last_low"] == -3.0


def test_dow_downtrend():
    # 更低高 + 更低低
    bars = _build(
        [(2, 1)] * 3
        + [(2, -3.0)]                     # 低1 = -3
        + [(2, 1)] * 3
        + [(10.0, 1)]                     # 高1 = 10
        + [(2, 1)] * 3
        + [(2, -5.0)]                     # 低2 = -5 (< 低1，更低低)
        + [(2, 1)] * 3
        + [(8.0, 1)]                      # 高2 = 8 (< 高1，更低高)
        + [(2, 1)] * 3
    )
    res = dow_trend(bars, lookback=3)
    assert res["trend"] == "downtrend"
    assert res["last_high"] == 8.0
    assert res["last_low"] == -5.0


def test_dow_range():
    # 高更高但低更低（无明确方向）→ range
    bars = _build(
        [(2, 1)] * 3
        + [(2, -3.0)]                     # 低1
        + [(2, 1)] * 3
        + [(8.0, 1)]                      # 高1
        + [(2, 1)] * 3
        + [(2, -5.0)]                     # 低2 更低
        + [(2, 1)] * 3
        + [(10.0, 1)]                     # 高2 更高 → 矛盾 → range
        + [(2, 1)] * 3
    )
    res = dow_trend(bars, lookback=3)
    assert res["trend"] == "range"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
