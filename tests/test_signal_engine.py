"""共振信号引擎单测（合成数据，无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals import SignalEngine
from smc_tracker.storage import Store


def _ev(type_, direction):
    return SimpleNamespace(type=type_, direction=direction)


def test_resonance_long():
    eng = SignalEngine()
    eng.set_flow("kPEPE", 300_000)            # 强净买入
    sig = eng.on_structure("kPEPE", _ev("BOS", "bull"), now_ms=1000)
    assert sig is not None and sig.direction == "long" and sig.score > 0.5


def test_resonance_short():
    eng = SignalEngine()
    eng.set_flow("WIF", -300_000)             # 强净卖出
    sig = eng.on_structure("WIF", _ev("BOS", "bear"), now_ms=1000)
    assert sig is not None and sig.direction == "short" and sig.score < -0.5


def test_disagreement_no_signal():
    """结构看多但聪明钱在卖 → 无共振，不出信号。"""
    eng = SignalEngine()
    eng.set_flow("DOGE", -300_000)
    assert eng.on_structure("DOGE", _ev("BOS", "bull"), now_ms=1000) is None


def test_below_threshold():
    """CHoCH(0.7) + 微弱流向 → 分数不足，不出。"""
    eng = SignalEngine()
    eng.set_flow("BOME", 10_000)              # tanh(0.05)≈0.05
    assert eng.on_structure("BOME", _ev("CHoCH", "bull"), now_ms=1000) is None


def test_oi_boost_pushes_over_threshold():
    eng = SignalEngine()
    eng.set_flow("SPX", 50_000)               # base≈0.47 < 0.5
    assert eng.on_structure("SPX", _ev("CHoCH", "bull"), now_ms=1000) is None
    eng.set_oi_change("SPX", 0.05)            # +0.3 信心 → 越过阈值
    sig = eng.evaluate("SPX", now_ms=2000)
    assert sig is not None and sig.direction == "long"
    assert "OI" in sig.reason


def test_cooldown_blocks_same_direction():
    eng = SignalEngine(cooldown_ms=300_000)
    eng.set_flow("kBONK", 300_000)
    assert eng.on_structure("kBONK", _ev("BOS", "bull"), now_ms=1_000) is not None
    # 冷却期内同向重复 → 拦截
    assert eng.on_structure("kBONK", _ev("BOS", "bull"), now_ms=2_000) is None
    # 冷却期后 → 再次放行
    assert eng.on_structure("kBONK", _ev("BOS", "bull"), now_ms=400_000) is not None


def test_opposite_direction_not_cooled():
    eng = SignalEngine(cooldown_ms=300_000)
    eng.set_flow("TRUMP", 300_000)
    assert eng.on_structure("TRUMP", _ev("BOS", "bull"), now_ms=1_000).direction == "long"
    eng.set_flow("TRUMP", -300_000)           # 反向共振
    sig = eng.on_structure("TRUMP", _ev("BOS", "bear"), now_ms=2_000)
    assert sig is not None and sig.direction == "short"   # 反向不受同向冷却限制


def test_zone_confluence_boost():
    """CHoCH(0.7)+中等流向 base≈0.47<阈值；OB/FVG 共振 +0.15 信心 → 越过。"""
    eng = SignalEngine()
    eng.set_flow("WIF", 50_000)
    assert eng.on_structure("WIF", _ev("CHoCH", "bull"), now_ms=1000) is None
    eng.set_zone("WIF", True)
    sig = eng.evaluate("WIF", now_ms=2000)
    assert sig is not None and "OB/FVG" in sig.reason


def test_require_sweep_hard_gate():
    """require_sweep=True：无同向扫荡则即便共振也不出信号（回测验证的过滤）。"""
    eng = SignalEngine(require_sweep=True)
    eng.set_flow("kPEPE", 300_000)
    assert eng.on_structure("kPEPE", _ev("BOS", "bull"), now_ms=1000) is None
    eng.set_sweep("kPEPE", True)
    assert eng.evaluate("kPEPE", now_ms=2000) is not None


def test_sweep_confluence_boost():
    """CHoCH(0.7)+中等流向 base≈0.47<阈值；流动性扫荡 +0.12 → 越过。"""
    eng = SignalEngine()
    eng.set_flow("kBONK", 50_000)
    assert eng.on_structure("kBONK", _ev("CHoCH", "bull"), now_ms=1000) is None
    eng.set_sweep("kBONK", True)
    sig = eng.evaluate("kBONK", now_ms=2000)
    assert sig is not None and "流动性扫荡" in sig.reason


def test_risk_attached_when_levels_set():
    eng = SignalEngine()
    eng.set_flow("kPEPE", 300_000)
    eng.set_levels("kPEPE", price=100, swing_low=98)
    sig = eng.on_structure("kPEPE", _ev("BOS", "bull"), now_ms=1000)
    assert sig is not None
    assert sig.entry == 100 and sig.stop < 100 < sig.target and sig.rr == 2.0


def test_signal_rejected_when_stop_too_far():
    eng = SignalEngine(max_stop_pct=0.08)
    eng.set_flow("WIF", 300_000)
    eng.set_levels("WIF", price=100, swing_low=80)   # 20% 止损 → 劣质 setup
    assert eng.on_structure("WIF", _ev("BOS", "bull"), now_ms=1000) is None


def test_persist_to_sqlite():
    d = tempfile.mkdtemp()
    store = Store(Path(d) / "s.db")
    eng = SignalEngine(store=store)
    eng.set_flow("kPEPE", 300_000)
    eng.on_structure("kPEPE", _ev("BOS", "bull"), now_ms=1000)
    assert store.count("signals") == 1
    rows = store.recent_signals("kPEPE")
    assert rows and rows[0][2] == "long"
    store.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
