"""布林带多周期压力/支撑分析器（纯计算，无 I/O，可测）。

主路径：talib.BBANDS（用户明确要求 TA-Lib）；import 失败自动回退 numpy bollinger。
数值平价：matype=0 即 SMA + ddof=0 std，与 numpy bollinger 完全一致（tests/test_talib_parity.py 已验证）。
"""
from __future__ import annotations

import numpy as np

# ---- TA-Lib 可选导入（主路径） ----
try:
    import talib as _talib
    _HAS_TALIB = True
except ImportError:  # pragma: no cover
    _talib = None  # type: ignore[assignment]
    _HAS_TALIB = False

from .technical import bollinger as _np_bollinger, ohlcv_arrays


def bb_bands(
    close: np.ndarray,
    period: int = 20,
    k: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """计算布林带，优先 talib.BBANDS（matype=0），失败回退 numpy bollinger。

    Args:
        close:  收盘价序列（float64）
        period: 均线周期（默认 20）
        k:      标准差倍数（默认 2.0）

    Returns:
        (upper, mid, lower) — 三条等长 ndarray，warmup 段为 NaN。
    """
    if _HAS_TALIB:
        upper, mid, lower = _talib.BBANDS(
            close, timeperiod=period, nbdevup=k, nbdevdn=k, matype=0
        )
        return upper, mid, lower
    return _np_bollinger(close, n=period, k=k)  # pragma: no cover


def _pos_label(pct_b: float) -> str:
    """按 %B 值分档返回人类可读位置标签。"""
    if pct_b >= 1.0:
        return "触上轨/压力(超买)"
    if pct_b >= 0.8:
        return "逼近压力"
    if pct_b > 0.5:
        return "中轨上偏多"
    if pct_b > 0.2:
        return "中轨下偏空"
    if pct_b > 0.0:
        return "逼近支撑"
    return "触下轨/支撑(超卖)"


def analyze_tf(
    candles: list,
    period: int = 20,
    k: float = 2.0,
) -> dict | None:
    """对单周期 K 线列表执行布林带分析，返回结构化指标 dict。

    Args:
        candles: Candle 对象列表（需 .c 属性）
        period:  BB 均线周期（默认 20）
        k:       标准差倍数（默认 2.0）

    Returns:
        None 若 K 线不足 period+1；
        否则 dict 含键：upper/mid/lower/price/pct_b/bandwidth/squeeze/pos_label/bull
    """
    if len(candles) < period + 1:
        return None

    arrays = ohlcv_arrays(candles)
    c_arr = arrays["c"]
    upper_arr, mid_arr, lower_arr = bb_bands(c_arr, period=period, k=k)

    # 取末值
    upper = float(upper_arr[-1])
    mid   = float(mid_arr[-1])
    lower = float(lower_arr[-1])
    price = float(c_arr[-1])

    # %B：带内位置；分母 ≤0 守卫 → 0.5（中性兜底）
    band_width_val = upper - lower
    if band_width_val > 1e-12:
        pct_b = (price - lower) / band_width_val
    else:
        pct_b = 0.5

    # bandwidth = (upper-lower)/mid（相对带宽，反映波动率）
    bandwidth = band_width_val / mid if abs(mid) > 1e-12 else 0.0

    # squeeze：带宽处于近 N 根低位（bandwidth < 近窗中位数*0.6）
    # N=min(len,120)，不足则不判为挤压
    squeeze_n = min(len(c_arr), 120)
    squeeze = False
    if squeeze_n >= period + 1:
        # 计算近 squeeze_n 根的所有有效 bandwidth 值
        # 全窗(squeeze_n==len)时复用上面已算的 upper/mid/lower，避免重复 talib 调用(T3)
        if squeeze_n == len(c_arr):
            up_hist, mid_hist, lo_hist = upper_arr, mid_arr, lower_arr
        else:
            up_hist, mid_hist, lo_hist = bb_bands(c_arr[-squeeze_n:], period=period, k=k)
        bw_hist = np.where(
            np.abs(mid_hist) > 1e-12,
            (up_hist - lo_hist) / mid_hist,
            0.0,
        )
        finite_bw = bw_hist[np.isfinite(bw_hist) & (bw_hist > 0)]
        if len(finite_bw) >= period:
            squeeze = bool(bandwidth < float(np.median(finite_bw)) * 0.6)

    return {
        "upper":     upper,
        "mid":       mid,
        "lower":     lower,
        "price":     price,
        "pct_b":     pct_b,
        "bandwidth": bandwidth,
        "squeeze":   squeeze,
        "pos_label": _pos_label(pct_b),
        "bull":      pct_b > 0.5,
    }


def _lean(consensus_pct: int) -> str:
    """按多头占比返回共识标签（内联，不依赖 digest 模块）。"""
    if consensus_pct >= 80:
        return "净多"
    if consensus_pct >= 60:
        return "偏多"
    if consensus_pct > 40:
        return "分歧"
    if consensus_pct > 20:
        return "偏空"
    return "净空"


def aggregate_coin(tf_results: dict[str, dict | None]) -> dict:
    """汇总某币各周期 analyze_tf 结果，计算多空共识。

    Args:
        tf_results: {timeframe: analyze_tf_dict_or_None}

    Returns:
        dict 含 bull_n/bear_n/total/consensus_pct/lean_label/squeeze_n
    """
    bull_n = 0
    bear_n = 0
    squeeze_n = 0

    for result in tf_results.values():
        if result is None:
            continue  # 数据不足的周期跳过
        pct_b = result.get("pct_b")
        if pct_b == 0.5:
            # pct_b==0.5 为 band_width=0（全平价）时的中性兜底值，
            # 不应计入 bull 也不应计入 bear，避免偏置共识方向。
            if result.get("squeeze"):
                squeeze_n += 1
            continue
        if result.get("bull"):
            bull_n += 1
        else:
            bear_n += 1
        if result.get("squeeze"):
            squeeze_n += 1

    total = bull_n + bear_n
    if total == 0:
        consensus_pct = 50
    else:
        consensus_pct = round(100 * bull_n / total)

    return {
        "bull_n":        bull_n,
        "bear_n":        bear_n,
        "total":         total,
        "consensus_pct": consensus_pct,
        "lean_label":    _lean(consensus_pct),
        "squeeze_n":     squeeze_n,
    }
