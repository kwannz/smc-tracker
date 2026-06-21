"""庄换仓预警单测（持仓 diff：平仓/反手/减仓，合成数据无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals import WhalePositionTracker
from smc_tracker.storage import Store

PRICES = {"BTC": 60000.0, "ETH": 3000.0}
LABELS = {"0xA": "庄#1"}


def test_first_scan_is_baseline():
    t = WhalePositionTracker(min_notional=1_000_000)
    # 首轮仅建基线，不报（存量持仓不算新动作）
    assert t.scan({("0xA", "BTC"): 100.0}, PRICES, LABELS, now_ms=1) == []


def test_exit_detected():
    t = WhalePositionTracker(min_notional=1_000_000)
    t.scan({("0xA", "BTC"): 100.0}, PRICES, LABELS, now_ms=1)        # 基线：100 BTC=$6M 多
    out = t.scan({}, PRICES, LABELS, now_ms=2)                       # 归零 → 平仓
    assert len(out) == 1 and out[0].kind == "exit" and out[0].direction == "long"


def test_reversal_detected():
    t = WhalePositionTracker(min_notional=1_000_000)
    t.scan({("0xA", "BTC"): 100.0}, PRICES, LABELS, now_ms=1)        # +$6M
    out = t.scan({("0xA", "BTC"): -50.0}, PRICES, LABELS, now_ms=2)  # -$3M 反向
    assert len(out) == 1 and out[0].kind == "reversal"


def test_reduce_detected():
    t = WhalePositionTracker(min_notional=1_000_000)
    t.scan({("0xA", "BTC"): 100.0}, PRICES, LABELS, now_ms=1)        # $6M
    out = t.scan({("0xA", "BTC"): 20.0}, PRICES, LABELS, now_ms=2)   # 降到 $1.2M，缩水$4.8M≥$1M
    assert len(out) == 1 and out[0].kind == "reduce"


def test_small_position_ignored():
    t = WhalePositionTracker(min_notional=1_000_000)
    t.scan({("0xA", "ETH"): 100.0}, PRICES, LABELS, now_ms=1)        # 100 ETH=$300k < $1M
    assert t.scan({}, PRICES, LABELS, now_ms=2) == []               # 小仓位退出不报


def test_persist():
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    t = WhalePositionTracker(store=store, min_notional=1_000_000)
    t.scan({("0xA", "BTC"): 100.0}, PRICES, LABELS, now_ms=1)
    t.scan({}, PRICES, LABELS, now_ms=2)
    assert store.count("position_changes") == 1
    store.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
