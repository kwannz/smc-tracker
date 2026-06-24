"""funding 极值反转信号 —— 资金费率进入历史极值=多空拥挤→预示反转（前瞻领先信号）。

第一性原理（perp 微观结构）：funding 为正=多头付费给空头=多头拥挤；持续高 funding 极值
常预示多头拥挤反转（回落）。反之负 funding 极值=空头拥挤→反弹。这是 flow_score 不含的
独立前瞻维度（QA：避免与 FlowPredictor 双计）。

返回 ∈[-1,1]：正=看涨反转（funding 极低/空头拥挤）；负=看跌反转（funding 极高/多头拥挤）。
极值判定：
  method="quantile"（默认，C.4 新增）：滚动窗口经验分位，稳健无分布假设，适合厚尾 funding。
  method="zscore"（向后兼容）：全历史等权 z-score，保留旧路径供平价对照测试。
常量历史无法定义极值→返 0（诚实不臆测）。
funding=0 的纯股票代币由上层 profile.has_funding 门控跳过，不在此判断。
"""
from __future__ import annotations

import math

import numpy as np


def funding_extreme_signal(
    funding_now: float,
    funding_history: list[float],
    *,
    min_samples: int = 20,
    window: int = 240,
    method: str = "quantile",
) -> float:
    """funding 极值反转信号 ∈[-1,1]（正=看涨反转）。

    参数：
        funding_now      — 当前 funding rate。
        funding_history  — 历史 funding 序列（同周期采样，时序从旧到新）。
        min_samples      — 最少样本数，不足→0.0。
        window           — 滚动窗口：仅取 funding_history[-window:] 的近期样本
                           （排除远古数据对分位稀释；默认 240 周期）。
        method           — "quantile"（经验分位，默认）或 "zscore"（旧路径，平价用）。
    """
    # 取近窗口样本
    hist = funding_history[-window:] if window > 0 else funding_history
    n = len(hist)
    if n < min_samples:
        return 0.0

    if method == "zscore":
        # 旧路径：全历史（已被 window 截断后的）等权 z-score
        mean = sum(hist) / n
        var = sum((x - mean) ** 2 for x in hist) / n
        std = math.sqrt(var)
        if std <= 0.0:
            return 0.0  # 常量历史无法定义极值
        z = (funding_now - mean) / std
        sig = -math.tanh(z / 2.0)
        return max(-1.0, min(1.0, sig))

    # method="quantile"：经验分位（无分布假设，稳健抗厚尾）
    arr = np.asarray(hist, dtype=float)
    # 常量历史：全部相等 → 分位退化 → 信号=0（诚实）
    if arr.max() == arr.min():
        return 0.0
    # 经验分位 p = 严格小于 funding_now 的比例（离散近似，numpy searchsorted）
    sorted_arr = np.sort(arr)
    rank = int(np.searchsorted(sorted_arr, funding_now, side="left"))
    p = rank / n  # ∈[0,1]
    # 映射：p→1（极高 funding，多头拥挤）→ 看跌（负）；p→0（极低）→ 看涨（正）
    # k≈1.5 调灵敏度（使 p=0.95 时 sig≈-0.83，适度强信号）
    k = 1.5
    sig = -math.tanh(k * (2.0 * p - 1.0))
    return max(-1.0, min(1.0, sig))
