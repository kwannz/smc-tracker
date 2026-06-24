"""sfg/ai_st.py — SFG AI SuperTrend 趋势因子（ai_st / ai_supertrend）。

因子分组：TREND（趋势簇）
输出范围：[-1, +1]，NaN = 暖机期 / fail-closed
Sign convention（趋势簇，与反转簇相反）：
  +1 = 上涨动量 / 多头趋势
  -1 = 下跌动量 / 空头趋势

算法来源：
  - SFG - AI SuperTrend.pine（Pine Script v5）
  - sfg_indicators_rs/src/indicators/ai_supertrend.rs
  - 规格文件 sfg_spec_ai_st.json（含 parity_notes / lookahead_risk）

无前视/不重绘（已验证）：
  - SuperTrend ratchet 仅读 prev state + current bar
  - KNN 窗口严格 [max(0, i+1-n_eff) .. i]（含当前）
  - k=1 退化：self-match 权重 1e6，等价于 identity map，非前视

诚实标注：
  - 此因子在 k=1（默认）时等价于阈值判断 price_wma > st_wma，统计预测力 ~50%随机
  - 短 K 线序列（< length + st_len - 1 = 109 根）全返回 NaN；请勿零填充
  - KNN 窗口含自身（self-included），k=1 时 KNN 完全退化（k 参数无效）
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from smc_tracker.util import to_float
from ._common import clamp, ohlcv_arrays, wma_series


# ─────────────────────────────────────────────────────────────────────────────
# 低层数值原语（与 Rust primitives.rs SMA-seeded 语义一致）
# ─────────────────────────────────────────────────────────────────────────────

def _sma_seeded_ema(x: np.ndarray, length: int) -> np.ndarray:
    """SMA-seeded EMA（Rust/Pine 语义）。

    seed：在第 `length` 个有限 bar 处（索引 length-1），seed = mean(x[0..length-1])。
    之后：out[i] = a * x[i] + (1-a) * out[i-1]，a = 2/(length+1)。
    NaN 输入：carry-forward 前值（与 Rust NaN-skip 一致）。
    warmup（seed 前）→ NaN。

    spec parity oracle: ema([10,20,30], 2) = [NaN, 15, 25]
    """
    n = len(x)
    out = np.full(n, np.nan)
    if length <= 0 or n == 0:
        return out

    a = 2.0 / (length + 1.0)
    one_minus_a = 1.0 - a

    # 收集前 `length` 个有限值以计算 seed
    finite_count = 0
    seed_sum = 0.0
    seed_idx = -1  # seed 将写入的索引

    for i in range(n):
        v = x[i]
        if math.isfinite(v):
            seed_sum += v
            finite_count += 1
            if finite_count == length:
                seed_idx = i
                break

    if seed_idx < 0:
        # 不足 length 个有限值，全部返回 NaN
        return out

    seed_val = seed_sum / length
    out[seed_idx] = seed_val
    prev = seed_val

    # 从 seed_idx+1 开始递推
    for i in range(seed_idx + 1, n):
        v = x[i]
        if math.isfinite(v):
            prev = a * v + one_minus_a * prev
        # NaN 输入：carry-forward（prev 不变）
        out[i] = prev

    return out


def _sma_seeded_rma(x: np.ndarray, length: int) -> np.ndarray:
    """SMA-seeded RMA（Wilder 平滑均线）。

    与 _sma_seeded_ema 相同逻辑，但 a = 1/length（Wilder alpha）。
    spec parity oracle: rma([10,20,30], 2) = [NaN, 15, 22.5]
    """
    n = len(x)
    out = np.full(n, np.nan)
    if length <= 0 or n == 0:
        return out

    a = 1.0 / length
    one_minus_a = 1.0 - a

    finite_count = 0
    seed_sum = 0.0
    seed_idx = -1

    for i in range(n):
        v = x[i]
        if math.isfinite(v):
            seed_sum += v
            finite_count += 1
            if finite_count == length:
                seed_idx = i
                break

    if seed_idx < 0:
        return out

    seed_val = seed_sum / length
    out[seed_idx] = seed_val
    prev = seed_val

    for i in range(seed_idx + 1, n):
        v = x[i]
        if math.isfinite(v):
            prev = a * v + one_minus_a * prev
        out[i] = prev

    return out


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True Range 序列。

    bar 0 = high[0] - low[0]（无前一 close）。
    bar i > 0 = NaN-skipping max(H-L, |H-prev_C|, |L-prev_C|)。
    若 H/L/C 无效 → NaN。
    """
    n = len(high)
    tr = np.full(n, np.nan)
    if n == 0:
        return tr

    # bar 0
    h0, l0 = high[0], low[0]
    if math.isfinite(h0) and math.isfinite(l0):
        tr[0] = h0 - l0

    for i in range(1, n):
        h, l, c_prev = high[i], low[i], close[i - 1]
        if not (math.isfinite(h) and math.isfinite(l)):
            continue
        hl = h - l
        if math.isfinite(c_prev):
            tr[i] = max(hl, abs(h - c_prev), abs(l - c_prev))
        else:
            # 无前一 close，退化为 H-L
            tr[i] = hl

    return tr


