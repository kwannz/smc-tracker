"""sfg/atr2.py — SFG ATR2 反转因子 series（KNN 特征用途）。

因子语义（reversal 簇，SFG ContinuousFactors.atr2 字段）：
  factor_atr2 = clamp( -atr2_confirmation / atr2_momentum_volatility )
  atr2_confirmation = SMA(norm_mom, smoothness) * magnify_ob
  norm_mom = mom / rolling_std_pop(mom, smoothness)
  mom[i] = close[i] - close[i - trend_length]

符号约定（spec sign_convention）：
  POSITIVE → bullish 均值回归（oversold，预期反弹）
  NEGATIVE → bearish 均值回归（overbought，预期下跌）
  NaN      → fail-closed 哨兵（warmup / 零波动 / 非有限确认）

Rust 锚定：continuous_factors.rs:306-320 (factor_atr2), :106-112 (clamp)

零波动策略（与 Rust/Pine 字节对齐）：
  vol == 0 → NaN（fail-closed），与 Rust atr2_signals.rs:316-323 一致。
  smc atr2_signals.py 存在 sign(mom)*2.0 分支，该分支专属标量包装，
  **本 series 版本严格遵循 Rust/Pine parity，零波动 → NaN。**

诚实标注（spec lookahead_risk）：
  - atr2 因子是 double-smoothed 信号（trend+smoothness+smoothness），滞后约 smoothness 根
  - 反转读数是当期均值回归偏向，非 t+0 前瞻性预测
  - 输入：仅 close[]；high/low/volume 用于其他列，不影响本因子
  - 短序列（< warmup = trend_length + 2*smoothness - 1）：退化为全 NaN
"""
from __future__ import annotations

from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from smc_tracker.indicators.sfg._common import clamp, ohlcv_arrays


# ─────────────────────────────────────────────────────────────────────────────
# 主函数：atr2_series
# ─────────────────────────────────────────────────────────────────────────────


