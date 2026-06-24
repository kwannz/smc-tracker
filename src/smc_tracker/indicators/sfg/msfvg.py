"""sfg/msfvg.py — SFG Market Structure & FVG (MSFVG) 反转簇因子。

移植来源：
  SFG - Market Structure & FVG.pine (Pine Script v5)
  sfg_indicators_rs/src/continuous_factors.rs:334-365    (factor_msfvg)
  sfg_indicators_rs/src/continuous_factors.rs:128-139    (level_factor helper)
  sfg_indicators_rs/src/indicators/market_structure_fvg.rs:350-437  (FVG detection/fill)

算法路径（仅因子路径，非 BOS/CHoCH/pivot 路径）：
  1. warmup guard: n < 2*swing_size+1 → 全部 NaN
  2. FVG 事件检测（causal，每根 bar）：
       bull_event = h[i-3] < l[i-1]
       bear_event = l[i-3] > h[i-1]
  3. zone 创建（FIFO ring，cap = fvg_history+1）
  4. zone fill/shrink（当前 bar low/high），shrink_mitigated=True 时部分填充=收缩
  5. nearest-zone 选择（closest midpoint to current close）
  6. 因子标量（factor_msfvg）→ clamp[-1,1]

符号约定（reversal cluster）：
  +1 = 价格在支撑区（bull FVG 在下方） → 预期看涨反转
  -1 = 价格在阻力区（bear FVG 在上方） → 预期看跌反转
  NaN = fail-closed（无可用 zone，在聚合器中视为「弃权」）

No-repaint / No-lookahead 保证：
  FVG 事件只用 h[i-3]/l[i-1]/l[i-3]/h[i-1]（均 < i）；
  fill/shrink 只用当前 bar i 的 low/high；
  nearest 选择只用当前 close c[i]；
  zone 状态机严格前向增量（FIFO + FIFO evict），bar i 的结果在 bar i 写定，
  后续 bar 只能删 zone / shrink zone，不会修改已发射的 series[i] 值。

CAVEAT（短序列退化）：
  若序列中从未出现 FVG 事件（例如纯平稳序列、低波动横盘），所有 series 值均为
  NaN（fail-closed）。这是正常行为而非 bug — 表示当前无有效 FVG 支撑/阻力区。
  在 KNN 特征聚合层 NaN 会被 skip（continuous_factors.rs:468-506 语义一致）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from smc_tracker.util import to_float

from ._common import clamp as _clamp_arr, level_factor, ohlcv_arrays


# ─────────────────────────────────────────────────────────────────────────────
# 内部数据结构：FVG Zone
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _FvgZone:
    """单个活跃 FVG 区域（mutable，随 bar 前向更新）。

    字段与 market_structure_fvg.rs zone struct 对应：
      top, bottom: 当前边界（shrink 后会变化）
      bull: True = 看涨 FVG，False = 看跌 FVG
    """
    top: float
    bottom: float
    bull: bool


# ─────────────────────────────────────────────────────────────────────────────
# 内部辅助：因子标量计算（暴露给测试用于 golden parity）
# ─────────────────────────────────────────────────────────────────────────────

def _factor_scalar(
    close: float,
    bull_top: float,
    bull_bot: float,
    bear_top: float,
    bear_bot: float,
) -> float:
    """MSFVG 因子标量（continuous_factors.rs:334-365）。

    Args:
        close:    当前收盘价
        bull_top: 最近 bull FVG zone 上边界（NaN = 无 bull zone）
        bull_bot: 最近 bull FVG zone 下边界
        bear_top: 最近 bear FVG zone 上边界（NaN = 无 bear zone）
        bear_bot: 最近 bear FVG zone 下边界

    Returns:
        float in [-1, 1]；NaN 表示 fail-closed（无可用 zone 或退化输入）。

    公式（spec factor_formula）：
      have_bull = finite(bull_top, bull_bot, close) AND bull_top > bull_bot
      have_bear = finite(bear_top, bear_bot, close) AND bear_top > bear_bot

      CASE both:
        f = level_factor(close, SL=bull_top, RL=bear_bot)
          = clamp(((bull_top+bear_bot)/2 - close) / ((bear_bot-bull_top)/2))

      CASE bull only:
        half = max(bull_top - bull_bot, 1e-10)
        f = clamp((bull_top - close + half) / (2*half))

      CASE bear only:
        half = max(bear_top - bear_bot, 1e-10)
        f = clamp(-(close - bear_bot + half) / (2*half))

      CASE neither: NaN
    """
    # 有效性检查
    have_bull = (
        math.isfinite(bull_top) and math.isfinite(bull_bot)
        and math.isfinite(close) and bull_top > bull_bot
    )
    have_bear = (
        math.isfinite(bear_top) and math.isfinite(bear_bot)
        and math.isfinite(close) and bear_top > bear_bot
    )

    if have_bull and have_bear:
        # both-zones: level_factor(close, SL=bull_top, RL=bear_bot)
        # half_range = (bear_bot - bull_top) / 2
        half_range = (bear_bot - bull_top) / 2.0
        if half_range <= 0:
            return math.nan  # 退化（bear_bot <= bull_top）
        mid = (bull_top + bear_bot) / 2.0
        raw = (mid - close) / half_range
        return float(max(-1.0, min(1.0, raw)))

    elif have_bull:
        # bull-only
        half = max(bull_top - bull_bot, 1e-10)
        raw = (bull_top - close + half) / (2.0 * half)
        return float(max(-1.0, min(1.0, raw)))

    elif have_bear:
        # bear-only
        half = max(bear_top - bear_bot, 1e-10)
        raw = -(close - bear_bot + half) / (2.0 * half)
        return float(max(-1.0, min(1.0, raw)))

    else:
        # neither: fail-closed
        return math.nan


# ─────────────────────────────────────────────────────────────────────────────
# 主函数：msfvg_series
# ─────────────────────────────────────────────────────────────────────────────

def msfvg_series(
    candles: list[Any],
    *,
    swing_size: int = 20,
    fvg_history: int = 7,
    shrink_mitigated: bool = True,
    bos_wicks_mode: bool = False,
    choch: bool = True,
) -> np.ndarray:
    """MSFVG 反转簇因子序列（长度 n，warmup=NaN）。

    Args:
        candles:         K 线列表，每项需有 .o/.h/.l/.c/.v 属性。
        swing_size:      Pivot arm（Pine swingSize=20）。仅影响 **整批** warmup guard
                         (n < 2*swing_size+1 时返回全 NaN)，因子路径本身不依赖 pivot。
        fvg_history:     活跃 FVG 环形队列容量 = fvg_history+1（默认 8 个 zone）。
        shrink_mitigated: True = 部分填充时 zone 边界收缩；False = 不收缩（保留原始 zone）。
        bos_wicks_mode:  仅影响 BOS/CHoCH（非因子路径），此参数保留供完整性，不影响因子。
        choch:           同上，保留供完整性。

    Returns:
        np.ndarray, shape=(n,), dtype=float64。
        warmup guard: n < 2*swing_size+1 → 全部 NaN（整批拒绝，对齐 Rust market_structure_fvg.rs:207-209）。
        整批 n 满足时，每根 bar 按「是否有存活 FVG zone」决定 finite/NaN（fail-closed）：
          - 早期 bar（i<3）无法触发 FVG 事件，active_zones 为空 → NaN。
          - i>=3 后如果有 FVG 事件且 zone 存活 → 可在任意 bar（包括 i < 2*swing_size）发射有限值。
        这与 Rust 行为对齐：per-row 不施加额外 warmup mask，只有整批守卫。

    CAVEAT（短序列退化）：
        纯平稳/低波动序列不触发 FVG 事件，所有值均 NaN。这是正常行为（fail-closed），
        在聚合器中 NaN 因子被跳过，不影响其他因子的贡献。
    """
    n = len(candles)
    out = np.full(n, math.nan)

    if n == 0:
        return out

    # 整批 warmup guard（对齐 Rust market_structure_fvg.rs:207-209）：
    # n < 2*swing_size+1 时不发射任何列（整批返回全 NaN）。
    # 注：per-row warmup mask 已移除（见 Rust 行为分析），每 bar 按 active_zones 是否非空决定输出。
    min_bars = 2 * swing_size + 1

    if n < min_bars:
        return out  # 全 NaN

    # 提取 OHLCV 数组（util.to_float 守卫，拒 NaN/inf/None）
    arrs = ohlcv_arrays(candles)
    h: np.ndarray = arrs["h"]
    l: np.ndarray = arrs["l"]
    c: np.ndarray = arrs["c"]

    # FVG ring buffer（FIFO，cap = fvg_history+1）
    cap: int = fvg_history + 1
    active_zones: list[_FvgZone] = []

    # 逐 bar 前向增量处理（严格 causal）
    for i in range(n):
        # ── STEP 1: FVG 事件检测（使用 i-3, i-1 两个过去 bar）────────────────
        # 需要 i >= 3 才能访问 h[i-3] / l[i-3] / h[i-1] / l[i-1]
        if i >= 3:
            h_i3 = h[i - 3]  # h[i-3]
            l_i3 = l[i - 3]  # l[i-3]
            h_i1 = h[i - 1]  # h[i-1]
            l_i1 = l[i - 1]  # l[i-1]

            # bull FVG: h[i-3] < l[i-1]（且两者均有限）
            if math.isfinite(h_i3) and math.isfinite(l_i1) and h_i3 < l_i1:
                # zone: top=l[i-1], bottom=h[i-3]
                zone = _FvgZone(top=l_i1, bottom=h_i3, bull=True)
                active_zones.append(zone)
                # FIFO evict oldest if over cap
                while len(active_zones) > cap:
                    active_zones.pop(0)

            # bear FVG: l[i-3] > h[i-1]（且两者均有限）
            if math.isfinite(l_i3) and math.isfinite(h_i1) and l_i3 > h_i1:
                # zone: top=l[i-3], bottom=h[i-1]
                zone = _FvgZone(top=l_i3, bottom=h_i1, bull=False)
                active_zones.append(zone)
                # FIFO evict oldest if over cap
                while len(active_zones) > cap:
                    active_zones.pop(0)

        # ── STEP 2: zone fill/shrink（使用当前 bar i 的 low/high）─────────────
        low_i = l[i]
        high_i = h[i]

        # 处理 zone fill/shrink，构建存活 zone 列表
        surviving: list[_FvgZone] = []
        for z in active_zones:
            if z.bull:
                # bull zone 填充：low[i] <= z.bottom => fully filled, DROP
                if math.isfinite(low_i) and low_i <= z.bottom:
                    pass  # 丢弃
                elif math.isfinite(low_i) and low_i < z.top:
                    # 部分 mitigation
                    if shrink_mitigated:
                        z.top = low_i  # 收缩 top 到 low[i]
                    surviving.append(z)
                else:
                    surviving.append(z)
            else:
                # bear zone 填充：high[i] >= z.top => fully filled, DROP
                if math.isfinite(high_i) and high_i >= z.top:
                    pass  # 丢弃
                elif math.isfinite(high_i) and high_i > z.bottom:
                    # 部分 mitigation
                    if shrink_mitigated:
                        z.bottom = high_i  # 收缩 bottom 到 high[i]
                    surviving.append(z)
                else:
                    surviving.append(z)
        active_zones = surviving

        # ── STEP 3: nearest-zone 选择（closest midpoint to close[i]）────────
        # 注：无 per-row warmup mask（对齐 Rust market_structure_fvg.rs:350-512 行为）。
        # 早期 bar（i<3）active_zones 始终为空 → STEP 4 _factor_scalar 返回 NaN（fail-closed）。
        # 整批 warmup guard 已在 n < min_bars 处理，per-row 不再重复。
        close_i = c[i]
        if not math.isfinite(close_i):
            continue  # close 无效 => NaN

        best_bull_top = math.nan
        best_bull_bot = math.nan
        best_bull_dist = math.inf

        best_bear_top = math.nan
        best_bear_bot = math.nan
        best_bear_dist = math.inf

        for z in active_zones:
            if not (math.isfinite(z.top) and math.isfinite(z.bottom) and z.top > z.bottom):
                continue
            mid = (z.top + z.bottom) / 2.0
            dist = abs(mid - close_i)
            if z.bull:
                if dist < best_bull_dist:
                    best_bull_dist = dist
                    best_bull_top = z.top
                    best_bull_bot = z.bottom
            else:
                if dist < best_bear_dist:
                    best_bear_dist = dist
                    best_bear_top = z.top
                    best_bear_bot = z.bottom

        # ── STEP 4: 因子标量计算 ───────────────────────────────────────────────
        f = _factor_scalar(close_i, best_bull_top, best_bull_bot,
                           best_bear_top, best_bear_bot)
        out[i] = f  # NaN = fail-closed（_factor_scalar 已处理）

    return out


# ─────────────────────────────────────────────────────────────────────────────
# 标量包装：msfvg_factor（供末值消费 + parity 测试）
# ─────────────────────────────────────────────────────────────────────────────

def msfvg_factor(
    candles: list[Any],
    *,
    swing_size: int = 20,
    fvg_history: int = 7,
    shrink_mitigated: bool = True,
    bos_wicks_mode: bool = False,
    choch: bool = True,
) -> float | None:
    """返回 msfvg_series 最后一个有限值（供 KNN 特征消费）。

    不足 warmup → None。全 NaN（无活跃 FVG zone）→ None。
    返回值保证：finite float in [-1, 1]。
    """
    s = msfvg_series(
        candles,
        swing_size=swing_size,
        fvg_history=fvg_history,
        shrink_mitigated=shrink_mitigated,
        bos_wicks_mode=bos_wicks_mode,
        choch=choch,
    )
    # 找最后一个有限值
    finite_mask = np.isfinite(s)
    if not np.any(finite_mask):
        return None
    # 反向找最后一个有限值的索引
    last_idx = int(np.where(finite_mask)[0][-1])
    val = float(s[last_idx])
    return val
