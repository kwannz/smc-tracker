"""sfg/pivot.py — SFG Pivot 反转因子（reversal-cluster「pivot structural-level proximity」）。

算法来源：
  - Rust: sfg_indicators_rs/src/continuous_factors.rs:252-256 (factor_pivot)
  - Rust: sfg_indicators_rs/src/indicators/pivot_buy_sell.rs:63-200 (pivot detection + shelf)
  - Pine: SFG - Pivot BS Signals.pine (ta.pivothigh/pivotlow + valuewhen carry-forward)

因子公式（spec factor_formula）：
  top  = forward-filled 最近确认的 pivot-high level（压力架）
  bot  = forward-filled 最近确认的 pivot-low  level（支撑架）
  c    = close[i]

  f = clamp( (top + bot - 2c) / (top - bot), -1, 1 )

  fail-closed：任一非有限 或 top <= bot → NaN

符号约定（SFG reversal sign_convention）：
  +1 = close 在 bot（支撑位）        → 看涨反转信号
  -1 = close 在 top（压力位）        → 看跌反转信号
   0 = close 在通道中点              → 中性
  NaN = warmup / 无完整 shelf / top<=bot 退化

重要：这是 mean-reversion/反转因子，与 Pine 突破决策信号方向相反；
不构成投资建议，KNN 特征使用时注意右侧滞后 right_bars 根。

确认滞后（lookahead_risk=LOW，无前视，但有 right_bars 滞后）：
  pivot 中心 c 在 c + right_bars 处确认，shelf = ffill(已确认 pivot)。
  当前 bar 只看已确认历史，不读未来。KNN 标签对齐时需计入此滞后。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from ._common import (
    ohlcv_arrays,
    pivot_high_series,
    pivot_low_series,
    forward_fill,
    level_factor,
)


def pivot_series(
    candles: list[Any],
    left_bars: int = 10,
    right_bars: int = 10,
) -> np.ndarray:
    """Pivot 反转因子序列（全长，warmup 段为 nan）。

    Args:
        candles:   K 线列表，每项需有 .h/.l/.c 属性（与 ohlcv_arrays 兼容）。
        left_bars:  pivot 左侧确认窗口（默认 10，spec params）。
        right_bars: pivot 右侧确认滞后（默认 10，确认 bar = center + right_bars）。

    Returns:
        shape (n,) 的 float64 ndarray：
          - 有限值在 [-1, 1]（clamp）
          - warmup / 无完整 shelf / 退化通道 → NaN（fail-closed sentinel）
          - 绝不 impute NaN 为 0

    因果保证（no-lookahead）：
        out[i] 仅用 candles[0..i]（含 i）推导，prefix-invariance。
        pivot_high/low 在中心 c 的确认 bar i = c + right_bars 处才写入 shelf。

    退化标注：
        若序列极短（n < left_bars + right_bars + 1），所有值均为 nan。
        若通道内 top <= bot（同级极值 / 平坦序列），该 bar 的 factor 为 nan。
    """
    if not candles:
        return np.array([], dtype=float)

    arrs = ohlcv_arrays(candles)
    h: np.ndarray = arrs["h"]
    l: np.ndarray = arrs["l"]
    c: np.ndarray = arrs["c"]
    n = len(c)

    # ── STEP 1：pivot 探测（确认滞后，spec:pivot_buy_sell.rs:63-126）─────────
    # pivot_high_series/pivot_low_series 已实现「在中心 c+right 处写入」语义
    ph: np.ndarray = pivot_high_series(h, left=left_bars, right=right_bars)
    pl: np.ndarray = pivot_low_series(l, left=left_bars, right=right_bars)

    # ── STEP 2：shelf level = 确认 bar 时读取的 pivot 极值（spec:STEP 2）────────
    # pivot_high_series 直接写入 high[center]（pivot 极值），
    # 对应 Rust h_shift[i] = high[i - right_bars]，即确认 bar 时的中心价格。
    # 此处 ph[i] 本身即为 top_level（已含确认偏移），无需额外 shift。

    # ── STEP 3：forward-fill → top_shift / bot_shift（spec:STEP 3）───────────
    # valuewhen_ffill：走到 ph 有限处，取该值并 forward-fill 到后续 bar
    top_shift: np.ndarray = forward_fill(ph)
    bot_shift: np.ndarray = forward_fill(pl)

    # ── STEP 4-6：level_factor + clamp（spec:STEP 4-6）───────────────────────
    # level_factor 实现: (mid-c)/half_range，half_range=(top-bot)/2
    # = (top+bot-2c)/(top-bot)，即 spec factor_formula
    # fail-closed：任一非有限 或 top<=bot（half_range<=0）→ NaN
    out: np.ndarray = level_factor(close=c, lower=bot_shift, upper=top_shift)

    return out


def pivot_factor(
    candles: list[Any],
    left_bars: int = 10,
    right_bars: int = 10,
) -> float | None:
    """Pivot 反转因子标量：返回 pivot_series 最后一个有限值。

    供末值消费（KNN 特征提取、parity 测试）。

    Args:
        candles:   K 线列表。
        left_bars:  pivot 左侧确认窗口（默认 10）。
        right_bars: pivot 右侧确认滞后（默认 10）。

    Returns:
        float 在 [-1, 1]，或 None（warmup 期 / 序列过短 / 无完整 shelf）。
    """
    s = pivot_series(candles, left_bars=left_bars, right_bars=right_bars)
    if len(s) == 0:
        return None
    # 取最后一个有限值
    finite_mask = np.isfinite(s)
    if not finite_mask.any():
        return None
    return float(s[finite_mask][-1])
