"""sfg/vap.py — VAP（Volume Algo Profile）反转簇因子，SFG 8因子反转向量第3位。

算法来源：SFG 研发规格 sfg_spec_vap.json（来自 Rust continuous_factors.rs + volume_algo_profile.rs）。
角色：**反转（reversal）** 因子，衡量当前价格偏离成交量共识中心（POC）的程度，
      用价值区半宽归一化。

符号约定（SFG sign_convention）：
  +1 = 看涨反转信号 — close 低于 POC（超卖，预期反弹）
  -1 = 看跌反转信号 — close 高于 POC（超买，预期回落）
   0 = close 恰好等于 POC

公式（spec factor_formula）：
  alpha = clamp( -2*(close - poc) / |vah - val| )

注意：
  - VAP 是滚动 per-bar 计算（不是最后一根全程快照），严格因果（no-lookahead）。
  - 在短序列（length 很大、窗口内 hi==lo）时退化为 NaN（fail-closed），
    见 docstring 退化说明。
  - 低延迟：per-bar 内核为纯 numpy 向量化的 histogram，外层 Python 循环
    数量 = K 线数量，适合中低频 KNN 特征计算。

诚实标注：VAP 是反转确认辅助特征，非预测保证；单因子方向 ≈ 50%-60%，
需与其他因子联合使用方能提升 lift（CLAUDE.md §二）。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from ._common import clamp, ohlcv_arrays


# ─────────────────────────────────────────────────────────────────────────────
# 内核：单窗口 VAP 计算（返回 poc_price, vah, val 或全 NaN）
# ─────────────────────────────────────────────────────────────────────────────


def _compute_window_vap(
    wh: np.ndarray,  # 窗口内 high 数组
    wl: np.ndarray,  # 窗口内 low 数组
    wv: np.ndarray,  # 窗口内 volume 数组
    rows: int,
    value_area_pct: float,
    eps: float = 1e-12,
) -> tuple[float, float, float]:
    """单窗口 VAP 核心：返回 (poc_price, vah, val)；退化时返回 (nan, nan, nan)。

    退化条件（fail-closed）：
      - hi <= lo（价格无范围，如所有 K 线价格相同）
      - 总成交量 tv <= 0
      - 任意输入含 NaN/inf

    算法对应 volume_algo_profile.rs:148-223（差分数组 + POC + 价值区外扩）。
    """
    _nan = (math.nan, math.nan, math.nan)

    # 1. 窗口极值（nanmax/nanmin）
    # 过滤有限值
    fh = wh[np.isfinite(wh)]
    fl = wl[np.isfinite(wl)]
    if len(fh) == 0 or len(fl) == 0:
        return _nan
    hi = float(np.max(fh))
    lo = float(np.min(fl))

    # 2. 范围守卫：hi > lo
    if not (math.isfinite(hi) and math.isfinite(lo) and hi > lo):
        return _nan

    # 3. 构建 bin 边界（linspace，强制 edges[rows]=hi 精确终点）
    # 对应 volume_algo_profile.rs:148-158
    step = (hi - lo) / rows
    # edges[k] = k*step + lo
    edges = np.empty(rows + 1, dtype=float)
    for k in range(rows):
        edges[k] = k * step + lo
    edges[rows] = hi  # 强制终点精确（镜像 Rust forced endpoint）

    # bin 中点
    mids = (edges[:rows] + edges[1:]) / 2.0  # shape (rows,)

    # 4. 差分数组体积分档（volume_algo_profile.rs:163-188）
    # searchsorted_right(edges, x) = count of edges <= x → np.searchsorted(edges, x, side='right')
    # searchsorted_left(edges, x)  = count of edges < x  → np.searchsorted(edges, x, side='left')
    row_diff = np.zeros(rows + 1, dtype=float)  # 长度 rows+1（差分尾部）

    n_bars = len(wh)
    for k in range(n_bars):
        h_k = wh[k]
        l_k = wl[k]
        v_k = wv[k]
        if not (math.isfinite(h_k) and math.isfinite(l_k) and math.isfinite(v_k)):
            continue
        if v_k <= 0:
            continue
        # searchsorted_right(edges, low) - 1 给出 low 落入 bin 的起始下标
        bin_start = max(0, int(np.searchsorted(edges, l_k, side="right")) - 1)
        # searchsorted_left(edges, high) = min(rows, ...)
        bin_end = min(rows, int(np.searchsorted(edges, h_k, side="left")))
        if bin_end <= bin_start:
            continue
        row_diff[bin_start] += v_k
        row_diff[bin_end] -= v_k

    # 5. 前缀和得到各 bin 成交量（volume_algo_profile.rs:188-193）
    row_vol = np.cumsum(row_diff[:rows])  # shape (rows,)
    tv = float(np.sum(row_vol))
    if tv <= 0:
        return _nan

    # 6. POC：最后一个最大值 bin（last-tie argmax）
    # 对应 volume_algo_profile.rs:361-374
    max_vol = float(np.max(row_vol))
    poc_idx = 0
    for j in range(rows):
        if row_vol[j] >= max_vol:
            poc_idx = j  # 末次等于最大值的 index（last tie wins）

    poc_price = float(mids[poc_idx])

    # 7. 价值区外扩（volume_algo_profile.rs:195-223）
    # target = pairwise_sum(row_vol) * value_area_pct
    # 使用简单 sum（Python 浮点与 Rust pairwise_sum 在 value_area_pct 精度内可接受）
    target = tv * value_area_pct
    lo_i = poc_idx
    hi_i = poc_idx
    acc = float(row_vol[poc_idx])

    while acc < target and (lo_i > 0 or hi_i < rows - 1):
        left_v = float(row_vol[lo_i - 1]) if lo_i > 0 else -math.inf
        right_v = float(row_vol[hi_i + 1]) if hi_i < rows - 1 else -math.inf
        # right_v >= left_v：优先扩右（tie 向右）
        if right_v >= left_v:
            hi_i += 1
            acc += float(row_vol[hi_i])
        else:
            lo_i -= 1
            acc += float(row_vol[lo_i])

    # vah = edges[hi_i+1], val = edges[lo_i]
    vah = float(edges[hi_i + 1])
    val = float(edges[lo_i])

    return poc_price, vah, val


# ─────────────────────────────────────────────────────────────────────────────
# 公开 API
# ─────────────────────────────────────────────────────────────────────────────


def vap_series(
    candles: list[Any],
    *,
    length: int = 150,
    rows: int = 150,
    value_area_pct: float = 0.70,
    eps: float = 1e-12,
    **_kwargs: Any,
) -> np.ndarray:
    """VAP 因子序列（rolling per-bar，严格因果）。

    Args:
        candles:         K 线列表，每项需有 .o/.h/.l/.c/.v 属性。
        length:          滚动回看窗口长度（默认 150 bars）。
        rows:            价格分档数量（默认 150 bins）。
        value_area_pct:  价值区目标覆盖率（默认 0.70 = 70%）。
        eps:             数值稳定性常量（不参与因子，见 spec params）。

    Returns:
        np.ndarray shape=(len(candles),)：
          - out[i] = clamp(-2*(close[i]-poc_i)/|vah_i-val_i|) ∈ [-1, 1]
          - 退化窗口（hi==lo 或 tv<=0）→ NaN（fail-closed）
          - 无固定 warmup gate：窗口从 1 增长到 length，
            只要 hi>lo 且 tv>0 即产出，否则 NaN。

    退化说明：
      - length 很大而序列短时，所有早期窗口 hi≈lo（极端情况）→ 全 NaN。
      - 单价格序列（hi=lo 全程）→ 永远 NaN。
      - 对 KNN 使用：建议至少有 max(length/2, 20) 根有效输出再使用。
    """
    if not candles:
        return np.array([], dtype=float)

    n = len(candles)
    arrs = ohlcv_arrays(candles)
    h = arrs["h"]
    l = arrs["l"]
    c = arrs["c"]
    v = arrs["v"]

    # 有效 rows 守卫（镜像 Rust: max(2, rows)）
    eff_rows = max(2, int(rows))
    eff_length = max(1, int(length))

    out = np.full(n, np.nan, dtype=float)

    for i in range(n):
        # 窗口：[start, i]（growing from 1 to length）
        start = max(0, i + 1 - eff_length)
        wh = h[start: i + 1]
        wl = l[start: i + 1]
        wv = v[start: i + 1]

        ci = c[i]
        if not (math.isfinite(ci) and ci > 0):
            continue  # close 无效 → NaN

        poc_price, vah, val = _compute_window_vap(
            wh, wl, wv, eff_rows, value_area_pct, eps
        )

        if not (math.isfinite(poc_price) and math.isfinite(vah) and math.isfinite(val)):
            continue  # 退化窗口

        # 价值区宽度守卫
        va_width = abs(vah - val)
        if va_width <= 0:
            continue

        # 最终因子：alpha = clamp(-2*(close-poc)/|vah-val|)
        # spec: alpha = clamp(-dist / (va_width_pct/2))
        #             = clamp(-(close-poc)/close / (|vah-val|/close / 2))
        #             = clamp(-2*(close-poc)/|vah-val|)
        raw = -2.0 * (ci - poc_price) / va_width
        # clamp via _common.clamp（接受标量通过 1-element array）
        clamped = float(np.clip(raw, -1.0, 1.0)) if math.isfinite(raw) else math.nan
        out[i] = clamped

    return out


def vap_factor(
    candles: list[Any],
    *,
    length: int = 150,
    rows: int = 150,
    value_area_pct: float = 0.70,
    eps: float = 1e-12,
    **_kwargs: Any,
) -> float | None:
    """VAP 因子末值（标量包装）。

    Returns:
        float ∈ [-1, 1] — series 最后一个有限值；
        None             — 序列不足（全 NaN）或空输入。

    用于 parity 测试 + 末值消费（实时信号）。
    """
    s = vap_series(
        candles,
        length=length,
        rows=rows,
        value_area_pct=value_area_pct,
        eps=eps,
    )
    if len(s) == 0:
        return None
    # 取最后一个有限值
    finite_mask = np.isfinite(s)
    if not np.any(finite_mask):
        return None
    return float(s[finite_mask][-1])