def _atr_series(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int) -> np.ndarray:
    """ATR = rma(true_range, length)，SMA-seeded Wilder。"""
    tr = _true_range(high, low, close)
    return _sma_seeded_rma(tr, length)


def _volume_weighted_base(
    close: np.ndarray,
    volume: np.ndarray,
    length: int,
) -> np.ndarray:
    """成交量加权基准线（ma_src='EMA' 默认路径）。

    cv = close * volume
    base = ema(cv, length) / ema(volume, length)
    guard: ema(volume) == 0 → NaN（规格 §1）
    """
    cv = close * volume
    ema_cv = _sma_seeded_ema(cv, length)
    ema_vol = _sma_seeded_ema(volume, length)

    n = len(close)
    base = np.full(n, np.nan)
    for i in range(n):
        ev = ema_vol[i]
        ec = ema_cv[i]
        if math.isfinite(ev) and math.isfinite(ec) and abs(ev) > 1e-30:
            base[i] = ec / ev
    return base


def _supertrend(
    base: np.ndarray,
    atr: np.ndarray,
    close: np.ndarray,
    factor: float,
) -> tuple[np.ndarray, np.ndarray]:
    """SuperTrend ratchet，返回 (st_line, direction)。

    direction 使用 Pine 内部惯例：-1 = 多头(bullish), +1 = 空头(bearish)。
    (不对外暴露 direction；label = (price_wma > st_wma) 已用直觉方向。)

    规格 §4 ratchet 规则：
      if i==0 or atr[i-1] 无限 → direction=1（空头），取 raw bands
      ratchet lower: lower = raw 仅当 (lower_raw>prev_lower OR close_prev<prev_lower)
      ratchet upper: upper = raw 仅当 (upper_raw<prev_upper OR close_prev>prev_upper)
      方向判断：
        if prev_st ≈ prev_upper（上轨，即空头），close>upper_i → bearish(dir=-1) else 1
        else（下轨，多头），close<lower_i → bullish(dir=1) else -1
    """
    n = len(base)
    st = np.full(n, np.nan)
    direction = np.full(n, np.nan)

    BAND_EPS = 1e-9

    prev_lower = math.nan
    prev_upper = math.nan
    prev_st = math.nan
    prev_dir = math.nan
    prev_atr = math.nan

    for i in range(n):
        b = base[i]
        a = atr[i]
        c = close[i]

        if not (math.isfinite(b) and math.isfinite(a) and math.isfinite(c)):
            # 无效 bar：carry-forward（保持 prev）
            st[i] = math.nan
            direction[i] = math.nan
            prev_atr = math.nan  # 标记为无效，下一 bar 重置
            continue

        upper_raw = b + factor * a
        lower_raw = b - factor * a

        if i == 0 or not math.isfinite(prev_atr):
            # 首 bar 或前一 bar ATR 无效 → 初始化
            lower_i = lower_raw
            upper_i = upper_raw
            dir_i = 1  # 初始空头（Pine 惯例）
        else:
            # ratchet lower
            if lower_raw > prev_lower or close[i - 1] < prev_lower:
                lower_i = lower_raw
            else:
                lower_i = prev_lower

            # ratchet upper
            if upper_raw < prev_upper or close[i - 1] > prev_upper:
                upper_i = upper_raw
            else:
                upper_i = prev_upper

            # 方向判断
            if math.isfinite(prev_st) and abs(prev_st - prev_upper) <= BAND_EPS:
                # 前 bar 在上轨（空头）
                dir_i = -1 if c > upper_i else 1
            else:
                # 前 bar 在下轨（多头）
                dir_i = 1 if c < lower_i else -1

        # st[i] = lower_i if dir_i == -1 else upper_i
        st_i = lower_i if dir_i == -1 else upper_i

        st[i] = st_i
        direction[i] = float(dir_i)

        prev_lower = lower_i
        prev_upper = upper_i
        prev_st = st_i
        prev_dir = float(dir_i)
        prev_atr = a

    return st, direction


