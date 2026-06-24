"""sfg/lrsd.py — SFG LRSD LinReg & Supply/Demand Zones 反转簇因子。

算法来源：SFG - LinReg & Supply Demand Zone.pine (Pine Script v5)
          + sfg_indicators_rs/src/indicators/linreg_supply_demand.rs
          + sfg_indicators_rs/src/continuous_factors.rs

核心：5-bar 非对称 Williams 分形 + 高量门控 → 供需区前向填充 → level_factor 归一化

sign_convention（reversal group）：
  +1 = close 在/低于支撑区底部 → 预期向上反转（bullish bias）
  -1 = close 在/高于压力区顶部 → 预期向下反转（bearish bias）
   0 = close 在区间中点
  NaN = fail-closed（区间未确立 / 退化）

重要滞后声明（CLAUDE.md §二 诚实标注）：
  - 3-bar confirmation lag：分形在 bar i 确认，但区间锚定在 bar i-3（中心）
    这是 Williams 分形的标准滞后，非 look-ahead
  - 使用本因子作 KNN 特征时，请仅在 closed bar 评估，并文档记录该滞后
  - 如序列极短（< vol_ma_len + 5 + 3 + 1 根），区间无法确立，退化为全 NaN
    INERT 参数（pivot_len, volume_threshold）在默认路径不参与计算，见 spec。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from ._common import (
    level_factor,
    ohlcv_arrays,
    forward_fill,
    sma_series,
)
from smc_tracker.util import to_float


# ─────────────────────────────────────────────────────────────────────────────
# 内部：分形检测 + 区间计算
# ─────────────────────────────────────────────────────────────────────────────

def _compute_zone_arrays(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    v: np.ndarray,
    vol_ma_len: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """计算 4 个区间边界序列（前向填充，因果）。

    算法：
      STEP 1: vol_ma = SMA(volume, vol_ma_len)
      STEP 2: 5-bar 非对称 Williams 分形 + volume gate
        up_fractal(i):  h[i-2]<h[i-1]<h[i-0..以i-3为中心写法]
        实际规范写法：
          up_fractal(i)   = h[i-3]>h[i-4] AND h[i-4]>h[i-5]
                            AND h[i-2]<h[i-3] AND h[i-1]<h[i-2]
                            AND vgate(i)
          down_fractal(i) = l[i-3]<l[i-4] AND l[i-4]<l[i-5]
                            AND l[i-2]>l[i-3] AND l[i-1]>l[i-2]
                            AND vgate(i)
          vgate(i) = vol_ma[i-3] is finite AND volume[i-3] > vol_ma[i-3]
      STEP 3: 区间赋值（确认 bar i → pivot 在 i-3）
        up_fractal → res_top = h[i-3], res_bot = max(o[i-3], c[i-3])
        down_fractal → sup_bot = l[i-3], sup_top = min(o[i-3], c[i-3])
      STEP 4: forward_fill

    Returns:
        (res_top, res_bot, sup_top, sup_bot) — 均为 shape (n,) 数组，前向填充
    """
    n = len(o)

    # STEP 1：滚动量能 MA
    vol_ma = sma_series(v, vol_ma_len)

    # STEP 2 & 3：分形扫描 + 区间赋值
    # 用 nan 初始化（只在分形确认时写入）
    res_top_raw = np.full(n, np.nan)
    res_bot_raw = np.full(n, np.nan)
    sup_bot_raw = np.full(n, np.nan)
    sup_top_raw = np.full(n, np.nan)

    # 分形确认需要：i-5..i-1 已 closed → 最早 i=5
    # (rust :162-189 从 i=5 开始扫描，bar 0..4 无 fractal 可能)
    for i in range(5, n):
        # volume gate: volume[i-3] > vol_ma[i-3]（严格大于）
        vm3 = vol_ma[i - 3]
        if not math.isfinite(vm3):
            continue
        vgate = v[i - 3] > vm3

        if not vgate:
            continue

        # up_fractal：中心 = i-3（pivot high）
        # 形状：h[i-5]<h[i-4]<h[i-3] AND h[i-2]<h[i-3] AND h[i-1]<h[i-2]
        # （注意：h[i-2]<h[i-3] 和 h[i-1]<h[i-2] 是右侧 2 根降序）
        if (
            math.isfinite(h[i - 5]) and math.isfinite(h[i - 4])
            and math.isfinite(h[i - 3]) and math.isfinite(h[i - 2])
            and math.isfinite(h[i - 1])
            and h[i - 3] > h[i - 4]   # 中心比左邻高
            and h[i - 4] > h[i - 5]   # 左邻比左左高（严格递升）
            and h[i - 2] < h[i - 3]   # 右邻比中心低
            and h[i - 1] < h[i - 2]   # 右右邻比右邻低（严格递降）
        ):
            # 写入确认 bar i 的位置
            res_top_raw[i] = h[i - 3]
            res_bot_raw[i] = max(o[i - 3], c[i - 3])

        # down_fractal：中心 = i-3（pivot low）
        # 形状：l[i-5]>l[i-4]>l[i-3] AND l[i-2]>l[i-3] AND l[i-1]>l[i-2]
        if (
            math.isfinite(l[i - 5]) and math.isfinite(l[i - 4])
            and math.isfinite(l[i - 3]) and math.isfinite(l[i - 2])
            and math.isfinite(l[i - 1])
            and l[i - 3] < l[i - 4]   # 中心比左邻低
            and l[i - 4] < l[i - 5]   # 左邻比左左低（严格递降）
            and l[i - 2] > l[i - 3]   # 右邻比中心高
            and l[i - 1] > l[i - 2]   # 右右邻比右邻高（严格递升）
        ):
            sup_bot_raw[i] = l[i - 3]
            sup_top_raw[i] = min(o[i - 3], c[i - 3])

    # STEP 4：前向填充（携带最近确认值，NaN 保留至无历史段）
    res_top_ff = forward_fill(res_top_raw)
    res_bot_ff = forward_fill(res_bot_raw)
    sup_top_ff = forward_fill(sup_top_raw)
    sup_bot_ff = forward_fill(sup_bot_raw)

    return res_top_ff, res_bot_ff, sup_top_ff, sup_bot_ff


# ─────────────────────────────────────────────────────────────────────────────
# 公开 API
# ─────────────────────────────────────────────────────────────────────────────

def lrsd_series(
    candles: list[Any],
    length: int = 100,
    vol_ma_len: int = 6,
) -> np.ndarray:
    """SFG LRSD 反转因子序列。

    Args:
        candles: K 线列表，每项须有 .o/.h/.l/.c/.v 属性。
        length:  LinReg 通道窗口长度。仅用作 warmup 文档参数，
                 **不参与反转因子数学计算**（见 spec algorithm_steps STEP 0）。
                 本实现用"分形可确认的最小根数"作内部 warmup 守卫，
                 不强制 n<length → 全 NaN（KNN 场景宽松，见 spec STEP 0 注释）。
        vol_ma_len: 量能 SMA 长度（分形量能门控，默认 6）。

    Returns:
        shape (n,) 的 float64 数组：
          - warmup 段（无区间）→ nan（fail-closed sentinel）
          - 有效段 → [-1, 1] 有限值

    注意：
        - 3-bar confirmation lag（Williams 分形标准滞后，非 look-ahead）
        - 若序列极短 (< vol_ma_len + 4) 将全部退化为 NaN
        - 此因子仅在 closed bar 评估有意义；intra-bar 值不稳定
    """
    n = len(candles)
    if n == 0:
        return np.array([], dtype=float)

    # 提取 OHLCV（to_float 守卫：NaN/inf/None → 0.0）
    arrs = ohlcv_arrays(candles)
    o: np.ndarray = arrs["o"]
    h: np.ndarray = arrs["h"]
    l: np.ndarray = arrs["l"]
    c: np.ndarray = arrs["c"]
    v: np.ndarray = arrs["v"]

    # 计算 4 个区间边界（前向填充，因果）
    res_top, res_bot, sup_top, sup_bot = _compute_zone_arrays(
        o, h, l, c, v, vol_ma_len
    )

    # STEP 6-8：validity gate + level_factor + clamp
    # have_sup = sup_top, sup_bot 均有限 AND sup_top >= sup_bot
    # have_res = res_top, res_bot 均有限 AND res_top >= res_bot
    # factor = level_factor(close, lower=sup_bot, upper=res_top)
    #   即：(mid_ref - close) / half_range，mid = (sup_bot + res_top)/2
    #   半宽 = (res_top - sup_bot)/2
    #   （使用宽包络：outer bottom of demand zone 和 outer top of supply zone）

    have_sup = (
        np.isfinite(sup_top)
        & np.isfinite(sup_bot)
        & (sup_top >= sup_bot)
    )
    have_res = (
        np.isfinite(res_top)
        & np.isfinite(res_bot)
        & (res_top >= res_bot)
    )
    valid_zones = have_sup & have_res

    # 对有效位置计算 level_factor，无效位置保持 nan
    # level_factor 需要 close, lower=sup_bot, upper=res_top
    # 先构造 "masked" 数组：无效位置填 nan（level_factor 会对 nan 输出 nan）
    safe_sup_bot = np.where(valid_zones, sup_bot, np.nan)
    safe_res_top = np.where(valid_zones, res_top, np.nan)

    result = level_factor(c, safe_sup_bot, safe_res_top)

    return result


def lrsd_factor(
    candles: list[Any],
    length: int = 100,
    vol_ma_len: int = 6,
) -> float | None:
    """SFG LRSD 反转因子标量包装（供末值消费 + parity 测试）。

    Returns:
        series 中最后一个有限值（float），或 None（不足 warmup / 全 NaN）。
    """
    series = lrsd_series(candles, length=length, vol_ma_len=vol_ma_len)
    if len(series) == 0:
        return None
    finite_mask = np.isfinite(series)
    if not np.any(finite_mask):
        return None
    return float(series[finite_mask][-1])
