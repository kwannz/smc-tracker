"""funding 极值反转信号 —— 资金费率进入历史极值=多空拥挤→预示反转（前瞻领先信号）。

第一性原理（perp 微观结构）：funding 为正=多头付费给空头=多头拥挤；持续高 funding 极值
常预示多头拥挤反转（回落）。反之负 funding 极值=空头拥挤→反弹。这是 flow_score 不含的
独立前瞻维度（QA：避免与 FlowPredictor 双计）。

返回 ∈[-1,1]：正=看涨反转（funding 极低/空头拥挤）；负=看跌反转（funding 极高/多头拥挤）。
极值判定基于历史波动（z-score）；常量历史（std=0）无法定义极值→返 0（诚实不臆测）。
funding=0 的纯股票代币由上层 profile.has_funding 门控跳过，不在此判断。
"""
from __future__ import annotations

import math


def funding_extreme_signal(
    funding_now: float, funding_history: list[float], *, min_samples: int = 20
) -> float:
    """funding 极值反转信号 ∈[-1,1]（正=看涨反转）。

    参数：
        funding_now      — 当前 funding rate。
        funding_history  — 历史 funding 序列（同周期采样）。
        min_samples      — 最少样本数，不足→0.0。
    """
    n = len(funding_history)
    if n < min_samples:
        return 0.0
    mean = sum(funding_history) / n
    var = sum((x - mean) ** 2 for x in funding_history) / n
    std = math.sqrt(var)
    if std <= 0.0:
        return 0.0  # 常量历史无法定义极值
    z = (funding_now - mean) / std
    # 高 funding（z>0，多头拥挤）→ 看跌反转（负）；低 funding → 看涨反转（正）
    sig = -math.tanh(z / 2.0)
    return max(-1.0, min(1.0, sig))
