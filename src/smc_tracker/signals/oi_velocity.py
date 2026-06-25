"""方向化 OI 速度 —— 把"持仓量变化"修成有方向的 positioning 信号（QA H4 修复）。

QA H4：`store.oi_change` 返回的是 OI 持仓量标量，**无方向**（持仓增加不区分多空建仓）。
把它直接当看涨/看跌是语义错误。方向化做法（perp 微观结构标准）：
  OI↑ + 价↑ = 新多进场（看涨，+）
  OI↑ + 价↓ = 新空进场（看跌，−）
  OI↓        = 平仓/去杠杆（符号随 Δoi<0 翻转，置信弱）
= (Δoi/oi_past) × sign(Δprice)，作为**方向化分数率**喂 FlowPredictor.predict(oi_velocity=...)
（不直接进 forward_mult，避免与 flow_score 内的 OI 分量双计——QA H5）。
"""
from __future__ import annotations


def oi_directional_velocity(
    oi_now: float, oi_past: float, price_now: float, price_past: float
) -> float:
    """方向化 OI 速度（分数率，正=看涨 positioning）。

    返回 (Δoi/oi_past) × sign(Δprice)；oi_past<=0 或价格不变 → 0.0。
    """
    if oi_past <= 0.0:
        return 0.0
    # 对称守卫：price_past/price_now <= 0 为冷启动或异常数据，无法判断方向 → 中性
    if price_past <= 0.0 or price_now <= 0.0:
        return 0.0
    d_oi = (oi_now - oi_past) / oi_past
    if price_now > price_past:
        price_sign = 1.0
    elif price_now < price_past:
        price_sign = -1.0
    else:
        return 0.0  # 价格无变化 → 无方向
    return d_oi * price_sign
