"""方向化 OI 速度单测（确定性，纯函数）。

QA H4 修复：OI 量变本身无方向（持仓增加不区分多空）。方向化做法：
OI↑+价↑=新多进场(看涨,+)；OI↑+价↓=新空进场(看跌,−)；OI↓=平仓(符号翻转)。
= (Δoi/oi_past) × sign(Δprice)，作为方向化分数率喂 FlowPredictor.predict(oi_velocity=...)。
"""
from __future__ import annotations

from smc_tracker.signals.oi_velocity import oi_directional_velocity


def test_oi_up_price_up_bullish():
    """OI 升 + 价升 = 新多 → 正。"""
    v = oi_directional_velocity(oi_now=1050.0, oi_past=1000.0, price_now=110.0, price_past=100.0)
    assert v > 0.0


def test_oi_up_price_down_bearish():
    """OI 升 + 价跌 = 新空 → 负。"""
    v = oi_directional_velocity(oi_now=1050.0, oi_past=1000.0, price_now=90.0, price_past=100.0)
    assert v < 0.0


def test_oi_down_price_up_negative():
    """OI 降 + 价升 = 空头回补(弱) → 负（Δoi<0 × +1）。"""
    v = oi_directional_velocity(oi_now=950.0, oi_past=1000.0, price_now=110.0, price_past=100.0)
    assert v < 0.0


def test_magnitude_scales_with_oi_change():
    """OI 变化越大幅度越大（5% vs 1%）。"""
    big = oi_directional_velocity(1050.0, 1000.0, 110.0, 100.0)   # +5%
    small = oi_directional_velocity(1010.0, 1000.0, 110.0, 100.0)  # +1%
    assert abs(big) > abs(small)


def test_zero_oi_past_returns_zero():
    """oi_past<=0 → 0.0（防除零，无数据）。"""
    assert oi_directional_velocity(100.0, 0.0, 110.0, 100.0) == 0.0


def test_flat_price_returns_zero():
    """价格不变 → sign(Δprice)=0 → 0.0。"""
    assert oi_directional_velocity(1050.0, 1000.0, 100.0, 100.0) == 0.0


def test_cold_start_price_past_zero_returns_neutral():
    """冷启动 price_past=0（app 默认 _last_close=0）且 OI 升 → 应返回 0.0 中性，不偏多。

    Bug：原实现只守 oi_past<=0，price_past=0 时 price_now>0=price_past 触发 price_sign=+1.0，
    OI 速度被静默偏置为看涨，造成冷启动偏多。
    """
    assert oi_directional_velocity(1050.0, 1000.0, 110.0, 0.0) == 0.0


def test_cold_start_price_now_zero_returns_neutral():
    """price_now=0（异常数据）→ 应返回 0.0 中性，不产生虚假看跌信号。"""
    assert oi_directional_velocity(1050.0, 1000.0, 0.0, 100.0) == 0.0
