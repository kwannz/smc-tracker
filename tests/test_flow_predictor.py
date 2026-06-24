"""前瞻资金流预测单测（合成数据，无网络）。

C.2 更新：flow_acceleration 返回 float | None（样本不足降权返 None）。
旧测试已更新以确保足够的 bin 非空（min_accel_samples 默认 8）。
"""
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
    """前半小流入 + 后半大流入 → accel>0 → predict long。

    需确保 ≥ min_accel_samples(8) 个 bin 非空：窗口 600s/10bins=60s/bin，
    每 bin 至少一条记录。前半 5 bins 各放一条，后半 5 bins 各放多条。
    """
    fp = FlowPredictor(accel_scale=100_000, threshold=0.35, window_ms=600_000,
                       min_accel_samples=8)
    # 前半 5 bins（每 bin 60s）：均匀小流入
    for i in range(5):
        fp.push("X", 10_000, NOW - 600_000 + i * 60_000 + 1000)
    # 后半 5 bins：大流入（每 bin 多条）
    for i in range(5):
        for j in range(3):
            fp.push("X", 100_000, NOW - 300_000 + i * 60_000 + j * 5_000)
    p = fp.predict("X", NOW, book_imbalance=0.3)
    assert p is not None and p.direction == "long" and p.flow_accel > 0


def test_accelerating_outflow_predicts_short():
    """前半小流出 + 后半大流出 → predict short。"""
    fp = FlowPredictor(accel_scale=100_000, threshold=0.35, window_ms=600_000,
                       min_accel_samples=8)
    # 前半 5 bins：小流出
    for i in range(5):
        fp.push("X", -10_000, NOW - 600_000 + i * 60_000 + 1000)
    # 后半 5 bins：大流出
    for i in range(5):
        for j in range(3):
            fp.push("X", -100_000, NOW - 300_000 + i * 60_000 + j * 5_000)
    p = fp.predict("X", NOW, book_imbalance=-0.3)
    assert p is not None and p.direction == "short"


def test_flat_no_prediction():
    """均匀小流入全窗口 → 无加速度 → score 不足 threshold → None。"""
    fp = FlowPredictor(min_accel_samples=3)
    for t in range(NOW - 590_000, NOW, 30_000):
        fp.push("X", 1_000, t)
    assert fp.predict("X", NOW, book_imbalance=0.0) is None


def test_conflict_filtered():
    """资金加速流入 但 挂单卖盘厚 → 矛盾不预测（accel_sig * book_imbalance < -0.04）。

    需确保 accel 非 None：在全窗口均匀分布足够 bin 的样本。
    """
    fp = FlowPredictor(threshold=0.1, min_accel_samples=3)
    # 前半少量，后半大量 → accel_sig > 0
    for i in range(5):
        fp.push("X", 5_000, NOW - 600_000 + i * 60_000 + 1000)
    for i in range(5):
        for j in range(2):
            fp.push("X", 100_000, NOW - 300_000 + i * 60_000 + j * 5_000)
    pred = fp.predict("X", NOW, book_imbalance=-0.8)
    assert pred is None, f"资金加速流入+卖盘厚 应矛盾过滤，got {pred}"


def test_acceleration_sign():
    """仅后半窗有流入（前半无）→ EMA 平滑加速度 > 0。

    需 min_accel_samples=3 使后半 bins 足够。
    """
    fp = FlowPredictor(window_ms=600_000, min_accel_samples=3)
    # 后半窗 5 bins 各放样本
    for i in range(5):
        fp.push("X", 50_000, NOW - 300_000 + i * 60_000 + 1000)
    result = fp.flow_acceleration("X", NOW)
    assert result is not None and result > 0, (
        f"仅后半窗流入应使加速度>0，got {result}"
    )


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
