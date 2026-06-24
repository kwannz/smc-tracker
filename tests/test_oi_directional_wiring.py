"""C.3 OI 方向化接线断言（合成数据，无网络）。

断言：OI↑价↑ → predict 收正 oi 贡献(direction 偏 long)；
     OI↑价↓ → 偏 short；无价史 → oi_sig=0 不崩。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals.flow_predictor import FlowPredictor
from smc_tracker.signals.oi_velocity import oi_directional_velocity

NOW = 10_000_000


# ──────────────────── oi_directional_velocity 单元 ────────────────────

def test_oi_up_price_up_bullish():
    """OI↑ + 价↑ = 新多进场 → 正值（看涨）。"""
    v = oi_directional_velocity(oi_now=1100.0, oi_past=1000.0,
                                price_now=52000.0, price_past=50000.0)
    assert v > 0.0, f"OI↑价↑应为正，got {v}"
    # 数值验算：Δoi/oi_past = 0.1，sign=+1 → 0.1
    assert abs(v - 0.1) < 1e-9, f"expected 0.1, got {v}"


def test_oi_up_price_down_bearish():
    """OI↑ + 价↓ = 新空进场 → 负值（看跌）。"""
    v = oi_directional_velocity(oi_now=1100.0, oi_past=1000.0,
                                price_now=48000.0, price_past=50000.0)
    assert v < 0.0, f"OI↑价↓应为负，got {v}"
    assert abs(v - (-0.1)) < 1e-9, f"expected -0.1, got {v}"


def test_oi_down_price_up_bearish():
    """OI↓ + 价↑ = 多头平仓 → 负值。"""
    v = oi_directional_velocity(oi_now=900.0, oi_past=1000.0,
                                price_now=52000.0, price_past=50000.0)
    assert v < 0.0, f"OI↓价↑（多平仓）应为负，got {v}"


def test_oi_no_price_change_zero():
    """价格不变 → 0.0（无方向信息）。"""
    v = oi_directional_velocity(1100.0, 1000.0, 50000.0, 50000.0)
    assert v == 0.0


def test_oi_past_zero_zero():
    """oi_past ≤ 0 → 0.0（安全，不除零）。"""
    v = oi_directional_velocity(1100.0, 0.0, 52000.0, 50000.0)
    assert v == 0.0


# ──────────────────── predict 接线断言 ────────────────────

def _fp_with_enough_samples() -> FlowPredictor:
    """构造有足够 accel 样本的 FlowPredictor（确保 accel 非 None）。"""
    fp = FlowPredictor(accel_scale=100_000, threshold=0.1, window_ms=600_000,
                       min_accel_samples=3)
    # 均匀分布到全窗口（确保多 bins 非空）
    for i in range(10):
        fp.push("X", 5_000.0, NOW - 600_000 + i * 60_000 + 1000)
    return fp


def test_oi_positive_biases_long():
    """OI↑价↑ → oi_vel>0 → predict 收正 oi 贡献 → direction=long。

    book_imbalance=0.3(看涨) + oi_vel=0.1(看涨) + 小额 accel → score>0 → long。
    """
    fp = _fp_with_enough_samples()
    oi_vel = oi_directional_velocity(1100.0, 1000.0, 52000.0, 50000.0)  # +0.1
    pred = fp.predict("X", NOW, book_imbalance=0.3, oi_velocity=oi_vel)
    assert pred is not None, "应给出预测"
    assert pred.direction == "long", f"OI↑价↑+看涨挂单 应预测 long，got {pred.direction}"
    assert pred.oi_velocity == oi_vel


def test_oi_negative_biases_short():
    """OI↑价↓ → oi_vel<0 → predict 收负 oi 贡献 → direction=short。"""
    fp = _fp_with_enough_samples()
    oi_vel = oi_directional_velocity(1100.0, 1000.0, 48000.0, 50000.0)  # -0.1
    pred = fp.predict("X", NOW, book_imbalance=-0.3, oi_velocity=oi_vel)
    assert pred is not None, "应给出预测"
    assert pred.direction == "short", f"OI↑价↓+卖盘挂单 应预测 short，got {pred.direction}"


def test_no_price_history_oi_zero_not_crash():
    """无价历（oi_past=0）→ oi_directional_velocity=0 → predict 不崩（oi_sig=0）。"""
    fp = _fp_with_enough_samples()
    oi_vel = oi_directional_velocity(1100.0, 0.0, 52000.0, 50000.0)  # 无价史 → 0
    assert oi_vel == 0.0
    # predict 不应抛异常
    pred = fp.predict("X", NOW, book_imbalance=0.3, oi_velocity=oi_vel)
    # 不崩即为通过（可能返回 None 或有效 pred）
    assert pred is None or pred.direction in ("long", "short")
