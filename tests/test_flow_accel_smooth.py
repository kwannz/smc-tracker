"""C.2 flow_acceleration EMA 预平滑单测（合成数据，确定性）。

断言：
- 样本不足 → flow_acceleration 返 None，predict 不崩（score 仅 book+oi）
- 注入单笔尖峰：平滑后 |accel| < 未平滑裸差（降抖效果）
- 稳定加速流入 → accel>0，predict direction=long
- 兼容：abs(flow_acceleration() or 0.0) 不抛
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals.flow_predictor import FlowPredictor

NOW = 10_000_000


def _push_uniform(fp: FlowPredictor, coin: str, amount: float, n: int,
                  start: int, interval: int) -> None:
    """均匀注入 n 笔相同金额的流向样本。"""
    for i in range(n):
        fp.push(coin, amount, start + i * interval)


# ──────────────────── 样本不足 → None ────────────────────

def test_insufficient_samples_returns_none():
    """空数据 → flow_acceleration 返 None。"""
    fp = FlowPredictor(window_ms=600_000, min_accel_samples=8)
    result = fp.flow_acceleration("X", NOW)
    assert result is None, f"expected None, got {result}"


def test_few_samples_returns_none():
    """仅 1-2 个样本，非空 bin 数 < min_accel_samples(8) → None。"""
    fp = FlowPredictor(window_ms=600_000, min_accel_samples=8)
    fp.push("X", 50_000.0, NOW - 10_000)
    fp.push("X", 50_000.0, NOW - 5_000)
    result = fp.flow_acceleration("X", NOW)
    assert result is None, f"expected None for too few bins, got {result}"


def test_predict_not_crash_when_accel_none():
    """accel=None 时 predict 不崩：score 仅由 book+oi 构成。"""
    fp = FlowPredictor(window_ms=600_000, threshold=0.1, min_accel_samples=8)
    # 不 push 任何数据 → accel=None
    # book_imbalance=0.9 (强买盘) → score=0.35*0.9+0=0.315 ≥ 0.1 → 可能给出预测
    pred = fp.predict("X", NOW, book_imbalance=0.9, oi_velocity=0.0)
    # 不抛异常是最重要的断言；若 score<threshold 也合法返回 None
    assert pred is None or pred.direction in ("long", "short")


def test_predict_reason_mentions_insufficient_samples():
    """样本不足时 predict reason 标注「流加速样本不足」。"""
    fp = FlowPredictor(window_ms=600_000, threshold=0.1, min_accel_samples=8)
    # book_imbalance=0.9 足以过 threshold（0.35*0.9=0.315>0.1）
    pred = fp.predict("X", NOW, book_imbalance=0.9)
    if pred is not None:
        assert "流加速样本不足" in pred.reason, f"reason={pred.reason!r}"


# ──────────────────── 抗峰值噪声 ────────────────────

def test_smoothed_accel_less_than_raw_spike():
    """单笔尖峰注入后，EMA 平滑加速度 < 裸 2 阶导（降抖效果）。

    构造：前半窗均匀小额，近半窗一笔极大尖峰 + 其余零。
    裸 2 阶导会被尖峰拉高；EMA 平滑后应更小。
    """
    window = 600_000
    half = window // 2

    # === 裸 2 阶导参考（使用旧接口直接测量两半窗速度差） ===
    # 直接计算：前半均匀 10k/bin × 5，近半 1 笔 1M + 零
    fp_smooth = FlowPredictor(window_ms=window, ema_alpha=0.3, min_accel_samples=3)
    fp_raw = FlowPredictor(window_ms=window, ema_alpha=1.0, min_accel_samples=3)  # alpha=1=无平滑

    # 前半窗：均匀小流入（5 笔）
    for i in range(5):
        t = NOW - window + i * (half // 5)
        fp_smooth.push("X", 10_000.0, t)
        fp_raw.push("X", 10_000.0, t)

    # 近半窗：1 笔超大尖峰 + 其余零 (4 笔)
    fp_smooth.push("X", 1_000_000.0, NOW - half + 1000)
    fp_raw.push("X", 1_000_000.0, NOW - half + 1000)
    for i in range(1, 5):
        fp_smooth.push("X", 0.0, NOW - half + 1000 + i * 10_000)
        fp_raw.push("X", 0.0, NOW - half + 1000 + i * 10_000)

    a_smooth = fp_smooth.flow_acceleration("X", NOW)
    a_raw = fp_raw.flow_acceleration("X", NOW)

    assert a_smooth is not None and a_raw is not None
    # EMA 平滑应使尖峰影响缩小
    assert abs(a_smooth) < abs(a_raw), (
        f"EMA 平滑后 |accel|={abs(a_smooth):.1f} 应 < 裸 |accel|={abs(a_raw):.1f}"
    )


# ──────────────────── 稳定加速流入 ────────────────────

def test_stable_acceleration_gives_positive_accel():
    """稳定加速流入序列 → flow_acceleration > 0，predict direction=long。

    前半窗小额，近半窗大额（递增），确保加速度显著。
    """
    window = 600_000
    half = window // 2
    fp = FlowPredictor(window_ms=window, ema_alpha=0.3, threshold=0.35,
                       min_accel_samples=3)

    # 前半：每 bin 均匀小额
    n_prior = 10
    for i in range(n_prior):
        fp.push("X", 5_000.0, NOW - window + i * (half // n_prior))

    # 后半：递增大额（加速）
    n_recent = 10
    for i in range(n_recent):
        fp.push("X", 50_000.0 + i * 10_000.0, NOW - half + i * (half // n_recent))

    accel = fp.flow_acceleration("X", NOW)
    assert accel is not None
    assert accel > 0, f"稳定加速流入 accel={accel} 应>0"

    # predict 结合正挂单意图 → long
    pred = fp.predict("X", NOW, book_imbalance=0.3)
    assert pred is not None and pred.direction == "long", (
        f"expected long, got {pred}"
    )


# ──────────────────── 兼容测试 ────────────────────

def test_abs_or_zero_compat():
    """app.py 调用模式 abs(flow_acceleration() or 0.0) 不抛。"""
    fp = FlowPredictor()
    # None case
    result = abs(fp.flow_acceleration("X", NOW) or 0.0)
    assert result == 0.0

    # non-None case
    _push_uniform(fp, "Y", 10_000.0, 10, NOW - 590_000, 60_000)
    result2 = abs(fp.flow_acceleration("Y", NOW) or 0.0)
    assert isinstance(result2, float) and result2 >= 0.0


def test_acceleration_returns_float_when_enough_samples():
    """足够样本下 flow_acceleration 返 float（非 None）。"""
    fp = FlowPredictor(window_ms=600_000, min_accel_samples=3)
    # 注入 8 个 bin 左右的样本（分散在窗口内）
    for i in range(20):
        fp.push("X", 10_000.0, NOW - 590_000 + i * 30_000)
    result = fp.flow_acceleration("X", NOW)
    assert isinstance(result, float), f"expected float, got {type(result)}"