def atr2_series(
    candles: list[Any],
    *,
    trend_length: int = 8,
    smoothness: int = 20,
    magnify_ob: float = 3.0,
) -> np.ndarray:
    """ATR2 反转因子 series（长度 n，warmup 段填 nan）。

    Args:
        candles:      K 线列表，需有 .c 属性（close 价）。
        trend_length: 动量步长（Pine trendLength，默认 8）。
        smoothness:   滚动 std/SMA 双窗口（Pine smoothness，默认 20）。
        magnify_ob:   放大倍数（Pine magnifyOB，默认 3.0）。

    Returns:
        np.ndarray shape=(n,)，dtype=float64。
        warmup 前（前 trend_length+2*smoothness-2 根）= nan。
        factor ∈ [-1, 1]（有效），nan（fail-closed sentinel）。

    Causal guarantee:
        out[i] 仅用 close[0..=i]，不读未来数据。
        prefix-invariance：截短序列与完整序列同索引输出相同。

    Short-series degeneration:
        若 len(candles) < trend_length + 2*smoothness - 1，返回全 nan 数组。
        此情况正常，不 raise，不 impute 为 0。
    """
    n = len(candles)
    out = np.full(n, np.nan)

    if n == 0:
        return out

    # ── 提取 close（只需 close 路径，见 spec inputs） ─────────────────────────
    arrs = ohlcv_arrays(candles)
    c: np.ndarray = arrs["c"]  # float64, length n

    # ── STEP 1: mom[i] = close[i] - close[i - trend_length] ──────────────────
    # 向量化：mom[trend_length:] = c[trend_length:] - c[:-trend_length]
    # 前 trend_length 根 = nan（因果性：i < trend_length 无法计算 mom）
    mom = np.full(n, np.nan)
    if n > trend_length:
        mom[trend_length:] = c[trend_length:] - c[:n - trend_length]

    # ── STEP 2: rolling_std_pop(mom, smoothness, ddof=0) ─────────────────────
    # 仅对 mom 有效段（从 trend_length 起）做滑窗，避免 nan 污染
    # Rust: ddof=0（总体标准差），sliding_window_view 精确对齐
    volatility = np.full(n, np.nan)
    mom_valid = mom[trend_length:]  # shape: (n - trend_length,)
    if len(mom_valid) >= smoothness:
        # sliding_window_view shape: (n-trend_length-smoothness+1, smoothness)
        windows_std = sliding_window_view(mom_valid, smoothness)
        # ddof=0（总体标准差，与 Rust rolling_std_pop 一致）
        std_vals = np.std(windows_std, axis=1, ddof=0)
        # 填回完整数组，起始 = trend_length + smoothness - 1
        vol_start = trend_length + smoothness - 1
        volatility[vol_start:] = std_vals

    # ── STEP 3: norm_mom = mom / volatility（Rust parity：零波动 → NaN）──────
    # fail-closed：vol == 0 或非有限 → NaN（不同于 smc atr2_signals.py 的±2分支）
    norm_mom = np.full(n, np.nan)
    valid_mask = np.isfinite(volatility) & np.isfinite(mom) & (volatility > 1e-12)
    norm_mom[valid_mask] = mom[valid_mask] / volatility[valid_mask]
    # 零波动（volatility <= 1e-12 且有限且 mom 有限）→ 保持 NaN（Rust/Pine parity）

    # ── STEP 4: confirmation = SMA(norm_mom, smoothness) * magnify_ob ────────
    # SMA：min_periods=smoothness，窗口内任一 NaN → NaN（Rust primitives:114-135）
    # norm_mom 有效段从 vol_start = trend_length + smoothness - 1 起
    confirmation = np.full(n, np.nan)
    vol_start = trend_length + smoothness - 1
    norm_valid = norm_mom[vol_start:]  # shape: (n - vol_start,)
    if len(norm_valid) >= smoothness:
        # sliding_window_view 对 norm_valid 做 SMA
        windows_sma = sliding_window_view(norm_valid, smoothness)  # (m, smoothness)
        # 检查每窗是否全有限（NaN-in-window → NaN out，strict min_periods）
        all_finite_mask = np.all(np.isfinite(windows_sma), axis=1)
        sma_vals = np.where(all_finite_mask, windows_sma.mean(axis=1), np.nan)
        # confirmation = sma * magnify_ob
        conf_vals = sma_vals * magnify_ob
        # 填回完整数组：
        # vol_start 处 norm_mom 开始有效，SMA 再需 smoothness 根 → 0-based 起始：
        # conf_start = vol_start + smoothness - 1
        #            = (trend_length + smoothness - 1) + smoothness - 1
        #            = trend_length + 2*smoothness - 2
        # spec STEP 8 "index >= trend_length+2*smoothness-1" 是 1-based 描述，
        # 即 0-based index = trend_length+2*smoothness-2，与此一致。
        conf_start = vol_start + smoothness - 1
        confirmation[conf_start:] = conf_vals

    # ── STEP 5: momentum_volatility = volatility（逐字复用 STEP 2，仅取有效段）
    # 供 STEP 6 使用，已在 volatility 数组中

    # ── STEP 6+7: factor = clamp(-confirmation / volatility) ─────────────────
    # fail-closed：confirmation 非有限 → NaN；volatility <= 0 / 非有限 → NaN
    conf_finite = np.isfinite(confirmation)
    vol_positive = np.isfinite(volatility) & (volatility > 0.0)
    factor_valid = conf_finite & vol_positive

    raw = np.full(n, np.nan)
    raw[factor_valid] = -confirmation[factor_valid] / volatility[factor_valid]

    # clamp to [-1, 1]，非有限 → NaN（使用 _common.clamp，与 Rust clamp 对齐）
    out = clamp(raw)

    return out


# ─────────────────────────────────────────────────────────────────────────────
# 标量包装：atr2_factor
# ─────────────────────────────────────────────────────────────────────────────


def atr2_factor(
    candles: list[Any],
    *,
    trend_length: int = 8,
    smoothness: int = 20,
    magnify_ob: float = 3.0,
) -> float | None:
    """ATR2 反转因子末值标量（供 parity 测试 + 末值消费）。

    Returns:
        float ∈ [-1, 1] — series 中最后一个有限值；
        None — 不足 warmup（candles 不够或全为 NaN）。
    """
    s = atr2_series(candles, trend_length=trend_length, smoothness=smoothness, magnify_ob=magnify_ob)
    finite_idx = np.where(np.isfinite(s))[0]
    if len(finite_idx) == 0:
        return None
    return float(s[finite_idx[-1]])
