"""sfg/dmha.py — SFG DMHA（Dynamic MACD + Heikin Ashi）趋势因子，纯 numpy 移植。

算法来源：SFG - Dynamic MACD + Heikin Ashi.pine（Pine Script v5）
Rust 参照：src/indicators/dynamic_macd_ha.rs + src/continuous_factors.rs

因子角色：trend 簇（非反转簇）。
符号约定（TREND/MOMENTUM，非反转）：
  +1.0 = bullish — MACD 序列 HA 蜡烛 green（ha_close > ha_open），上涨动量
  -1.0 = bearish — MACD 序列 HA 蜡烛 red  （ha_close < ha_open），下跌动量
   0.0 = doji（ha_close == ha_open），中性
   NaN  = fail-closed：数据不足 warmup 或输入非有限

算法流程（对应 sfg_spec_dmha.json algorithm_steps）：
  STEP1  gf(src, length)  — Ehlers 风格 IIR 通用滤波器（seed=0.0，NaN carry-forward）
  STEP2  fast_gf=gf(src,12), slow_gf=gf(src,25), range_gf=gf(h-l,25)；range_gf=0→毒化 NaN
  STEP3  raw_macd = (fast_gf - slow_gf) / range_gf * 100
  STEP4  macd = HMA(raw_macd, smooth_len=6)
  STEP5  signal = EMA(macd, signal_length=6)  [仅供 hist；不参与趋势因子]
  STEP6  hist = macd - signal                  [同上]
  STEP7  HA on MACD series：open_macd=shift1, high/low=nanmax/nanmin(macd,macd[1]), close_macd=macd
         ha_close = (open_macd + high_macd + low_macd + close_macd) / 4
  STEP8  ha_open 迭代种子（递推）
  STEP9  ha_bull / ha_bear 严格比较
  STEP10 dmha_state = +1/0/-1/NaN
  STEP11 factor = clamp(dmha_state)（clamp 对 {-1,0,+1} 为恒等，NaN→fail-closed）

WARMUP 诚实标注：
  HMA(6) 首个非 NaN 在 idx=6；HA 需要 macd[i-1]，故 ha_close 首个非 NaN 在 idx=7；
  idx=8 是文档化的结构性 doji 重置帧（gf 未收敛 + WMA 4-lane FMA ULP 差）。
  参考规格建议丢弃前 220 根（生产 1000 根）才完全信任；本模块忠实输出每根值，
  由调用方按需丢弃 warmup 段。
  短序列（如全 < 9 根）：在 range_gf 长通道窗口下可能全为 NaN，已诚实标注。

无前视保证：gf/WMA/HMA 均为 trailing，ha 使用 macd[i-1]（过去帧），完全因果。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from ._common import clamp, hma_series, first_obs_ema, ohlcv_arrays


# ─────────────────────────────────────────────────────────────────────────────
# STEP1：gf — Ehlers 通用 IIR 滤波器
# ─────────────────────────────────────────────────────────────────────────────

def _gf_seeded(src: np.ndarray, length: int) -> np.ndarray:
    """Ehlers 风格通用 IIR 滤波器（generic filter），seed=0.0，NaN carry-forward。

    公式（rs:109-121）：
      beta  = (1 - cos(2*pi/length)) / 1.0
      alpha = -beta + sqrt(beta^2 + 2*beta)
      last  = 0.0（初始种子，Pine `var filter = 0.0`）
      每步: x = src[i] if finite else last
             last = alpha*x + (1-alpha)*last
    输出等长，无 warmup NaN（从 bar 0 起即有输出，但早期未收敛）。
    """
    n = len(src)
    out = np.empty(n, dtype=float)
    if n == 0:
        return out

    # 计算 alpha（只依赖 length）
    length_eff = max(2, length)  # length < 2 无意义，防止 div-by-zero
    two_pi_over_len = 2.0 * math.pi / length_eff
    beta = (1.0 - math.cos(two_pi_over_len)) / 1.0
    alpha = -beta + math.sqrt(beta * beta + 2.0 * beta)
    one_minus_alpha = 1.0 - alpha

    last = 0.0  # Pine: var filter = 0.0（seed at 0，not NaN）
    for i in range(n):
        v = src[i]
        # NaN carry-forward（Pine nz(src[i], filter_prev)）
        x = v if math.isfinite(v) else last
        last = alpha * x + one_minus_alpha * last
        out[i] = last

    return out


# ─────────────────────────────────────────────────────────────────────────────
# 主系列函数
# ─────────────────────────────────────────────────────────────────────────────

def dmha_series(
    candles: list[Any],
    fast_length: int = 12,
    slow_length: int = 25,
    smooth_len: int = 6,
    signal_length: int = 6,
) -> np.ndarray:
    """DMHA 趋势因子序列：长度 n，warmup 段为 NaN，有限值 ∈ {-1.0, 0.0, +1.0}。

    Args:
        candles:       K 线列表，每项需有 .h/.l/.c 属性。
        fast_length:   快线 gf 长度（默认 12）。
        slow_length:   慢线 gf 长度（默认 25，同时用于 range_gf）。
        smooth_len:    HMA 平滑长度（默认 6）。
        signal_length: EMA 信号线长度（默认 6，仅影响 hist，不影响趋势因子）。

    Returns:
        np.ndarray 长度 n，dtype=float64。
        前 warmup 段为 NaN（HMA(6) warmup ~7 根，HA 再+1）。
        NaN = fail-closed（数据不足或输入非有限）。

    SIGN CONVENTION（趋势动量，非反转）：
        +1.0 → bullish MACD 动量（上涨）
        -1.0 → bearish MACD 动量（下跌）
         0.0 → doji
         NaN → 不足 warmup / 数据缺失

    WARMUP 诚实标注：
        规格建议 >=220 根丢弃期（生产 >=1000），短序列（<10 根）几乎全 NaN。
        idx=8 存在文档化结构性 doji（gf 未收敛 + ULP FMA 差）；已在 warmup 范围内。
    """
    if not candles:
        return np.array([], dtype=float)

    arrs = ohlcv_arrays(candles)
    h: np.ndarray = arrs["h"]
    l: np.ndarray = arrs["l"]
    c: np.ndarray = arrs["c"]
    n = len(c)

    # ── STEP1/2：HLCC4 + hl + gf × 3 ────────────────────────────────────────
    # 价格源固定为 HLCC4（Pine 结构性变更，不可调）
    src = (h + l + c + c) / 4.0   # HLCC4 = (h+l+c+c)/4
    hl = h - l                      # bar range

    fast_gf_arr = _gf_seeded(src, fast_length)
    slow_gf_arr = _gf_seeded(src, slow_length)
    range_gf_arr = _gf_seeded(hl, slow_length)

    # STEP2：range_gf == 0.0 → 毒化 NaN（防止 div-by-zero）
    range_gf_safe = np.where(range_gf_arr == 0.0, np.nan, range_gf_arr)

    # ── STEP3：raw_macd ────────────────────────────────────────────────────────
    # raw_macd[i] = (fast_gf - slow_gf) / range_gf * 100，range_gf 非有限→NaN
    raw_macd = np.full(n, np.nan)
    valid_range = np.isfinite(range_gf_safe)
    raw_macd[valid_range] = (
        (fast_gf_arr[valid_range] - slow_gf_arr[valid_range])
        / range_gf_safe[valid_range]
        * 100.0
    )

    # ── STEP4：macd = HMA(raw_macd, smooth_len) ───────────────────────────────
    # HMA(s,L) = WMA(2*WMA(s,L//2) - WMA(s,L), round(sqrt(L)))
    # L=6: half=3, full=6, root=round(sqrt(6))=2
    # 首个非 NaN 在 idx = (full-1)+(root-1) = 5+1 = 6
    macd = hma_series(raw_macd, smooth_len)

    # ── STEP5/6：signal 和 hist（仅为完整性，不影响趋势因子）─────────────────
    # signal = EMA(macd, signal_length) 首观察种子
    # 这里使用 _common.first_obs_ema（与规格语义一致）
    # hist = macd - signal
    # （不在此函数返回，但可在扩展时使用）

    # ── STEP7：HA on MACD series ──────────────────────────────────────────────
    # open_macd[i] = macd[i-1]（shift1，i=0 时 NaN）
    # high_macd[i] = nanmax(macd[i], macd[i-1])
    # low_macd[i]  = nanmin(macd[i], macd[i-1])
    # close_macd[i] = macd[i]

    # 构建 shift1（前一根 macd）
    macd_prev = np.empty(n, dtype=float)
    macd_prev[0] = np.nan
    macd_prev[1:] = macd[:-1]

    # close_macd = macd（当前帧）
    close_macd = macd.copy()

    # open_macd = macd[i-1]（shift1）
    open_macd = macd_prev.copy()

    # high/low macd：nanmax/nanmin between macd[i] and macd[i-1]
    # 若任一为 NaN 则取另一个；若两者都有限则取 max/min
    # 注：规格 rs:304-328 写的是 max(macd[i], macd[i-1]) with NaN handling
    high_macd = np.full(n, np.nan)
    low_macd = np.full(n, np.nan)
    both_finite = np.isfinite(macd) & np.isfinite(macd_prev)
    cur_finite_only = np.isfinite(macd) & ~np.isfinite(macd_prev)
    prev_finite_only = ~np.isfinite(macd) & np.isfinite(macd_prev)

    high_macd[both_finite] = np.maximum(macd[both_finite], macd_prev[both_finite])
    high_macd[cur_finite_only] = macd[cur_finite_only]
    high_macd[prev_finite_only] = macd_prev[prev_finite_only]

    low_macd[both_finite] = np.minimum(macd[both_finite], macd_prev[both_finite])
    low_macd[cur_finite_only] = macd[cur_finite_only]
    low_macd[prev_finite_only] = macd_prev[prev_finite_only]

    # ha_close = (open_macd + high_macd + low_macd + close_macd) / 4
    # NaN if any term NaN
    ha_close = np.full(n, np.nan)
    all_finite_for_close = (
        np.isfinite(open_macd)
        & np.isfinite(high_macd)
        & np.isfinite(low_macd)
        & np.isfinite(close_macd)
    )
    ha_close[all_finite_for_close] = (
        open_macd[all_finite_for_close]
        + high_macd[all_finite_for_close]
        + low_macd[all_finite_for_close]
        + close_macd[all_finite_for_close]
    ) / 4.0

    # ── STEP8：ha_open 迭代种子（递推）──────────────────────────────────────
    # 规格（rs:330-346）：
    #   if i==0 OR ha_open[i-1] is NaN:
    #     if open_macd & close_macd both finite: ha_open = (open_macd+close_macd)/2
    #     elif close_macd finite: ha_open = close_macd
    #     else: NaN
    #   else: ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2
    ha_open = np.full(n, np.nan)
    for i in range(n):
        prev_ha_open = ha_open[i - 1] if i > 0 else np.nan
        if i == 0 or not math.isfinite(prev_ha_open):
            # 初始种子或上一帧 ha_open 为 NaN：重新种子
            o_fin = math.isfinite(open_macd[i])
            c_fin = math.isfinite(close_macd[i])
            if o_fin and c_fin:
                ha_open[i] = (open_macd[i] + close_macd[i]) / 2.0
            elif c_fin:
                ha_open[i] = close_macd[i]
            # else: 保持 NaN
        else:
            # 正常递推
            prev_ha_close = ha_close[i - 1]
            if math.isfinite(prev_ha_close):
                ha_open[i] = (prev_ha_open + prev_ha_close) / 2.0
            else:
                # ha_close[i-1] 为 NaN，carry-forward ha_open
                ha_open[i] = prev_ha_open

    # ── STEP9/10：dmha_state ──────────────────────────────────────────────────
    # ha_bull = ha_close > ha_open（严格；NaN 比较为 False）
    # ha_bear = ha_close < ha_open（严格；NaN 比较为 False）
    # dmha_state: NaN if ha_open or ha_close is NaN; else +1/-1/0
    dmha_state = np.full(n, np.nan)
    both_valid = np.isfinite(ha_open) & np.isfinite(ha_close)
    ha_bull = ha_close > ha_open   # NaN → False（numpy 严格比较）
    ha_bear = ha_close < ha_open

    # 仅在 both_valid 时才赋值
    dmha_state[both_valid & ha_bull] = 1.0
    dmha_state[both_valid & ha_bear] = -1.0
    dmha_state[both_valid & ~ha_bull & ~ha_bear] = 0.0  # doji

    # ── STEP11：factor = clamp(dmha_state) ───────────────────────────────────
    # 由于 dmha_state ∈ {-1,0,+1,NaN}，clamp 对有限值为恒等，NaN→fail-closed
    result = clamp(dmha_state)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 标量包装：末值消费
# ─────────────────────────────────────────────────────────────────────────────

def dmha_factor(
    candles: list[Any],
    fast_length: int = 12,
    slow_length: int = 25,
    smooth_len: int = 6,
    signal_length: int = 6,
) -> float | None:
    """DMHA 因子末值（供 KNN 特征 + parity 测试）。

    返回 series 中最后一个有限值（float ∈ {-1.0, 0.0, +1.0}），
    若序列全为 NaN（不足 warmup）→ 返回 None。

    Args:
        candles: 同 dmha_series。
        fast_length / slow_length / smooth_len / signal_length: 同 dmha_series。

    Returns:
        float ∈ {-1.0, 0.0, +1.0} 或 None（不足 warmup）。
    """
    series = dmha_series(
        candles,
        fast_length=fast_length,
        slow_length=slow_length,
        smooth_len=smooth_len,
        signal_length=signal_length,
    )
    if len(series) == 0:
        return None
    # 取最后一个有限值
    finite_mask = np.isfinite(series)
    if not np.any(finite_mask):
        return None
    # 逆序找最后一个有限值
    for i in range(len(series) - 1, -1, -1):
        if math.isfinite(series[i]):
            return float(series[i])
    return None
