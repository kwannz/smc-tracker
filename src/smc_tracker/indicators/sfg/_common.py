"""sfg/_common.py — SFG 因子公共数值原语。

所有函数保证：
  - 纯 numpy 向量化（低延迟）
  - **trailing 尾对齐**：out[i] 只使用 x[0..i]，绝不读 x[i+1..] 未来数据
  - 输出等长，warmup 段填 nan
  - no-lookahead / no-repaint（prefix-invariance）在测试层钉死

这是后续 10 个因子的共享地基，正确性极关键。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from smc_tracker.util import to_float


# ─────────────────────────────────────────────────────────────────────────────
# 基础保护原语
# ─────────────────────────────────────────────────────────────────────────────

def clamp(x: np.ndarray) -> np.ndarray:
    """将数组 clamp 到 [-1, 1]；非有限值 → nan（fail-closed sentinel）。

    与 SFG Rust clamp() [continuous_factors.rs:104-112] 语义完全一致：
    非有限 → NaN（不是 0），有限 → clamp(-1,1)。
    """
    out = np.where(np.isfinite(x), x, np.nan)
    # 仅对有限值做 clip（避免 clip(nan) 行为不一致）
    finite_mask = np.isfinite(out)
    clipped = np.clip(out, -1.0, 1.0)
    # 保证 nan 不被 clip 覆盖
    result = np.where(finite_mask, clipped, np.nan)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 反转因子共享内核
# ─────────────────────────────────────────────────────────────────────────────

def level_factor(
    close: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
) -> np.ndarray:
    """6 个反转因子共享内核：(mid - close) / half_range → clamp[-1,1]。

    语义（SFG sign_convention）：
      +1 = close 在支撑/折扣区 = 看涨反转信号
      -1 = close 在压力/溢价区 = 看跌反转信号
       0 = close 在中点 = 中性
    NaN 触发条件：half_range ≤ 0 或任一输入非有限。
    """
    # 计算中点和半宽
    mid = (lower + upper) / 2.0
    half = (upper - lower) / 2.0

    # 守卫：half<=0 或任意输入非有限 → nan
    valid = (
        np.isfinite(close)
        & np.isfinite(lower)
        & np.isfinite(upper)
        & (half > 0)
    )

    # 仅在 valid 处做除法（np.divide where 守卫），避免 half=0 触发
    # "invalid value encountered in divide" 警告（out 预填 nan 即 fail-closed sentinel）
    raw = np.full(close.shape, np.nan, dtype=float)
    np.divide(mid - close, half, out=raw, where=valid)
    return clamp(raw)


# ─────────────────────────────────────────────────────────────────────────────
# EMA（首个有限值为 seed，NaN carry-forward）
# ─────────────────────────────────────────────────────────────────────────────

def first_obs_ema(arr: np.ndarray, span: int) -> np.ndarray:
    """EMA，以序列中首个有限值为 seed；中间 NaN carry-forward（前值保持）。

    与标准 EMA 的区别：
      - 首个 NaN 前：输出 NaN（无种子）
      - 首个有限值：输出 = 该值（seed，无递推）
      - 之后有限值：正常 EMA 递推
      - 之后 NaN 值：carry-forward（保持上一个有限 EMA 输出）
    GPI 因子需要此语义（乘以首根有效价格为基准）。
    alpha = 2 / (max(span,1) + 1)
    """
    n = len(arr)
    out = np.full(n, np.nan)
    effective_span = max(span, 1)
    alpha = 2.0 / (effective_span + 1.0)
    one_minus_alpha = 1.0 - alpha

    prev = math.nan
    seeded = False

    for i in range(n):
        v = arr[i]
        if not math.isfinite(v):
            # carry-forward：若已有种子则保持 prev，否则继续 NaN
            if seeded:
                out[i] = prev
            # else: out[i] 保持 NaN
        else:
            if not seeded:
                # 首个有限值作种子
                prev = v
                seeded = True
            else:
                prev = one_minus_alpha * prev + alpha * v
            out[i] = prev

    return out


# ─────────────────────────────────────────────────────────────────────────────
# 移动平均系列（全部 trailing，绝不居中）
# ─────────────────────────────────────────────────────────────────────────────

def sma_series(x: np.ndarray, n: int) -> np.ndarray:
    """简单移动平均（trailing）：out[i] = mean(x[i-n+1 .. i])。

    warmup（i < n-1）→ nan。
    """
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    # cumsum 法向量化，O(N)
    cs = np.cumsum(np.insert(x.astype(float), 0, 0.0))
    out[n - 1:] = (cs[n:] - cs[:len(x) - n + 1]) / n
    return out


def wma_series(x: np.ndarray, n: int) -> np.ndarray:
    """加权移动平均（trailing，线性权重，最新权最大）。

    out[i] = Σ(k·x[i-n+1+k]) for k=1..n  / Σ(k) for k=1..n
           = WMA(x[i-n+1..i], weights=[1,2,...,n])
    warmup（i < n-1）→ nan。
    这是 HMA 的构建块，trailing 保证 no-lookahead。
    """
    m = len(x)
    out = np.full(m, np.nan)
    if n <= 0 or m < n:
        return out
    # 权重：1, 2, ..., n（最新权最大）
    weights = np.arange(1.0, n + 1.0)  # shape (n,)
    denom = weights.sum()              # = n*(n+1)/2
    # sliding_window_view 产生 shape (m-n+1, n) 的矩阵（每行=trailing窗口）
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(x.astype(float), n)  # (m-n+1, n)
    # 行向量乘权重，求和，除以 denom
    out[n - 1:] = (windows * weights) .sum(axis=1) / denom
    return out


def hma_series(x: np.ndarray, n: int) -> np.ndarray:
    """赫尔移动平均（Hull MA）：基于 trailing WMA，低延迟。

    Pine Script 公式：
      hma = wma(2*wma(n/2) - wma(n), round(sqrt(n)))
    所有内部 WMA 均 trailing，保证 no-lookahead。
    warmup = n - 1 + round(sqrt(n)) - 1 根 → 全部 nan。
    """
    m = len(x)
    out = np.full(m, np.nan)
    if n <= 0 or m == 0:
        return out

    half_n = max(1, round(n / 2))
    sqrt_n = max(1, round(math.sqrt(n)))

    wma_n = wma_series(x, n)
    wma_half = wma_series(x, half_n)

    # 组合序列：2*wma(n/2) - wma(n)
    combined = np.where(
        np.isfinite(wma_half) & np.isfinite(wma_n),
        2.0 * wma_half - wma_n,
        np.nan,
    )

    # 对 combined 再做 trailing WMA(sqrt_n)
    hma = wma_series(combined, sqrt_n)
    return hma


# ─────────────────────────────────────────────────────────────────────────────
# 滚动极值
# ─────────────────────────────────────────────────────────────────────────────

def rolling_max_series(
    x: np.ndarray, n: int, min_periods: int | None = None
) -> np.ndarray:
    """滚动最大值（trailing，out[i] = max(x[i-n+1..i])）。

    min_periods: 窗口至少需要多少个有效元素才输出（默认=n，即满窗）。
    warmup（不足 min_periods）→ nan。
    """
    return _rolling_extreme(x, n, min_periods=min_periods, mode="max")


def rolling_min_series(
    x: np.ndarray, n: int, min_periods: int | None = None
) -> np.ndarray:
    """滚动最小值（trailing，out[i] = min(x[i-n+1..i])）。

    min_periods: 默认=n（满窗）。
    """
    return _rolling_extreme(x, n, min_periods=min_periods, mode="min")


def _rolling_extreme(
    x: np.ndarray, n: int, min_periods: int | None, mode: str
) -> np.ndarray:
    """rolling_max / rolling_min 的内部实现。"""
    m = len(x)
    out = np.full(m, np.nan)
    if n <= 0 or m == 0:
        return out

    effective_min = n if min_periods is None else min_periods
    effective_min = max(1, effective_min)

    fn = np.max if mode == "max" else np.min

    for i in range(m):
        start = max(0, i - n + 1)
        window = x[start : i + 1]
        # 仅统计有限值
        finite_vals = window[np.isfinite(window)]
        if len(finite_vals) >= effective_min:
            out[i] = fn(finite_vals)

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Pivot 极值（确认滞后，prefix-invariance 保证 no-repaint）
# ─────────────────────────────────────────────────────────────────────────────

def pivot_high_series(
    high: np.ndarray, left: int = 5, right: int = 5
) -> np.ndarray:
    """Pivot High 序列：中心 bar c 是 left 根和 right 根内的严格最高点时发射。

    **关键：发射时机在 i = c + right 处写入**（确认滞后=right 根）。
    i < c + right 时全为 nan，严格不读未来。
    严格不等（等于不算 pivot）。
    返回 shape 与输入相同，nan = 无 pivot；非 nan = pivot 价格。
    """
    m = len(high)
    out = np.full(m, np.nan)
    if m < left + right + 1:
        return out

    for c in range(left, m - right):
        pivot_val = high[c]
        if not math.isfinite(pivot_val):
            continue
        # 严格大于左边 left 根
        left_ok = all(
            math.isfinite(high[c - j]) and pivot_val > high[c - j]
            for j in range(1, left + 1)
        )
        if not left_ok:
            continue
        # 严格大于右边 right 根
        right_ok = all(
            math.isfinite(high[c + j]) and pivot_val > high[c + j]
            for j in range(1, right + 1)
        )
        if not right_ok:
            continue
        # 确认滞后：在 i = c + right 处写入
        out[c + right] = pivot_val

    return out


def pivot_low_series(
    low: np.ndarray, left: int = 5, right: int = 5
) -> np.ndarray:
    """Pivot Low 序列：中心 bar c 是 left 根和 right 根内的严格最低点时发射。

    发射时机与 pivot_high_series 一致（i = c + right 处写入）。
    """
    m = len(low)
    out = np.full(m, np.nan)
    if m < left + right + 1:
        return out

    for c in range(left, m - right):
        pivot_val = low[c]
        if not math.isfinite(pivot_val):
            continue
        # 严格小于左边 left 根
        left_ok = all(
            math.isfinite(low[c - j]) and pivot_val < low[c - j]
            for j in range(1, left + 1)
        )
        if not left_ok:
            continue
        # 严格小于右边 right 根
        right_ok = all(
            math.isfinite(low[c + j]) and pivot_val < low[c + j]
            for j in range(1, right + 1)
        )
        if not right_ok:
            continue
        # 确认滞后：在 i = c + right 处写入
        out[c + right] = pivot_val

    return out


# ─────────────────────────────────────────────────────────────────────────────
# 前向填充（look-ahead-safe，只向后填）
# ─────────────────────────────────────────────────────────────────────────────

def forward_fill(arr: np.ndarray) -> np.ndarray:
    """用前一个有限值填充 NaN（前向填充，无前视）。

    前导 NaN（无历史可填）→ 保持 NaN。
    仅用 x[0..i] 填 x[i]，无论 x[i+1..] 如何都不影响早期输出（no-lookahead）。
    """
    out = arr.copy().astype(float)
    last = math.nan
    for i in range(len(out)):
        if math.isfinite(out[i]):
            last = out[i]
        elif math.isfinite(last):
            out[i] = last
        # else: out[i] 保持 NaN（无历史）
    return out


# ─────────────────────────────────────────────────────────────────────────────
# OHLCV 提取（util.to_float 守卫）
# ─────────────────────────────────────────────────────────────────────────────

def ohlcv_arrays(candles: list[Any]) -> dict[str, np.ndarray]:
    """Candle 列表 → {o,h,l,c,v} numpy 数组。

    **fail-closed 语义**：缺/非有限字段（NaN/inf/None）→ NaN（不是 0.0）。
    SFG 因子的下游均有 NaN 守卫（carry-forward / fail-closed gate），缺数据
    应传播为 NaN 让因子诚实弃权，而非伪造 0.0 价格（会污染递归 EMA/带边界）。
    与 SFG Rust 的 is_finite() fail-closed 语义一致。
    （一般数据摄入仍用 util.to_float 默认 →0.0；此处 SFG 因子语境专用 NaN。）
    """
    if not candles:
        empty = np.array([], dtype=float)
        return {"o": empty, "h": empty, "l": empty, "c": empty, "v": empty}
    nan = math.nan
    return {
        "o": np.array([to_float(c.o, default=nan) for c in candles], dtype=float),
        "h": np.array([to_float(c.h, default=nan) for c in candles], dtype=float),
        "l": np.array([to_float(c.l, default=nan) for c in candles], dtype=float),
        "c": np.array([to_float(c.c, default=nan) for c in candles], dtype=float),
        "v": np.array([to_float(c.v, default=nan) for c in candles], dtype=float),
    }
