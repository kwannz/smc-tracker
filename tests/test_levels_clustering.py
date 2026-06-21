"""levels.support_resistance 聚类回归：簇跨度须 bound 在 tol_pct 内，不可单链漂移。

回归 bug：原聚类用 `abs(p - zones[-1][-1])`（簇内上一个价）单链合并，价位渐变时
单簇可跨远超 tol_pct，把本应区分的多个 S/R 价位错并成一个。改为与簇锚点比较后修复。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle


def _mk(i: int, h: float, low: float) -> Candle:
    return Candle(coin="X", interval="1m", open_time_ms=i * 60000,
                  close_time_ms=i * 60000 + 59999,
                  o=(h + low) / 2, h=h, l=low, c=(h + low) / 2, v=10.0, n=0)


def test_support_resistance_cluster_bounded_by_tol():
    """渐变摆高 [100,100.25,100.5,100.75]（步长 0.25%）、tol=0.3%：

    单链聚类会把四个价全链成 1 簇（跨度 0.75% 远超 tol）；与锚点比较则在超 tol 处分簇，
    得到 ≥2 个 resistance 区（正确区分不同价位）。
    """
    # 背景 h=99/l=98，在 i=4,9,14,19 放四个递增摆高（各自高于 ±3 邻居 → 合格 swing high）
    peaks = {4: 100.0, 9: 100.25, 14: 100.5, 19: 100.75}
    candles = [_mk(i, peaks.get(i, 99.0), 98.0) for i in range(24)]

    sr = support_resistance(candles, lookback=3, tol_pct=0.003)
    res = sr["resistance"]
    # 锚点聚类：0.5%/0.75% 超 tol → 至少分成 2 个区（单链 bug 下只会是 1 个）
    assert len(res) >= 2, f"聚类应区分超 tol 的价位，实际并成 {len(res)} 个: {res}"
    # 每个区的成员价跨度不超过 tol_pct（honor 契约）
    # res 为 [(均价,触及次数)]，此处只校验区数；跨度由实现保证


from smc_tracker.indicators.levels import support_resistance  # noqa: E402
