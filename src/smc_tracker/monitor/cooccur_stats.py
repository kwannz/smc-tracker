"""协同共现显著性统计 —— 二项 null model + 活跃度归一（确定性纯函数，无 I/O）。

B2 实现：用闭式二项右尾概率判断地址对「协同共现」是否优于随机期望。
消除高频地址结构性偏向：高频地址因绝对次数高而"天然相关"，
通过独立性期望比 (lift) + 显著性检验 (p-value) 过滤掉随机人群。

API：
  pair_lift(pair_count, a_activity, b_activity, total_events) → (lift, p_value)
  is_significant(lift, p_value, min_lift, max_p) → bool
  _binom_tail_log(k_min, n, p_prob) → log(p)  — 内部辅助，导出供测试校验
"""
from __future__ import annotations

import math

import numpy as np

# ─── 内部常量 ──────────────────────────────────────────────────────────────────
_EPS = 1e-12           # 除零守卫
_MIN_EVENTS = 30       # total_events 低于此值时样本不足，返回中性 (1.0, 1.0)
_N_EXACT_MAX = 200     # n <= 此值使用对数精确累加；n > 此值用正态近似


def _binom_tail_log(k_min: int, n: int, p_prob: float) -> float:
    """二项右尾 log P(X >= k_min | n, p_prob)，对数空间防溢出。

    n <= _N_EXACT_MAX：对数空间精确逐项累加（确定性）。
    n > _N_EXACT_MAX：正态近似（连续性校正）兜底，保持低延迟。
    边界：
      k_min <= 0      → log(1.0) = 0.0（必然事件）
      k_min > n       → log(eps) ≈ −∞（零概率，保护用 log(eps)）
      p_prob <= 0      → 0 次概率 1（只有 k_min=0 非零）
      p_prob >= 1      → n 次全中（只有 k_min=n 非零）
    """
    k_min = max(0, k_min)
    if k_min <= 0:
        return 0.0                       # P(X >= 0) = 1
    if k_min > n:
        return math.log(_EPS)            # P(X > n) = 0
    p_prob = min(max(float(p_prob), 0.0), 1.0)
    if p_prob <= 0.0:
        # 所有观测必然为 0，P(X >= k_min>0) = 0
        return math.log(_EPS)
    if p_prob >= 1.0:
        # 所有观测必然为 n，P(X >= k_min) = 1 iff k_min<=n
        return 0.0

    if n <= _N_EXACT_MAX:
        # 对数空间逐项精确求和
        # log C(n,k) 用 math.lgamma 防溢出
        log_p = math.log(p_prob)
        log_q = math.log(1.0 - p_prob)
        log_sum = -math.inf
        for k in range(k_min, n + 1):
            log_comb = (math.lgamma(n + 1)
                        - math.lgamma(k + 1)
                        - math.lgamma(n - k + 1))
            log_term = log_comb + k * log_p + (n - k) * log_q
            # log_sum = log(exp(log_sum) + exp(log_term)) = logsumexp(log_sum, log_term)
            if log_term > log_sum:
                log_sum = log_term + math.log1p(math.exp(log_sum - log_term))
            else:
                log_sum = log_sum + math.log1p(math.exp(log_term - log_sum))
        return log_sum
    else:
        # n 大：正态近似（Central Limit Theorem），连续性校正
        mean = n * p_prob
        var = n * p_prob * (1.0 - p_prob)
        std = math.sqrt(var) if var > 0 else _EPS
        # 连续性校正：P(X >= k) ≈ P(Z >= (k - 0.5 - mean) / std)
        z = (k_min - 0.5 - mean) / std
        # 标准正态右尾 log CDF：用 math.erfc
        # P(Z >= z) = erfc(z / sqrt(2)) / 2
        p_val = math.erfc(z / math.sqrt(2.0)) / 2.0
        return math.log(max(p_val, _EPS))


def pair_lift(
    pair_count: int,
    a_activity: int,
    b_activity: int,
    total_events: int,
) -> tuple[float, float]:
    """返回 (lift, p_value)——协同共现相对于随机期望的强度与显著性。

    null model（独立性假设）：
      expected = a_activity * b_activity / total_events  （独立期望共现次数）
      lift     = pair_count / max(expected, eps)          （>1 = 强于随机）

    p_value: 二项右尾 P(X >= pair_count | n=a_activity, p=b_activity/total_events)
      n <= 200：对数空间精确；n > 200：正态近似（连续性校正）。
      确定性，无随机，低延迟。

    安全守卫：
      total_events < _MIN_EVENTS(30)  → 样本不足，返回中性 (1.0, 1.0)
      a_activity=0 或 b_activity=0    → 返回中性 (1.0, 1.0)（无法判断）
      除法均加 eps 守卫
    """
    # 整数化守卫
    pair_count = max(0, int(pair_count))
    a_activity = max(0, int(a_activity))
    b_activity = max(0, int(b_activity))
    total_events = max(0, int(total_events))

    # 样本不足 → 中性（诚实，不冒进）
    if total_events < _MIN_EVENTS or a_activity == 0 or b_activity == 0:
        return (1.0, 1.0)

    # 独立性期望
    expected = a_activity * b_activity / total_events
    lift = pair_count / max(expected, _EPS)

    # 二项右尾概率：n=a_activity, p=b_activity/total_events
    p_prob = b_activity / total_events
    log_p = _binom_tail_log(pair_count, a_activity, p_prob)
    p_value = math.exp(log_p)

    return (lift, p_value)


def is_significant(
    lift: float,
    p_value: float,
    min_lift: float,
    max_p: float,
) -> bool:
    """显著 ⟺ lift >= min_lift 且 p_value <= max_p（含极显著旁路）。

    纯阈值判据，确定性。
    min_lift=2.0 + max_p=0.01 = 99% 置信且强度 ≥2× 随机（业界常用强关联标准）。
    """
    if lift >= min_lift and p_value <= max_p:
        return True
    # 极显著旁路：lift 未达阈但 p 极小（< 1e-9）的强协同不漏判（防庄家集团被阈值挡掉）
    return p_value < 1e-9 and lift >= 1.5
