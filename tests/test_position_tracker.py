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


def test_no_exit_when_price_missing():
    """缺价时上轮有持仓的 coin 不应误报 exit（沿用上轮，跳过本轮）。
    复现 bug：prices 里没有 BTC → current 里没有 (0xA, BTC) →
    与上轮 diff 时 new=0 → 本应被当作「缺价，无法判断」而跳过，
    但旧代码直接把 new=0 传给 _classify → 误报 exit。
    """
    t = WhalePositionTracker(min_notional=1_000_000)
    # 基线：$6M 多仓
    t.scan({("0xA", "BTC"): 100.0}, PRICES, LABELS, now_ms=1)
    # 第二轮：BTC 仓位数据还在，但 prices 里没有 BTC（缺价） → 不应报 exit
    no_btc_prices: dict[str, float] = {"ETH": 3000.0}
    out = t.scan({("0xA", "BTC"): 100.0}, no_btc_prices, LABELS, now_ms=2)
    assert out == [], f"缺价时不应 emit exit，但得到 {out}"


def test_prev_preserved_after_missing_price():
    """缺价轮之后，价格恢复时仍能正确 diff（prev 沿用上轮而非归零）。"""
    t = WhalePositionTracker(min_notional=1_000_000)
    # 基线
    t.scan({("0xA", "BTC"): 100.0}, PRICES, LABELS, now_ms=1)
    # 缺价轮 → 应无事件，prev 应保持 $6M
    no_btc_prices: dict[str, float] = {"ETH": 3000.0}
    t.scan({("0xA", "BTC"): 100.0}, no_btc_prices, LABELS, now_ms=2)
    # 价格恢复，仓位归零 → 现在才应该 exit
    out = t.scan({}, PRICES, LABELS, now_ms=3)
    assert len(out) == 1 and out[0].kind == "exit", (
        f"价格恢复后平仓应 emit exit，但得到 {out}")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
