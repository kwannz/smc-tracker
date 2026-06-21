"""技术指标引擎：10 指标 + 价格行为 + 4 combo + 斐波那契 + 支撑压力 + 时间策略 + KNN 预测。

纯 numpy（低延迟，无 TA-Lib/pandas 硬依赖；已向量化 ~1ms）。
数值正确性由 TA-Lib 基准交叉验证（tests/test_talib_parity.py，10 指标浮点级一致，
TA-Lib 未装则自动跳过——零硬依赖）。
"""
from .technical import (adx, atr, bollinger, cci, ema, macd, obv, rsi, sma,
                        stochastic, vwap, ohlcv_arrays, compute_indicators)
from .price_action import detect_patterns, pa_features, pa_bias
from .fibonacci import fib_levels, in_golden_pocket, nearest_fib
from .levels import support_resistance, pivot_points, nearest_levels
from .combo import combo_signals, combo_consensus
from .sessions import current_session, in_killzone
from .knn import KNNPredictor, feature_matrix
from .patterns import (detect_double_top, detect_double_bottom, dow_trend,
                       swing_highs, swing_lows)
from .volume import (relative_volume, volume_spike, volume_trend, volume_profile,
                     VolumeMonitor)
from .engine import analyze, fmt_analysis

__all__ = [
    "detect_double_top", "detect_double_bottom", "dow_trend", "swing_highs", "swing_lows",
    "relative_volume", "volume_spike", "volume_trend", "volume_profile", "VolumeMonitor",
    "rsi", "macd", "ema", "sma", "bollinger", "atr", "stochastic", "adx", "obv",
    "vwap", "cci", "ohlcv_arrays", "compute_indicators",
    "detect_patterns", "pa_features", "pa_bias",
    "fib_levels", "in_golden_pocket", "nearest_fib",
    "support_resistance", "pivot_points", "nearest_levels",
    "combo_signals", "combo_consensus",
    "current_session", "in_killzone",
    "KNNPredictor", "feature_matrix", "analyze", "fmt_analysis",
]
