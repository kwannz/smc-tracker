"""前瞻资金流预测单测（合成数据，无网络）。

C.2 更新：flow_acceleration 返回 float | None（样本不足降权返 None）。
旧测试已更新以确保足够的 bin 非空（min_accel_samples 默认 8）。

C.3 新增测试：
- test_ema_reduces_noise_variance: EMA 平滑后加速度对随机噪声方差显著低于无平滑（纯传递 alpha=1.0）。
- test_trailing_causality_no_future: trailing 因果性验证——NOW 之后的样本不影响加速度计算。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

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


def test_ema_reduces_noise_variance():
    """C.3 EMA 平滑后加速度对随机单点噪声的方差显著低于无平滑（alpha=1.0 裸差值）。

    方法论：
    - 底层信号：10 个 bin 各 50k/min（均匀流，理想加速度 = 0）。
    - 噪声注入：每轮在 bin 1~9（避免 bin 0 的 EMA 首节点放大效应）随机注入 2 个
      大振幅噪声点（std=300k），共 N=50 轮（固定 seed=42 确定性）。
    - 对比：alpha=0.3 (EMA 平滑) vs alpha=1.0 (无平滑/裸加速度)。
    - 断言：EMA 方差 < 0.5 × 裸方差（实测 ~0.30，远低于阈值）。

    注：bin 0 是最旧节点，EMA 从它出发向后传播——单点 bin0 噪声会污染
    后续全部 9 个 bin 的 EMA 值，导致方差反而上升。这是 causal EMA 的固有
    性质（与 lookahead 无关，已在注释中记录为已知局限）。Bins 1~9 随机位置
    的噪声则可靠地被 EMA 衰减，这是本测试覆盖的典型场景。
    """
    window_ms = 600_000
    n_bins = 10
    bin_ms = window_ms // n_bins  # 60 000 ms / bin

    def collect_accels(ema_alpha: float, seed: int = 42, n_runs: int = 50) -> list[float]:
        rng = np.random.default_rng(seed)
        results: list[float] = []
        for _ in range(n_runs):
            fp = FlowPredictor(window_ms=window_ms, min_accel_samples=8, ema_alpha=ema_alpha)
            # 底层均匀信号：每 bin 一条 50k 记录
            for i in range(n_bins):
                fp.push("X", 50_000.0, NOW - window_ms + i * bin_ms + 1_000)
            # 2 个随机噪声点，位于 bin 1~9（不含最旧的 bin 0）
            for _ in range(2):
                spike_bin = int(rng.integers(1, n_bins))
                spike_mag = float(rng.normal(0.0, 300_000.0))
                fp.push("X", spike_mag, NOW - window_ms + spike_bin * bin_ms + 2_000)
            val = fp.flow_acceleration("X", NOW)
            if val is not None:
                results.append(val)
        return results

    accels_bare = collect_accels(1.0)   # alpha=1.0 → 无平滑（裸差值）
    accels_ema  = collect_accels(0.3)   # alpha=0.3 → EMA 平滑（默认值）

    assert len(accels_bare) == len(accels_ema) == 50, "样本数不足，检查 min_accel_samples"

    var_bare = float(np.var(accels_bare))
    var_ema  = float(np.var(accels_ema))
    ratio = var_ema / var_bare if var_bare > 0 else 1.0

    assert ratio < 0.5, (
        f"EMA 方差未充分降低：ratio={ratio:.3f}（期望 < 0.5）。"
        f" var_bare={var_bare:.3e}, var_ema={var_ema:.3e}"
    )


def test_trailing_causality_no_future():
    """C.3 trailing 因果性：now_ms 之后（含等于）的样本不参与加速度计算。

    验证 flow_acceleration 的时间过滤逻辑：
    `if ts < t0 or ts >= now_ms: continue`
    即 [t0, now_ms) 半开区间，now_ms 及之后的数据被忽略。
    """
    window_ms = 600_000
    n_bins = 10
    bin_ms = window_ms // n_bins
    min_samples = 3

    def build_fp(with_future: bool, future_mag: float = 9_999_999.0) -> FlowPredictor:
        fp = FlowPredictor(window_ms=window_ms, min_accel_samples=min_samples)
        for i in range(n_bins):
            fp.push("X", 50_000.0, NOW - window_ms + i * bin_ms + 1_000)
        if with_future:
            # 时间戳 = NOW（边界，应被排除）
            fp.push("X", future_mag, NOW)
            # 时间戳 > NOW（明确未来，应被排除）
            fp.push("X", future_mag, NOW + 60_000)
        return fp

    accel_base = build_fp(with_future=False).flow_acceleration("X", NOW)
    accel_with = build_fp(with_future=True).flow_acceleration("X", NOW)

    assert accel_base is not None, "基础加速度不应为 None（样本充足）"
    assert accel_with is not None, "带未来样本的加速度不应为 None"
    assert accel_base == accel_with, (
        f"未来样本不应影响加速度计算：base={accel_base}, with_future={accel_with}"
    )


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
