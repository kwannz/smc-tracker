"""技术指标引擎：10 指标 + 价格行为 + 4 combo + 斐波那契 + 支撑压力 + 时间策略 + KNN 预测
+ 布林带多周期分析。

纯 numpy（低延迟，无 TA-Lib/pandas 硬依赖；已向量化 ~1ms）。
数值正确性由 TA-Lib 基准交叉验证（tests/test_talib_parity.py，10 指标浮点级一致，
TA-Lib 未装则自动跳过——零硬依赖）。
布林带多周期分析（bollinger_bands.py）：主路径 talib.BBANDS，回退 numpy bollinger。
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
from .bollinger_bands import bb_bands, analyze_tf, aggregate_coin
from .harmonic import (find_pivots, pivots_from_structure, _alternate_immutable,
                       detect_xabcd, project_prz, analyze_candles,
                       HARMONIC_RATIOS)
from .harmonic_state import HarmonicState
from .atr2_signals import atr2_confirmation
from .sfg import (
    lrsd_series, gpi_series, vap_series, pdbb_series, pivot_series,
    ami_series, atr2_series, msfvg_series, ai_st_series, dmha_series,
    level_factor,
)

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
    "bb_bands", "analyze_tf", "aggregate_coin",
    "find_pivots", "pivots_from_structure", "_alternate_immutable",
    "detect_xabcd", "project_prz", "analyze_candles", "HARMONIC_RATIOS",
    "HarmonicState",
    "atr2_confirmation",
    # SFG 10 因子 series（向量化，供 KNN feature_matrix 使用，零孤儿）
    "lrsd_series", "gpi_series", "vap_series", "pdbb_series", "pivot_series",
    "ami_series", "atr2_series", "msfvg_series", "ai_st_series", "dmha_series",
    "level_factor",
]
