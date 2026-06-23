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



# ── PositionSize / compute_position_size 新增测试 ──────────────────────────────

from smc_tracker.signals.risk import PositionSize, compute_position_size


def test_position_size_normal():
    """正常情形：account=10000, risk_pct=1%, entry=100, stop=95。
    per_unit_risk=5, risk_usd=100, qty=20, notional=2000, leverage=0.2, capped=False。
    """
    ps = compute_position_size(10_000, 0.01, entry=100, stop=95)
    assert ps is not None
    assert abs(ps.risk_usd - 100.0) < 1e-9
    assert abs(ps.qty - 20.0) < 1e-9
    assert abs(ps.notional - 2000.0) < 1e-9
    assert abs(ps.leverage - 0.2) < 1e-9
    assert ps.capped is False


def test_position_size_capped():
    """杠杆封顶：account=1000, risk_pct=5%, entry=100, stop=99.9
    per_unit_risk=0.1, risk_usd=50, 裸qty=500, notional=50000, leverage=50 > max_leverage=10
    → 缩仓：qty = 10*1000/100 = 100, notional=10000, leverage=10, capped=True。
    """
    ps = compute_position_size(1_000, 0.05, entry=100, stop=99.9, max_leverage=10.0)
    assert ps is not None
    assert ps.capped is True
    assert abs(ps.leverage - 10.0) < 1e-9
    assert abs(ps.notional - 10_000.0) < 1e-9
    assert abs(ps.qty - 100.0) < 1e-9


def test_position_size_guard_account_zero():
    assert compute_position_size(0, 0.01, entry=100, stop=95) is None


def test_position_size_guard_account_negative():
    assert compute_position_size(-500, 0.01, entry=100, stop=95) is None


def test_position_size_guard_risk_pct_zero():
    assert compute_position_size(10_000, 0.0, entry=100, stop=95) is None


def test_position_size_guard_risk_pct_negative():
    assert compute_position_size(10_000, -0.01, entry=100, stop=95) is None


def test_position_size_guard_risk_pct_over_one():
    assert compute_position_size(10_000, 1.01, entry=100, stop=95) is None


def test_position_size_guard_entry_zero():
    assert compute_position_size(10_000, 0.01, entry=0, stop=95) is None


def test_position_size_guard_entry_negative():
    assert compute_position_size(10_000, 0.01, entry=-100, stop=95) is None


def test_position_size_guard_entry_equals_stop():
    assert compute_position_size(10_000, 0.01, entry=100, stop=100) is None


def test_position_size_capped_risk_usd_is_actual_risk():
    """🔴 审计缺陷：缩仓分支 risk_usd 应为缩仓后真实风险 qty*|entry-stop|，非 account*risk_pct。

    场景：account=1000, risk_pct=5%, entry=100, stop=99.9, max_leverage=10
      裸风险额 = 1000*0.05 = 50，裸qty = 50/0.1 = 500，leverage = 500*100/1000 = 50 > 10
      缩仓后 qty = 10*1000/100 = 100，notional = 10000，leverage = 10
      缩仓后真实风险 = 100 * |100 - 99.9| = 100 * 0.1 = 10（不是 50）
    修复前 risk_usd 返回 50（account*risk_pct）→ 高估 5 倍。
    修复后 risk_usd 应返回 10.0。
    """
    ps = compute_position_size(1_000, 0.05, entry=100, stop=99.9, max_leverage=10.0)
    assert ps is not None
    assert ps.capped is True
    # 缩仓后真实每笔风险 = qty * |entry - stop| = 100 * 0.1 = 10
    expected_risk_usd = ps.qty * abs(100.0 - 99.9)
    assert abs(ps.risk_usd - expected_risk_usd) < 1e-9, (
        f"capped 分支 risk_usd={ps.risk_usd} 不等于缩仓后真实风险 {expected_risk_usd}"
    )
    # 确认小于 account*risk_pct（即原始风险额 50）
    assert ps.risk_usd < 1_000 * 0.05, (
        f"缩仓后 risk_usd={ps.risk_usd} 应小于原始风险额 {1_000 * 0.05}"
    )


def test_position_size_uncapped_risk_usd_unchanged():
    """非缩仓分支 risk_usd 仍为 account*risk_pct（不改动原逻辑）。"""
    ps = compute_position_size(10_000, 0.01, entry=100, stop=95, max_leverage=10.0)
    assert ps is not None
    assert ps.capped is False
    assert abs(ps.risk_usd - 10_000 * 0.01) < 1e-9


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("全部通过")
