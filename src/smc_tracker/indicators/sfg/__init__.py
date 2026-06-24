"""sfg — SFG 因子移植子包。

本包提供 SFG 10 因子的 Python/numpy 移植。
公共数值原语由 _common.py 提供（零前视、尾对齐、全向量化）。
10 个因子的 *_series 向量化函数（返回等长 np.ndarray，warmup=nan）
供 KNN feature_matrix 直接切片使用（一次计算全序列，循环内 [i] 取值）。
"""
from __future__ import annotations

# 公共原语
from ._common import (
    clamp,
    level_factor,
    first_obs_ema,
    sma_series,
    wma_series,
    hma_series,
    rolling_max_series,
    rolling_min_series,
    pivot_high_series,
    pivot_low_series,
    forward_fill,
    ohlcv_arrays,
)

# 10 个因子的向量化 series 函数（KNN 特征矩阵用途）
from .lrsd import lrsd_series
from .gpi import gpi_series
from .vap import vap_series
from .pdbb import pdbb_series
from .pivot import pivot_series
from .ami import ami_series
from .atr2 import atr2_series
from .msfvg import msfvg_series
from .ai_st import ai_st_series
from .dmha import dmha_series

__all__ = [
    # 公共原语
    "clamp",
    "level_factor",
    "first_obs_ema",
    "sma_series",
    "wma_series",
    "hma_series",
    "rolling_max_series",
    "rolling_min_series",
    "pivot_high_series",
    "pivot_low_series",
    "forward_fill",
    "ohlcv_arrays",
    # 10 个因子 series（向量化，供 KNN feature_matrix 切片）
    "lrsd_series",
    "gpi_series",
    "vap_series",
    "pdbb_series",
    "pivot_series",
    "ami_series",
    "atr2_series",
    "msfvg_series",
    "ai_st_series",
    "dmha_series",
]
