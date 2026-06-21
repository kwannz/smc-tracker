"""前瞻资金流预测单测（合成数据，无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals.flow_predictor import FlowPredictor, orderbook_imbalance

NOW = 10_000_000


def test_orderbook_imbalance_bid_heavy():
    bids = [{"px": 100, "sz": 10}] * 15
    asks = [{"px": 101, "sz": 1}] * 15
    r = orderbook_imbalance(bids, asks)
    assert r["imbalance"] > 0.5 and r["bid_usd"] > r["ask_usd"]


def test_orderbook_imbalance_ask_heavy():
    bids = [{"px": 100, "sz": 1}] * 15
    asks = [{"px": 101, "sz": 10}] * 15
    assert orderbook_imbalance(bids, asks)["imbalance"] < -0.5


def test_accelerating_inflow_predicts_long():
    fp = FlowPredictor(accel_scale=100_000, threshold=0.35, window_ms=600_000)
    for t in range(NOW - 590_000, NOW - 300_000, 60_000):
        fp.push("X", 10_000, t)                 # 前半窗：小幅流入
    for t in range(NOW - 290_000, NOW, 30_000):
        fp.push("X", 100_000, t)                # 近半窗：大幅流入 → 加速
    p = fp.predict("X", NOW, book_imbalance=0.3)
    assert p is not None and p.direction == "long" and p.flow_accel > 0


def test_accelerating_outflow_predicts_short():
    fp = FlowPredictor(accel_scale=100_000, threshold=0.35, window_ms=600_000)
    for t in range(NOW - 290_000, NOW, 30_000):
        fp.push("X", -100_000, t)               # 加速流出
    p = fp.predict("X", NOW, book_imbalance=-0.3)
    assert p is not None and p.direction == "short"


def test_flat_no_prediction():
    fp = FlowPredictor()
    for t in range(NOW - 590_000, NOW, 60_000):
        fp.push("X", 1_000, t)
    assert fp.predict("X", NOW, book_imbalance=0.0) is None


def test_conflict_filtered():
    """资金加速流入 但 挂单卖盘厚 → 矛盾,不预测(避免假信号)。"""
    fp = FlowPredictor(threshold=0.1)
    for t in range(NOW - 290_000, NOW, 30_000):
        fp.push("X", 100_000, t)
    assert fp.predict("X", NOW, book_imbalance=-0.8) is None


def test_acceleration_sign():
    fp = FlowPredictor(window_ms=600_000)
    for t in range(NOW - 290_000, NOW, 30_000):
        fp.push("X", 50_000, t)                 # 仅近半窗有流入 → 加速>0
    assert fp.flow_acceleration("X", NOW) > 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
