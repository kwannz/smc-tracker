"""atr2_signals.py — SFG ATR2 归一化动量确认信号（纯 numpy 实现）。

算法来源：SFG - ATR2 Signals.pine (Pine Script v5)
  mom = ta.change(src, trendLength)              # close[t] - close[t-trendLength]
  volatility = ta.stdev(mom, smoothness)         # 滚动标准差
  normMom = mom / volatility                      # 归一化动量（分母守卫 ≤0）
  smoothedMom = ta.sma(normMom, smoothness)       # 再次平滑
  magnifiedMom = smoothedMom * magnifyOB          # 放大倍数（默认 3.0）

bias 判断：
  magnifiedMom > threshold  → "long"（偏多）
  magnifiedMom < -threshold → "short"（偏空）
  否则                       → "neutral"

诚实标注（CLAUDE.md §二）：ATR2 是动量确认辅助，非预测保证；
历史统计高 lift≠赚钱，仅辅助谐波/订单流确认，不构成投资建议。
"""
from __future__ import annotations

from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .technical import atr, ohlcv_arrays


def atr2_confirmation(
    candles: list[Any],
    trend_length: int = 8,
    smoothness: int = 20,
    magnify: float = 3.0,
    threshold: float = 1.0,
) -> dict[str, Any] | None:
    """计算 SFG ATR2 归一化动量确认值。

    Args:
        candles: K 线列表，每项需有 .h/.l/.c 属性（与 ohlcv_arrays 兼容）。
        trend_length: 动量计算步长（Pine trendLength=8 默认值）。
        smoothness: 滚动标准差与 SMA 平滑窗口（Pine smoothness=20 默认值）。
        magnify: 放大倍数（Pine magnifyOB=3.0 默认值）。
        threshold: bias 判断阈值（|magnifiedMom|>threshold→long/short；默认 1.0）。

    Returns:
        dict 包含：
          "confirmation": float  — magnifiedMom 末值（>0偏多，<0偏空）
          "bias": str            — "long" / "short" / "neutral"
          "atr": float           — ATR(14) 末值（波动率止损参考）
          "atr_pct": float       — atr / 末根收盘价（比例化，便于跨币比较）
        candles 不足（< trend_length + smoothness - 1 + smoothness，即暖机期）→ None。
    """
    # ── 最小数量守卫 ──────────────────────────────────────────────────────────
    # mom 需要 trend_length 根前置数据；
    # 滚动 stdev(mom, smoothness) 需要 smoothness 根有效 mom；
    # SMA(normMom, smoothness) 再需要 smoothness 根有效 normMom。
    # 总需最少: trend_length + smoothness - 1 + smoothness - 1 + 1
    #        = trend_length + 2*smoothness - 1
    # ATR(14) 需要 15 根，通常小于上面的值。
    min_required: int = trend_length + 2 * smoothness - 1
    if not candles or len(candles) < min_required:
        return None

    # ── 提取 close / high / low ──────────────────────────────────────────────
    arrs = ohlcv_arrays(candles)
    c: np.ndarray = arrs["c"]
    h: np.ndarray = arrs["h"]
    l: np.ndarray = arrs["l"]

    # ── Pine ta.change(src, trendLength) ─────────────────────────────────────
    # mom[i] = close[i] - close[i - trend_length]
    mom: np.ndarray = np.full(len(c), np.nan)
    mom[trend_length:] = c[trend_length:] - c[:-trend_length]

    # ── Pine ta.stdev(mom, smoothness) ───────────────────────────────────────
    # 只对有效段（从 trend_length 起）做滚动标准差，其余保持 NaN
    volatility: np.ndarray = np.full(len(c), np.nan)
    # mom 有效段从 trend_length 开始；在该段用 sliding_window_view
    mom_valid = mom[trend_length:]   # shape: (len(c) - trend_length,)
    if len(mom_valid) >= smoothness:
        stdev_vals = np.std(
            sliding_window_view(mom_valid, smoothness), axis=1
        )  # shape: (len(mom_valid) - smoothness + 1,)
        # 填回完整数组：起始位置 = trend_length + smoothness - 1
        start_idx = trend_length + smoothness - 1
        volatility[start_idx:] = stdev_vals

    # ── normMom = mom / volatility（分母守卫 + 零波动退化） ───────────────────
    # 正常: norm = mom/volatility；
    # **零波动退化**(完美线性趋势 mom 恒定→std=0，真实交易罕见但合成/极端行情会出现):
    #   无波动参考无法归一化，按动量方向给**有界强确认 ±2**（perfect trend=强方向），
    #   避免 NaN 让整条信号失效（此前 bug：linear trend→None）。
    norm_mom: np.ndarray = np.full(len(c), np.nan)
    finite = np.isfinite(volatility) & np.isfinite(mom)
    pos_vol = finite & (volatility > 1e-12)
    norm_mom[pos_vol] = mom[pos_vol] / volatility[pos_vol]
    zero_vol = finite & (volatility <= 1e-12)
    norm_mom[zero_vol] = np.sign(mom[zero_vol]) * 2.0

    # ── SMA(normMom, smoothness) ──────────────────────────────────────────────
    # 仅对 norm_mom 有效段做 SMA，避免 cumsum NaN 污染
    smoothed_mom: np.ndarray = np.full(len(c), np.nan)
    # norm_mom 有效段从 start_idx = trend_length + smoothness - 1 开始
    start_idx = trend_length + smoothness - 1
    norm_valid = norm_mom[start_idx:]
    if len(norm_valid) >= smoothness:
        # 用 sliding_window_view 做 NaN-safe 均值（窗口内全有限才计算）
        windows = sliding_window_view(norm_valid, smoothness)  # (m, smoothness)
        # 检查每窗是否全有限
        all_finite = np.all(np.isfinite(windows), axis=1)
        sma_vals = np.where(all_finite, windows.mean(axis=1), np.nan)
        sma_start = start_idx + smoothness - 1
        smoothed_mom[sma_start:] = sma_vals

    # ── magnifiedMom = smoothedMom × magnify ─────────────────────────────────
    magnified_mom: np.ndarray = smoothed_mom * magnify

    # ── 取末值 ────────────────────────────────────────────────────────────────
    last_magnified = magnified_mom[-1]
    if not np.isfinite(last_magnified):
        # 暖机期不足，仍无有效值
        return None

    confirmation: float = float(last_magnified)

    # ── ATR(14) 末值 ──────────────────────────────────────────────────────────
    atr_arr = atr(h, l, c, 14)
    last_atr = atr_arr[-1]
    if not np.isfinite(last_atr) or last_atr <= 0:
        return None

    atr_val: float = float(last_atr)
    last_close: float = float(c[-1])
    # 分母守卫（价格不应为零，但防御性检查）
    atr_pct: float = atr_val / last_close if last_close > 0 else 0.0

    # ── bias 判断 ──────────────────────────────────────────────────────────────
    if confirmation > threshold:
        bias = "long"
    elif confirmation < -threshold:
        bias = "short"
    else:
        bias = "neutral"

    return {
        "confirmation": confirmation,
        "bias": bias,
        "atr": atr_val,
        "atr_pct": atr_pct,
    }