def _weighted_knn_1d(
    st: np.ndarray,
    label: np.ndarray,
    k: int,
    n_points: int,
    i: int,
) -> float:
    """1D 加权 KNN 预测，对 bar i 执行（含自身）。

    窗口：[max(0, i+1-n_eff) .. i]（inclusive），n_eff = max(n_points, k)。
    距离：d_j = |st[j] - st[i]|（只对有限 st_j / label_j 的 bar）。
    权重：w_j = 1 / (d_j + 1e-6)。
    排序：距离升序，tie-break 新 bar 优先（newer index first）。
    取前 k 个邻居（k_eff = min(k, len(candidates))）。

    返回：pred = Σ(w_j * label_j) / Σ(w_j)，若无有限候选 → nan。

    规格 §7 注意：self-included（j==i 时 d=0, w=1e6），k=1 时退化为 label[i]。
    """
    n_eff = max(n_points, k)
    start = max(0, i + 1 - n_eff)
    query_st = st[i]

    if not math.isfinite(query_st):
        return math.nan

    # 收集候选（含自身）
    candidates: list[tuple[float, int]] = []  # (distance, position_in_window)
    for j in range(start, i + 1):
        st_j = st[j]
        lb_j = label[j]
        if math.isfinite(st_j) and math.isfinite(lb_j):
            d = abs(st_j - query_st)
            candidates.append((d, j))

    if not candidates:
        return math.nan

    # 排序：距离升序，tie-break 新 bar 优先（更大 j = 更新）
    # np.lexsort((-pos, dist))：先按 dist 升序，dist 相等时按 -j 升序 = j 降序
    dists = np.array([c[0] for c in candidates], dtype=float)
    indices = np.array([c[1] for c in candidates], dtype=int)
    sort_order = np.lexsort((-indices, dists))

    k_eff = min(k, len(candidates))
    top_k = sort_order[:k_eff]

    total_weight = 0.0
    weighted_sum = 0.0
    for idx in top_k:
        d = dists[idx]
        lb = label[candidates[idx][1]]
        w = 1.0 / (d + 1e-6)
        weighted_sum += w * lb
        total_weight += w

    if total_weight <= 0:
        return math.nan

    return weighted_sum / total_weight


# ─────────────────────────────────────────────────────────────────────────────
# 公共 API
# ─────────────────────────────────────────────────────────────────────────────

