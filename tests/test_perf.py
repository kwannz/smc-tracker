"""延迟埋点单测：分位数正确性 + 环形覆盖 + NaN/inf 守卫 + 多阶段格式化。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.perf import LatencyTracker


def test_empty_stats_none():
    t = LatencyTracker()
    assert t.stats("x") is None
    assert t.fmt() == ""


def test_percentiles_on_known_samples():
    t = LatencyTracker()
    for v in range(1, 101):              # 1..100
        t.record("s", float(v))
    st = t.stats("s")
    assert st["n"] == 100
    assert abs(st["p50"] - 50.5) < 1e-9
    assert st["max"] == 100.0
    assert abs(st["mean"] - 50.5) < 1e-9
    assert 99.0 <= st["p99"] <= 100.0


def test_ring_buffer_overwrites_oldest():
    t = LatencyTracker(capacity=8)
    for v in range(100):                 # 远超容量
        t.record("s", float(v))
    st = t.stats("s")
    assert st["n"] == 8                   # 只保留最近 8 个(92..99)
    assert st["max"] == 99.0
    assert st["p50"] >= 92.0


def test_nan_inf_guard():
    t = LatencyTracker()
    t.record("s", float("nan"))
    t.record("s", float("inf"))
    assert t.stats("s") is None          # 非有限值被忽略
    t.record("s", 3.0)
    assert t.stats("s")["n"] == 1


def test_multi_stage_fmt():
    t = LatencyTracker()
    t.record("接收→处理", 1.5)
    t.record("信号计算", 0.5)
    out = t.fmt()
    assert "接收→处理" in out and "信号计算" in out
    assert "P50=" in out and "P99=" in out and "max=" in out


def test_record_is_independent_per_stage():
    t = LatencyTracker()
    t.record("a", 10.0)
    t.record("b", 20.0)
    assert t.stats("a")["max"] == 10.0
    assert t.stats("b")["max"] == 20.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
