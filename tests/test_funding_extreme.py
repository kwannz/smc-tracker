"""funding 极值反转信号单测（确定性，纯函数）。

语义：funding 进入历史极值=多空拥挤→预示反转。
返回 ∈[-1,1]，正=看涨反转（funding 极低=空头拥挤→反弹）；负=看跌反转（funding 极高=多头拥挤→回落）。
极值判定基于历史波动（z-score）：常量历史（std=0）无法定义极值→返 0（不臆测）。
按 profile.has_funding 门控由上层负责。
"""
from __future__ import annotations

from smc_tracker.signals.funding_extreme import funding_extreme_signal


def _varied_hist() -> list[float]:
    """有波动的历史 funding（均值≈0.0001，std>0）。"""
    return [0.0001 + 0.00002 * ((i % 7) - 3) for i in range(30)]


def test_insufficient_history_returns_zero():
    """样本不足 → 0.0（不臆测）。"""
    assert funding_extreme_signal(0.01, [0.001, 0.002], min_samples=20) == 0.0


def test_crowded_long_gives_bearish_reversal():
    """funding 远高于历史（多头拥挤）→ 负（看跌反转）。"""
    sig = funding_extreme_signal(0.01, _varied_hist(), min_samples=20)
    assert sig < 0.0


def test_crowded_short_gives_bullish_reversal():
    """funding 远低于历史（空头拥挤，负 funding）→ 正（看涨反转）。"""
    sig = funding_extreme_signal(-0.01, _varied_hist(), min_samples=20)
    assert sig > 0.0


def test_near_mean_is_neutral():
    """funding 接近历史均值 → 近 0。"""
    sig = funding_extreme_signal(0.0001, _varied_hist(), min_samples=20)
    assert abs(sig) < 0.1


def test_flat_history_zero_std_returns_zero():
    """历史无波动（std=0）→ 无法定义极值 → 返 0（不臆测），即使 funding_now 偏离。"""
    assert funding_extreme_signal(0.01, [0.0001] * 30, min_samples=20) == 0.0


def test_clamped_to_unit_range():
    """极端偏离 → 幅度封顶 1.0。"""
    sig = funding_extreme_signal(1.0, _varied_hist(), min_samples=20)
    assert -1.0 <= sig <= 1.0
