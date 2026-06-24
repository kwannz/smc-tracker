"""funding 极值反转信号单测（确定性，纯函数）。

语义：funding 进入历史极值=多空拥挤→预示反转。
返回 ∈[-1,1]，正=看涨反转（funding 极低=空头拥挤→反弹）；负=看跌反转（funding 极高=多头拥挤→回落）。
极值判定：
  - method="quantile"（默认，C.4）：滚动窗口经验分位，无分布假设，稳健。
  - method="zscore"（向后兼容）：旧全历史 z-score 路径（回归保护）。
常量历史无法定义极值→返 0（不臆测）。
按 profile.has_funding 门控由上层负责。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals.funding_extreme import funding_extreme_signal


def _varied_hist() -> list[float]:
    """有波动的历史 funding（均值≈0.0001，std>0）。"""
    return [0.0001 + 0.00002 * ((i % 7) - 3) for i in range(30)]


# ──────────────────── 旧测试：method="quantile"（默认） ────────────────────

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
    assert abs(sig) < 0.3  # 经验分位的中间区域也比较平坦


def test_flat_history_zero_std_returns_zero():
    """常量历史 → 无法定义极值 → 返 0（不臆测），即使 funding_now 偏离。"""
    assert funding_extreme_signal(0.01, [0.0001] * 30, min_samples=20) == 0.0


def test_clamped_to_unit_range():
    """极端偏离 → 幅度封顶 1.0。"""
    sig = funding_extreme_signal(1.0, _varied_hist(), min_samples=20)
    assert -1.0 <= sig <= 1.0


# ──────────────────── C.4 新增：经验分位稳健性 ────────────────────

def _fat_tail_hist() -> list[float]:
    """厚尾历史：大量接近 0 的数据 + 少量极值（funding 厚尾典型形态）。"""
    # 25 个接近 0，5 个极值
    return [0.00005] * 25 + [0.002, 0.003, 0.005, 0.008, 0.010]


def test_quantile_robust_to_fat_tail():
    """经验分位法对厚尾历史中的极高 funding 给强负信号（multi-head 拥挤告警）。

    |sig_quantile| 应 ≥ |sig_zscore|（稳健性断言：分位不假设高斯）。
    """
    hist = _fat_tail_hist()
    funding_extreme_high = 0.009  # 位于历史 90th+ 百分位

    sig_q = funding_extreme_signal(funding_extreme_high, hist, min_samples=20,
                                   method="quantile")
    sig_z = funding_extreme_signal(funding_extreme_high, hist, min_samples=20,
                                   method="zscore")
    # 两者均应为负（高 funding 看跌）
    assert sig_q < 0.0, f"sig_q={sig_q} 应为负"
    assert sig_z < 0.0, f"sig_z={sig_z} 应为负"
    # 经验分位对极值应更敏感（大多数样本近 0 → 0.009 处于极高分位）
    assert abs(sig_q) >= abs(sig_z) * 0.8, (
        f"经验分位应与 z-score 量级相当或更强: |q|={abs(sig_q):.3f} |z|={abs(sig_z):.3f}"
    )


def test_zscore_method_matches_old_implementation():
    """method='zscore' 回退路径数值 == 旧实现（回归保护）。

    旧实现：全样本 mean/std → -tanh(z/2)
    新实现 method='zscore'：同样逻辑（window 截断后仍用 zscore）。
    用 window > len(hist) 使截断无效，验证 zscore 数值一致。
    """
    import math
    hist = _varied_hist()
    n = len(hist)
    mean = sum(hist) / n
    var = sum((x - mean) ** 2 for x in hist) / n
    std = math.sqrt(var)
    funding_now = 0.01

    # 旧实现手算
    z = (funding_now - mean) / std
    expected = max(-1.0, min(1.0, -math.tanh(z / 2.0)))

    # method="zscore"，window > len → 不截断
    got = funding_extreme_signal(funding_now, hist, min_samples=20,
                                 window=len(hist) + 10, method="zscore")
    assert abs(got - expected) < 1e-9, f"zscore 回归: got={got}, expected={expected}"


def test_window_truncation_ignores_ancient_extreme():
    """window 截断：远古极值注入到历史开头，不应触发当前信号。

    hist = [极高极值 × 10] + [正常值 × 30]，window=30
    funding_now = 正常值范围内 → window=30 截取近端 → 正常值构成分布 → sig≈0
    """
    ancient_extremes = [1.0] * 10         # 远古极高 funding
    normal_hist = [0.0001 + 0.00002 * ((i % 7) - 3) for i in range(30)]
    hist = ancient_extremes + normal_hist

    # 当前 funding = 历史均值 → 应接近 0 信号
    funding_now = 0.0001
    sig = funding_extreme_signal(funding_now, hist, min_samples=20,
                                 window=30, method="quantile")
    assert abs(sig) < 0.5, (
        f"远古极值不应影响当前信号（window=30），sig={sig:.3f}"
    )


def test_constant_history_within_window_returns_zero():
    """window 内全相等 → 常量分位退化 → sig=0。"""
    hist = [0.0001] * 30
    sig = funding_extreme_signal(0.01, hist, min_samples=20, method="quantile")
    assert sig == 0.0, f"常量历史应返回 0，got {sig}"


def test_harmonic_forward_compat_no_new_kw():
    """harmonic_forward 旧调用（无 window/method kw）签名兼容 smoke test。"""
    hist = _varied_hist()
    # 旧调用方只传 positional + min_samples
    sig = funding_extreme_signal(0.01, hist, min_samples=20)
    assert isinstance(sig, float) and -1.0 <= sig <= 1.0
