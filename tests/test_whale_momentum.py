"""庄 PnL 动量追踪单测（合成快照，无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.whale_momentum import WhaleMomentum
from smc_tracker.storage import Store


def _store():
    return Store(Path(tempfile.mkdtemp()) / "s.db")


# row = (addr, label, day, week, month, alltime, acct)
def test_momentum_detects_pnl_gain():
    s = _store(); wm = WhaleMomentum(s)
    wm.snapshot([("0xa", "庄A", 100, 500, 1000, 5_000_000, 2_000_000)], now_ms=1000)
    rows2 = [("0xa", "庄A", 200, 600, 1100, 5_300_000, 2_200_000)]   # 全期 +30万
    wm.snapshot(rows2, now_ms=3_700_000)
    m = wm.momentum(rows2, now_ms=3_700_000, window_ms=3_600_000, min_change=100_000)
    assert len(m) == 1 and abs(m[0].pnl_change - 300_000) < 1 and m[0].hot
    assert 0.9 < m[0].hours < 1.2
    s.close()


def test_momentum_below_threshold():
    s = _store(); wm = WhaleMomentum(s)
    wm.snapshot([("0xa", "A", 0, 0, 0, 5_000_000, 2_000_000)], now_ms=1000)
    rows2 = [("0xa", "A", 0, 0, 0, 5_010_000, 2_005_000)]            # 仅 +1万 < 阈值
    wm.snapshot(rows2, now_ms=3_700_000)
    assert wm.momentum(rows2, now_ms=3_700_000, min_change=100_000) == []
    s.close()


def test_momentum_no_prior_snapshot():
    s = _store(); wm = WhaleMomentum(s)
    rows = [("0xa", "A", 0, 0, 0, 5_000_000, 2_000_000)]
    wm.snapshot(rows, now_ms=1000)
    assert wm.momentum(rows, now_ms=1000, window_ms=3_600_000) == []   # 无更早快照
    s.close()


def test_hot_now_ranks_by_day():
    rows = [("0xa", "A", 500, 0, 0, 1, 1), ("0xb", "B", 1000, 0, 0, 1, 1),
            ("0xc", "C", 100, 0, 0, 1, 1)]
    hot = WhaleMomentum.hot_now(rows, limit=2)
    assert hot[0][0] == "0xb" and hot[1][0] == "0xa"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
