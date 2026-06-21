"""多庄共识 + 持仓面板 单测（合成数据，无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals import WhaleConsensus, positioning
from smc_tracker.storage import Store

PRICES = {"BTC": 60000.0, "PEPE": 0.00001}
LABELS = {"0xA": "庄#1", "0xB": "庄#2", "0xC": "庄#3", "0xD": "庄#4"}


def test_positioning_panel():
    pos = {("0xA", "BTC"): 1.0, ("0xB", "BTC"): 2.0, ("0xC", "BTC"): -0.5}
    panel = positioning(pos, PRICES, LABELS)
    btc = panel[0]
    assert btc.coin == "BTC" and btc.n_long == 2 and btc.n_short == 1
    # 净名义 = (1+2-0.5)*60000 = 150000
    assert abs(btc.net_notional - 150_000) < 1e-6


def test_consensus_fires_on_majority():
    # 3 庄做多 BTC，1 庄做空 → 3≥2×1，净名义大 → 看多共识
    pos = {("0xA", "BTC"): 5.0, ("0xB", "BTC"): 5.0, ("0xC", "BTC"): 5.0,
           ("0xD", "BTC"): -1.0}
    wc = WhaleConsensus(min_consensus=3, min_net_notional=200_000)
    sigs = wc.scan(pos, PRICES, LABELS, now_ms=1000)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.coin == "BTC" and s.direction == "long" and s.n_agree == 3 and s.n_oppose == 1


def test_no_consensus_when_split():
    # 2 多 2 空 → 非明显多数 → 不出
    pos = {("0xA", "BTC"): 5.0, ("0xB", "BTC"): 5.0,
           ("0xC", "BTC"): -5.0, ("0xD", "BTC"): -5.0}
    wc = WhaleConsensus(min_consensus=3)
    assert wc.scan(pos, PRICES, LABELS, now_ms=1000) == []


def test_below_min_consensus():
    pos = {("0xA", "BTC"): 5.0, ("0xB", "BTC"): 5.0}   # 仅 2 庄 < min 3
    wc = WhaleConsensus(min_consensus=3)
    assert wc.scan(pos, PRICES, LABELS, now_ms=1000) == []


def test_cooldown_and_persist():
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    pos = {("0xA", "BTC"): 5.0, ("0xB", "BTC"): 5.0, ("0xC", "BTC"): 5.0}
    wc = WhaleConsensus(store=store, min_consensus=3, min_net_notional=200_000,
                        cooldown_ms=1_000_000)
    assert len(wc.scan(pos, PRICES, LABELS, now_ms=1000)) == 1
    assert store.count("consensus") == 1
    # 冷却期内不重复
    assert wc.scan(pos, PRICES, LABELS, now_ms=2000) == []
    store.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
