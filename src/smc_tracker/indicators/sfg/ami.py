"""sfg/ami.py — SFG AMI（AI Momentum Index / MLMI）因子移植。

角色：reversal 簇（ContinuousFactors.ami，index 5 of 8）。

算法来源：SFG - AI Momentum Index 1.pine + continuous_factors.rs:274-290
          + ai_momentum_index.rs + mlmi_store.rs

符号约定（反转簇，诚实标注）：
  +1 = 看涨反转预期 = prediction 近通道低端（动量耗尽/超卖）
  -1 = 看跌反转预期 = prediction 近通道高端（动量过热/超买）
  这是对原始 AMI prediction 的 NEGATION（均值回归 encoding）。
  underlying prediction 本身 >0 表示净上涨历史，但因子将其反转为看跌信号。

注意：历史 KNN 预测在短序列下受 store size 限制（见 spec:lookahead_risk），
      结果是确定性的但受起始 bar 影响。Python 端从 bar 0 开始 warm，
      序列不同起点结果会不同（非 repaint，是确定性 seeding 问题）。

退化（short sequence）：
  若序列长度 < wma_slow_len + momentum_window（~40 根，WMA/RSI 双级暖机），
  全为 nan。channel_lookback=2000 在 <=2000 根时等同于 inception-to-now rolling。

无前视：
  - label = close[i] >= close[prev_event]（past+present only）
  - KNN query：仅访问已存储的 inception-to-bar-i slots
  - channel bounds：trailing rolling max/min(pred, 2000, min_periods=1)
  - crossover/crossunder：仅用 [i] 和 [i-1]
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from smc_tracker.util import to_float
from ._common import wma_series, clamp, rolling_max_series, rolling_min_series


# ─────────────────────────────────────────────────────────────────────────────
# RSI（Wilder's，RMA-seeded，含饱和 mask）
# ─────────────────────────────────────────────────────────────────────────────

def _rsi_series(close: np.ndarray, length: int) -> np.ndarray:
    """Wilder RSI（RMA alpha=1/L，SMA seed，饱和 mask）。

    与 Pine ta.rsi() 语义对齐（spec algorithm_steps[0]）：
      - bar0 → NaN
      - 前 length 根暖机 → NaN
      - loss==0 & gain>0 → 100
      - gain==0 & loss>0 → 0
      - gain==0 & loss==0 → 50（价格无变化）
      - 否则 RSI = 100 - 100/(1 + rs)，rs = avg_gain / avg_loss
    """
    n = len(close)
    out = np.full(n, np.nan)
    if length <= 0 or n < length + 1:
        return out

    # 计算逐差（delta）
    delta = np.full(n, np.nan)
    delta[1:] = close[1:] - close[:-1]

    gain = np.where(delta > 0, delta, 0.0)   # 上涨幅度
    loss = np.where(delta < 0, -delta, 0.0)  # 下跌幅度

    alpha = 1.0 / length
    one_minus_alpha = 1.0 - alpha

    # 第一个 avg_gain/avg_loss：SMA(gain[1..length]) 种子
    # 需要 bar 1..length（含）作为初始 SMA 窗口
    seed_start = 1           # delta[0] = NaN，从 1 开始
    seed_end = length        # 包含 bar length

    avg_g = float(np.mean(gain[seed_start: seed_end + 1]))
    avg_l = float(np.mean(loss[seed_start: seed_end + 1]))

    # bar=length 输出 RSI（第一个有效 bar）
    def _rsi_from_avgs(ag: float, al: float) -> float:
        """由 avg_gain, avg_loss 计算单个 RSI 值（含饱和 mask）。"""
        if al == 0.0 and ag > 0.0:
            return 100.0
        if ag == 0.0 and al > 0.0:
            return 0.0
        if ag == 0.0 and al == 0.0:
            return 50.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    out[seed_end] = _rsi_from_avgs(avg_g, avg_l)

    # RMA 递推：bar length+1 .. n-1
    for i in range(seed_end + 1, n):
        g_i = float(gain[i])
        l_i = float(loss[i])
        avg_g = one_minus_alpha * avg_g + alpha * g_i
        avg_l = one_minus_alpha * avg_l + alpha * l_i
        out[i] = _rsi_from_avgs(avg_g, avg_l)

    return out


# ─────────────────────────────────────────────────────────────────────────────
# MLMI KNN 预测（批量，inception-to-now）
# ─────────────────────────────────────────────────────────────────────────────

def _mlmi_prediction_series(
    rsi_slow: np.ndarray,
    rsi_quick: np.ndarray,
    ma_quick: np.ndarray,
    ma_slow: np.ndarray,
    close: np.ndarray,
    k: int = 200,
) -> np.ndarray:
    """对每根 bar 执行 Pine-literal 的 KNN 累计预测。

    spec algorithm_steps[3-4]:
    - sentinel slot (0, 0, 0, label=0) 参与每次 query
    - 事件 bar：append (rsi_slow[i], rsi_quick[i], close[i], label)
    - label = (close[i] >= prev_price) ? +1 : -1
    - KNN query：所有 slots 的 d2，取 m-th smallest（m=min(max(k,1), n_finite)）作 max_dist
    - pred = sum(label[j] for j if d2[j] <= max_dist)
    - 无有限邻居 → pred = 0.0（非 NaN，Pine-literal）

    在实践中 store size < k=200，所有 slot 都被选中 → pred = 累计 ±1 sum。
    """
    n = len(close)
    pred = np.zeros(n, dtype=float)  # Pine-literal: 0.0 when no finite neighbours

    # KNN store: 四个列表（含 sentinel slot 0）
    # sentinel: param1=0, param2=0, price=0, label=0
    store_p1: list[float] = [0.0]    # rsi_slow
    store_p2: list[float] = [0.0]    # rsi_quick
    store_price: list[float] = [0.0]
    store_label: list[int] = [0]

    for i in range(n):
        p1_i = float(rsi_slow[i])
        p2_i = float(rsi_quick[i])
        c_i = float(close[i])
        mq = float(ma_quick[i])
        ms = float(ma_slow[i])

        # ── 事件判断（crossover / crossunder）──────────────────────────────
        event = False
        if i > 0 and math.isfinite(mq) and math.isfinite(ms):
            mq_prev = float(ma_quick[i - 1])
            ms_prev = float(ma_slow[i - 1])
            if math.isfinite(mq_prev) and math.isfinite(ms_prev):
                crossover = (mq > ms) and (mq_prev <= ms_prev)
                crossunder = (mq < ms) and (mq_prev >= ms_prev)
                event = crossover or crossunder

        # ── 事件时 append（先 query 后 append，Pine bar-evaluation 顺序）─────
        # spec: "APPEND happens only on event bars when close is finite"
        # Pine 语义：先 knnPredict 再 storePreviousTrade
        # 所以先 query（使用当前已存 store），再 append
        # query ────────────────────────────────────────────────────────────
        n_stored = len(store_p1)
        if not (math.isfinite(p1_i) and math.isfinite(p2_i)):
            # NaN features → pred=0.0（Pine-literal）
            pred[i] = 0.0
        else:
            # 计算所有 slot 的 d2
            d2_list: list[float] = []
            for j in range(n_stored):
                d2_j = (store_p1[j] - p1_i) ** 2 + (store_p2[j] - p2_i) ** 2
                d2_list.append(d2_j)

            d2_arr = np.array(d2_list, dtype=float)
            finite_d2 = d2_arr[np.isfinite(d2_arr)]
            n_finite = len(finite_d2)

            if n_finite == 0:
                # 无有限距离 → pred=0.0
                pred[i] = 0.0
            else:
                # max_dist = m-th smallest，m = min(max(k,1), n_finite)
                m_idx = min(max(k, 1), n_finite) - 1  # 0-indexed
                sorted_d2 = np.sort(finite_d2)
                max_dist = float(sorted_d2[m_idx])

                # sum labels for d2 <= max_dist
                p_sum = 0.0
                for j in range(n_stored):
                    if math.isfinite(d2_arr[j]) and d2_arr[j] <= max_dist:
                        p_sum += store_label[j]
                pred[i] = p_sum

        # append（事件触发 + close 有限）─────────────────────────────────
        if event and math.isfinite(c_i) and math.isfinite(p1_i) and math.isfinite(p2_i):
            # label 编码：与上一个存储事件的 price 比较
            prev_price = store_price[-1]  # sentinel or last event price
            label_new = 1 if c_i >= prev_price else -1
            store_p1.append(p1_i)
            store_p2.append(p2_i)
            store_price.append(c_i)
            store_label.append(label_new)

    return pred


# ─────────────────────────────────────────────────────────────────────────────
# ami_series：主向量化函数
# ─────────────────────────────────────────────────────────────────────────────

def ami_series(
    candles: list[Any],
    momentum_window: int = 20,
    num_neighbors: int = 200,
    channel_lookback: int = 2000,
    wma_quick_len: int = 5,
    wma_slow_len: int = 20,
    rsi_quick_len: int = 5,
    rsi_slow_len: int = 20,
) -> np.ndarray:
    """计算 SFG AMI 因子序列（全长，warmup=nan，clamp [-1,1]）。

    Args:
        candles: K 线列表，需有 .c 属性（close-only；open/high/low/volume 忽略）。
        momentum_window: WMA 平滑窗口（Pine "Trend Length"，default=20）。
        num_neighbors: KNN 邻居数 k（default=200，实践中 ≈ store size，全选）。
        channel_lookback: 通道 highest/lowest 窗口（default=2000，短序列等同 inception）。
        wma_quick_len: 快速 MA 长度（default=5，hardcoded）。
        wma_slow_len: 慢速 MA 长度（default=20，hardcoded）。
        rsi_quick_len: 快速 RSI 长度（default=5，hardcoded）。
        rsi_slow_len: 慢速 RSI 长度（default=20，hardcoded）。

    Returns:
        np.ndarray，长度=len(candles)，float64，warmup 期为 nan，
        有效值 clamp 到 [-1, 1]。

    符号约定（reversal cluster）：
        +1 = 看涨反转预期（prediction 近通道低端，超卖）
        -1 = 看跌反转预期（prediction 近通道高端，超买）

    注意：短序列退化（< ~40 根）全为 nan；
          channel_lookback=2000 在 <=2000 根时等同 inception-to-now（min_periods=1）。
    """
    if not candles:
        return np.array([], dtype=float)

    n = len(candles)
    nan_out = np.full(n, np.nan)

    # ── 提取 close（close-only 算法；fail-closed：缺/非有限 → NaN）─────────────
    close = np.array([to_float(c.c, default=math.nan) for c in candles], dtype=float)

    # ── 特征维度：RSI 后 WMA 平滑 ─────────────────────────────────────────────
    # rsi_slow = wma(rsi(close, 20), momentum_window)  → p1/param1
    # rsi_quick = wma(rsi(close, 5), momentum_window)  → p2/param2
    rsi_raw_quick = _rsi_series(close, rsi_quick_len)   # rsi(close, 5)
    rsi_raw_slow = _rsi_series(close, rsi_slow_len)     # rsi(close, 20)

    rsi_quick = wma_series(rsi_raw_quick, momentum_window)  # wma(rsi5, mw)
    rsi_slow = wma_series(rsi_raw_slow, momentum_window)    # wma(rsi20, mw)

    # ── MA cross 事件触发序列 ───────────────────────────────────────────────────
    ma_quick = wma_series(close, wma_quick_len)   # wma(close, 5)
    ma_slow = wma_series(close, wma_slow_len)     # wma(close, 20)

    # ── MLMI KNN 预测序列 ───────────────────────────────────────────────────────
    # 这一步是 O(n * store_size)，store_size ≈ 事件数（cross 次数，< n）
    pred = _mlmi_prediction_series(
        rsi_slow=rsi_slow,
        rsi_quick=rsi_quick,
        ma_quick=ma_quick,
        ma_slow=ma_slow,
        close=close,
        k=num_neighbors,
    )
    # pred 在 Pine-literal 定义下永不为 NaN：NaN features → 0.0

    # ── 通道上下界（trailing rolling max/min，min_periods=1）─────────────────
    # spec: upper=highest(pred, 2000), lower=lowest(pred, 2000), min_periods=1
    upper = rolling_max_series(pred, channel_lookback, min_periods=1)
    lower = rolling_min_series(pred, channel_lookback, min_periods=1)

    # ── 因子公式（fail-closed：任一非有限 或 range≤0 → NaN）────────────────────
    # norm = (pred - lower) / (upper - lower)
    # factor = clamp(-(2*norm - 1), -1, 1)
    #        = clamp((lower + upper - 2*pred) / (upper - lower), -1, 1)
    rng = upper - lower  # channel range
    valid = (
        np.isfinite(pred)
        & np.isfinite(upper)
        & np.isfinite(lower)
        & (rng > 0.0)
    )

    raw = np.full(n, np.nan)
    raw[valid] = (lower[valid] + upper[valid] - 2.0 * pred[valid]) / rng[valid]

    return clamp(raw)


# ─────────────────────────────────────────────────────────────────────────────
# ami_factor：标量包装（末值消费，供 parity 测试 + KNN 特征）
# ─────────────────────────────────────────────────────────────────────────────

def ami_factor(
    candles: list[Any],
    **params: Any,
) -> float | None:
    """计算 AMI 因子末值（标量）。

    Returns:
        float（有限值，clamp[-1,1]）或 None（warmup 不足时）。
    """
    series = ami_series(candles, **params)
    if len(series) == 0:
        return None
    finite_vals = series[np.isfinite(series)]
    if len(finite_vals) == 0:
        return None
    return float(finite_vals[-1])
