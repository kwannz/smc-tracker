"""信号风险参数单测（compute_risk，纯函数）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals.risk import compute_risk


def test_long_uses_tightest_structure_stop():
    # price=100，swing_low=95、ob_bottom=96 → 取最紧(96)做止损基准
    p = compute_risk("long", 100, swing_low=95, swing_high=0,
                     ob_bottom=96, ob_top=0, target_rr=2.0)
    assert p is not None
    assert abs(p.stop - 96 * 0.999) < 1e-6
    risk = 100 - p.stop
    assert abs(p.target - (100 + 2 * risk)) < 1e-6
    assert p.rr == 2.0 and p.stop < 100 < p.target


def test_short_uses_structure_above():
    p = compute_risk("short", 100, swing_low=0, swing_high=105,
                     ob_bottom=0, ob_top=104, target_rr=2.0)
    assert p is not None
    assert abs(p.stop - 104 * 1.001) < 1e-6
    assert p.target < 100 < p.stop


def test_reject_when_stop_too_far():
    # swing_low=80 → 止损约 20% > max_stop_pct=8% → 拒绝
    assert compute_risk("long", 100, swing_low=80, swing_high=0,
                        ob_bottom=0, ob_top=0, max_stop_pct=0.08) is None


def test_default_stop_when_no_levels():
    p = compute_risk("long", 100, 0, 0, 0, 0, default_stop_pct=0.02)
    assert p is not None and abs(p.stop - 100 * (1 - 0.02)) < 1e-6


def test_invalid_inputs():
    assert compute_risk("long", 0, 0, 0, 0, 0) is None
    assert compute_risk("flat", 100, 0, 0, 0, 0) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
