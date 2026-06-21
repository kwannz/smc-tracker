"""OKX 信号落库 + 跨所共振 confluence 集成单测（无网络，合成数据）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals import ConfluenceAggregator
from smc_tracker.storage import Store


def _store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "okx_test.db")


# ---- db roundtrip ----

def test_insert_okx_signal_roundtrip():
    """insert_okx_signal 写入后 recent_okx_signals 能查回正确数据。"""
    s = _store()
    s.insert_okx_signal(1000, "BTC", "long", "accumulation", -0.00015, 500_000.0)
    s.insert_okx_signal(2000, "ETH", "short", "distribution", 0.00020, -400_000.0)

    rows = s.recent_okx_signals(since_ms=0)
    assert len(rows) == 2

    # 第一行 BTC long
    ts0, coin0, dir0, kind0, fund0, net0 = rows[0]
    assert ts0 == 1000
    assert coin0 == "BTC"
    assert dir0 == "long"
    assert kind0 == "accumulation"
    assert abs(fund0 - (-0.00015)) < 1e-9
    assert abs(net0 - 500_000.0) < 1e-3

    # 第二行 ETH short
    ts1, coin1, dir1, kind1, fund1, net1 = rows[1]
    assert coin1 == "ETH" and dir1 == "short"
    s.close()


def test_recent_okx_signals_since_filter():
    """since_ms 过滤：只返回 ts>=since_ms 的行。"""
    s = _store()
    s.insert_okx_signal(500, "SOL", "long", "accumulation", -0.0001, 350_000.0)
    s.insert_okx_signal(1500, "SOL", "short", "distribution", 0.0002, -600_000.0)

    # since=1000 只返回第二行
    rows = s.recent_okx_signals(since_ms=1000)
    assert len(rows) == 1
    assert rows[0][1] == "SOL" and rows[0][2] == "short"
    s.close()


# ---- confluence 聚合 ----

def test_okx_confluence_btc_long():
    """okx_signals(BTC, long) + divergence(BTC, bullish→long) → BTC long 超级信号，sources 含 'OKX'。"""
    s = _store()
    # OKX 信号：BTC long（资金费空头拥挤+taker净买=吸筹bullish→long）
    s.insert_okx_signal(100, "BTC", "long", "accumulation", -0.0002, 800_000.0)
    # 背离表：BTC bullish（to_dir 映射为 long）
    s.insert_divergence((100, "BTC", "bullish", 0.5, -0.0002, 0.05, 500_000, "okx背离"))

    out = ConfluenceAggregator(s, min_sources=2).scan(now_ms=200)
    assert len(out) == 1
    sig = out[0]
    assert sig.coin == "BTC"
    assert sig.direction == "long"
    assert sig.n_sources == 2
    assert "OKX" in sig.sources
    s.close()


def test_okx_confluence_three_sources():
    """OKX + 共识 + 背离 三源同向 long → n_sources=3，sources 含 'OKX'/'共识'/'背离'。"""
    s = _store()
    s.insert_okx_signal(100, "ETH", "long", "accumulation", -0.0001, 600_000.0)
    s.insert_consensus((100, "ETH", "long", 3, 0, 2e6, 1.0, "庄"))
    s.insert_divergence((100, "ETH", "bullish", 0.4, -0.0001, 0.04, 400_000, "x"))

    out = ConfluenceAggregator(s, min_sources=2).scan(now_ms=200)
    assert len(out) == 1
    sig = out[0]
    assert sig.n_sources == 3
    assert "OKX" in sig.sources
    assert "共识" in sig.sources
    assert "背离" in sig.sources
    s.close()


def test_okx_conflict_no_signal():
    """OKX long vs 共识 short → 矛盾，不出超级信号。"""
    s = _store()
    s.insert_okx_signal(100, "SOL", "long", "accumulation", -0.0001, 500_000.0)
    s.insert_consensus((100, "SOL", "short", 3, 0, 1e6, 1.0, "庄"))

    out = ConfluenceAggregator(s, min_sources=2).scan(now_ms=200)
    assert out == []
    s.close()


def test_okx_single_source_no_signal():
    """仅 OKX 一源，不满足 min_sources=2，不出。"""
    s = _store()
    s.insert_okx_signal(100, "DOGE", "short", "distribution", 0.0003, -700_000.0)

    out = ConfluenceAggregator(s, min_sources=2).scan(now_ms=200)
    assert out == []
    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
