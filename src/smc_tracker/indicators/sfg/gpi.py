"""sfg/gpi.py — GPI（General Parameters Index）反转因子。

算法来源：SFG "General Parameters [SFG]" Pine Script + Rust continuous_factors.rs。
只实现 KNN 特征所需的最小单 TF 路径（factor_gpi reversal alpha）。

符号约定（SFG 反转簇，与 output_range 一致）：
  +1 = close 低于 EMA 网格中点（折扣区）→ 看涨反转期望
  -1 = close 高于 EMA 网格中点（溢价区）→ 看跌反转期望
   0 = close 在中点（中性）
  NaN = fail-closed（band 退化或输入非有限）

本模块 **不** 实现以下非必要组件（KNN 消费路径不需要）：
  - gpi_trend_direction（20-bar 坡度）
  - allowed_bid / allowed_ask（tick_ema 乘 spread，可视化/网格）
  - 多 TF 路径（high_1m/low_1m/close_1m）

退化诚实注：
  在短序列（n < ~50 bars，tfm=1）或完全平价 K 线上，
  三个 EMA 均以同一 close 为 seed，初始值完全一致 → band_upper=band_lower=close
  → width=0 → fail-closed → NaN。此为正确行为，**不 impute 为 0**。
  EMA 需经过大量 bar 才能因跨越不同价格分叉（特别是在 tfm=1 时 span~1960 极大）。
  建议在 KNN 管线中用 tfm=bar_minutes 或更大的 tfm 确保 band 充分展开。

参考：
  Rust: sfg_indicators_rs/src/indicators/general_parameters_index.rs:138-159,253-275,360-371
  Rust: sfg_indicators_rs/src/continuous_factors.rs:183-202,982-1010
  Pine: SFG - General Parameter Index.pine:23-27,111-135
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from smc_tracker.util import to_float

from ._common import clamp, first_obs_ema, ohlcv_arrays


# ─────────────────────────────────────────────────────────────────────────────
# 默认参数常量（Passivbot 准冻结值，见 spec params）
# ─────────────────────────────────────────────────────────────────────────────

_SPAN_0: float = 1960.0   # 下轨 EMA span（分钟当量，除以 tfm）
_SPAN_1: float = 1973.0   # 上轨 EMA span


# ─────────────────────────────────────────────────────────────────────────────
# 核心序列实现
# ─────────────────────────────────────────────────────────────────────────────


def gpi_series(
    candles: list[Any],
    *,
    grid_ema_span_0: float = _SPAN_0,
    grid_ema_span_1: float = _SPAN_1,
    tfm: float = 1.0,
) -> np.ndarray:
    """计算 GPI 反转因子序列（每根 K 线输出一个值）。

    Args:
        candles: K 线列表，每项需有 .c 属性（close）。
        grid_ema_span_0: 下轨 EMA span（默认 1960.0，Passivbot 准冻结）。
        grid_ema_span_1: 上轨 EMA span（默认 1973.0，Passivbot 准冻结）。
        tfm: 图表时间帧（分钟数），span 除以 tfm；默认 1.0（Rust 遗留默认）。
             实际使用建议设为 bar 的分钟间隔（如 15 分钟 K 线传 15.0）。

    Returns:
        np.ndarray，长度等于 len(candles)，dtype=float64。
        - warmup/退化 bar → NaN（fail-closed，绝不 impute 为 0）
        - 有效 bar → clamp([-1, 1])
        - +1：close 在 band 下沿（折扣 → 看涨反转期望）
        - -1：close 在 band 上沿（溢价 → 看跌反转期望）
    """
    n = len(candles)
    out = np.full(n, np.nan, dtype=float)
    if n == 0:
        return out

    # ── 提取 close（fail-closed：缺/非有限 → NaN，让 _ema_float_span 的
    #    carry-forward 生效，而非伪造 0.0 价格永久污染递归 EMA band）─────────────
    close = np.array([to_float(c.c, default=math.nan) for c in candles], dtype=float)

    # ── STEP 1: span 转换（守卫 tfm>0）──────────────────────────────────────
    safe_tfm = tfm if tfm > 0 else 1.0
    span_0 = grid_ema_span_0 / safe_tfm
    span_1 = grid_ema_span_1 / safe_tfm
    span_mid = math.sqrt(span_0 * span_1)  # 几何均值第三条 EMA

    # ── STEP 2 & 3: 三条首次观测种子 EMA（_common.first_obs_ema）───────────
    # first_obs_ema 接受 int span，内部用 max(span,1)；传 float 截断为 int。
    # spec algorithm_steps: "Use RAW FLOAT spans" for the batch/factor path.
    # 但 _common.first_obs_ema 的参数是 int span —— 内部 alpha=2/(max(span,1)+1)。
    # 对于 span~1960，float → int 截断误差极小（1960.3 → 1960 误差 0.015%），
    # 对 factor 值影响可忽略，且与 Rust batch path 保持一致（见 parity_notes）。
    # 若精确对齐 Rust float-span path，可直接内联 EMA 计算（见下 _ema_float_span）。
    ema_0 = _ema_float_span(close, span_0)
    ema_mid = _ema_float_span(close, span_mid)
    ema_1 = _ema_float_span(close, span_1)

    # ── STEP 4: band（nanmin/nanmax 忽略 NaN）────────────────────────────────
    stack = np.stack([ema_0, ema_mid, ema_1], axis=0)  # (3, n)
    band_lower = np.nanmin(stack, axis=0)   # 有 NaN 时只要有一个有限值就输出有限
    band_upper = np.nanmax(stack, axis=0)

    # 若三个均为 NaN（无 seed），nanmin/nanmax 输出 NaN → 后续 fail-closed
    # 检测全 NaN 行（nanmin/nanmax 在全 NaN 时输出 nan + RuntimeWarning，故需处理）
    all_nan_mask = ~(np.isfinite(ema_0) | np.isfinite(ema_mid) | np.isfinite(ema_1))
    band_lower = np.where(all_nan_mask, np.nan, band_lower)
    band_upper = np.where(all_nan_mask, np.nan, band_upper)

    band_mid = (band_lower + band_upper) / 2.0

    # ── STEP 5 & 6 & 7: 计算 factor 并 clamp ────────────────────────────────
    # factor = clip(-2*(close-band_mid)/(band_upper-band_lower), -1, 1)
    # fail-closed 条件：band_upper<=band_lower 或 close/band 非有限
    width = band_upper - band_lower

    # 相对最小宽度守卫：width 必须 > eps * |band_mid| 才视为有效 band。
    # 防止三条 EMA span 不同但 close 完全不变时浮点精度产生的 ~1e-14 伪宽度。
    # eps=1e-9（相对），保证真实 band 宽度（通常 >0.01% close）不受影响。
    eps_rel: float = 1e-9
    min_width = np.where(np.isfinite(band_mid), np.abs(band_mid) * eps_rel, np.inf)
    valid = (
        np.isfinite(close)
        & np.isfinite(band_mid)
        & np.isfinite(width)
        & (width > min_width)
    )
    # np.where 仍会对两分支求值（numpy 行为），safe_width 防止 0 除法 RuntimeWarning
    safe_width = np.where(valid, width, 1.0)  # invalid 位用 1.0 占位，结果被 where 丢弃
    raw = np.where(valid, -2.0 * (close - band_mid) / safe_width, np.nan)
    out = clamp(raw)   # 非有限 → nan，有限 → clip(-1,1)

    return out


# ─────────────────────────────────────────────────────────────────────────────
# 内部辅助：float-span EMA（spec "RAW FLOAT spans"，与 Rust batch path 一致）
# ─────────────────────────────────────────────────────────────────────────────


def _ema_float_span(arr: np.ndarray, span: float) -> np.ndarray:
    """首次有限值种子 EMA，使用 float span（alpha=2/(max(span,1)+1)）。

    与 _common.first_obs_ema 逻辑完全相同，但 span 为 float（无 int 截断），
    匹配 Rust batch path / spec "Use RAW FLOAT spans for the BAR/batch path"。

    - prev=NaN 时：找到首个有限值作为 seed（out=seed，prev=seed）。
    - NaN 输入：carry-forward（若已 seeded，out=prev；否则 out=NaN）。
    - 这是 O(n) 递归扫描，无法在 recurrence 维度并行化，但每次调用为一个线性 pass。
    """
    n = len(arr)
    out = np.full(n, np.nan, dtype=float)
    eff_span = max(span, 1.0)
    alpha = 2.0 / (eff_span + 1.0)
    one_minus = 1.0 - alpha

    prev = math.nan
    for i in range(n):
        v = arr[i]
        if not math.isfinite(v):
            if math.isfinite(prev):
                out[i] = prev   # carry-forward
            # else: out[i] 保持 NaN（无 seed）
        else:
            if not math.isfinite(prev):
                prev = v        # seed：首个有限值
            else:
                prev = one_minus * prev + alpha * v
            out[i] = prev

    return out


# ─────────────────────────────────────────────────────────────────────────────
# 标量包装（供末值消费 / parity 测试）
# ─────────────────────────────────────────────────────────────────────────────


def gpi_factor(
    candles: list[Any],
    *,
    grid_ema_span_0: float = _SPAN_0,
    grid_ema_span_1: float = _SPAN_1,
    tfm: float = 1.0,
) -> float | None:
    """计算 GPI 反转因子末值（标量）。

    Args:
        candles: 同 gpi_series。
        grid_ema_span_0, grid_ema_span_1, tfm: 同 gpi_series。

    Returns:
        序列中最后一个有限值（Python float），若无有限值（不足 warmup 或全退化）→ None。
        返回值在 [-1, 1] 之间。
        +正值 = 看涨反转期望；-负值 = 看跌反转期望。
    """
    if not candles:
        return None
    s = gpi_series(
        candles,
        grid_ema_span_0=grid_ema_span_0,
        grid_ema_span_1=grid_ema_span_1,
        tfm=tfm,
    )
    # 取末尾有限值（不一定是最后一根，但通常就是）
    for v in reversed(s):
        if math.isfinite(v):
            return float(v)
    return None