def ai_st_series(
    candles: list[Any],
    *,
    k: int = 1,
    n_points: int = 30,
    price_len: int = 2,
    st_len: int = 90,
    length: int = 20,
    factor: float = 1.5,
    ma_src: str = "EMA",
) -> np.ndarray:
    """AI SuperTrend 趋势因子序列。

    Args:
        candles:   K 线列表，每项需有 .o/.h/.l/.c/.v 属性。
        k:         KNN 邻居数（默认 1，Pine 奇偶路径；=1 时输出 {-1,+1}）。
        n_points:  KNN 窗口大小（默认 30）。
        price_len: price_wma 的 WMA 长度（默认 2）。
        st_len:    st_wma 的 WMA 长度（默认 90，主暖机驱动）。
        length:    volume-weighted base EMA + ATR Wilder RMA 长度（默认 20）。
        factor:    ATR 倍数（默认 1.5）。
        ma_src:    基准 MA 类型（默认 "EMA"；目前只实现 EMA 路径）。

    Returns:
        np.ndarray，长度 = len(candles)，warmup 段 = NaN，有效段 in [-1, 1]。

    暖机：length + st_len - 1 = 109（默认参数）根后开始产生有限值。

    退化（k=1）：ai_st = (price_wma > st_wma) ? +1 : -1（spec §8 identity map）。
    """
    n = len(candles)
    out = np.full(n, np.nan)
    if n == 0:
        return out

    # ── 提取 OHLCV ──────────────────────────────────────────────────────────
    arrs = ohlcv_arrays(candles)
    h = arrs["h"]
    lo = arrs["l"]
    c = arrs["c"]
    v = arrs["v"]

    # ── §1 成交量加权基准线 ──────────────────────────────────────────────────
    base = _volume_weighted_base(c, v, length)

    # ── §2 ATR（Wilder RMA）────────────────────────────────────────────────
    atr_arr = _atr_series(h, lo, c, length)

    # ── §3-4 SuperTrend ratchet ──────────────────────────────────────────────
    st_line, _direction = _supertrend(base, atr_arr, c, factor)

    # ── §5 WMA 平滑 ──────────────────────────────────────────────────────────
    price_wma = wma_series(c, price_len)
    st_wma = wma_series(st_line, st_len)

    # ── §6 KNN label[i] = (price_wma[i] > st_wma[i]) ? 1.0 : 0.0 ────────────
    # NaN > NaN → False → 0.0（与 Pine 一致）
    label = np.where(
        np.isfinite(price_wma) & np.isfinite(st_wma) & (price_wma > st_wma),
        1.0,
        0.0,
    )

    # ── §7 KNN 预测 ──────────────────────────────────────────────────────────
    # 逐 bar 计算（state-dependent，无法向量化）
    pred = np.full(n, np.nan)
    for i in range(n):
        if not math.isfinite(st_line[i]):
            continue
        p = _weighted_knn_1d(st_line, label, k, n_points, i)
        if math.isfinite(p):
            pred[i] = p

    # ── §8 factor = clamp(pred*2 - 1) ─────────────────────────────────────
    # Fallback §10：pred NaN 但 st_line 和 close 有限 → tanh 退化值
    raw = np.full(n, np.nan)

    finite_pred = np.isfinite(pred)
    raw[finite_pred] = pred[finite_pred] * 2.0 - 1.0

    # fallback：pred 无效但 st 和 close 有限
    no_pred = ~finite_pred
    has_st_c = np.isfinite(st_line) & np.isfinite(c)
    fallback_mask = no_pred & has_st_c
    if np.any(fallback_mask):
        denom = np.maximum(np.abs(c[fallback_mask]), 1e-12)
        rel = (c[fallback_mask] - st_line[fallback_mask]) / denom
        raw[fallback_mask] = np.tanh(100.0 * rel)

    # clamp 到 [-1, 1]（非有限 → NaN）
    out = clamp(raw)
    return out


def ai_st_factor(
    candles: list[Any],
    *,
    k: int = 1,
    n_points: int = 30,
    price_len: int = 2,
    st_len: int = 90,
    length: int = 20,
    factor: float = 1.5,
    ma_src: str = "EMA",
) -> float | None:
    """AI SuperTrend 趋势因子标量（末值）。

    返回 ai_st_series 的最后一个有限值。
    不足暖机期 → None。

    供 parity 测试 + 末值消费（KNN feature space）。
    """
    series = ai_st_series(
        candles,
        k=k,
        n_points=n_points,
        price_len=price_len,
        st_len=st_len,
        length=length,
        factor=factor,
        ma_src=ma_src,
    )
    # 找最后一个有限值
    for i in range(len(series) - 1, -1, -1):
        v = series[i]
        if math.isfinite(v):
            return float(v)
    return None
