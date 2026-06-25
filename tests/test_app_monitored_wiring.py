"""app 监控集热载入对账应用单测（不起网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.app import _apply_reconcile


class _FakeMon:
    def __init__(self, c2s):
        self.coin_to_symbol = dict(c2s)
        self.top_n = len(c2s)


def test_apply_reconcile_adds_and_removes():
    mon = _FakeMon({"BTC": "BTCUSDT", "ETH": "ETHUSDT"})
    changed = _apply_reconcile(mon, {"BTC": "BTCUSDT", "SOL": "SOLUSDT"})
    assert mon.coin_to_symbol == {"BTC": "BTCUSDT", "SOL": "SOLUSDT"}
    assert mon.top_n == 2
    assert changed is True


def test_apply_reconcile_noop_returns_false():
    mon = _FakeMon({"BTC": "BTCUSDT"})
    changed = _apply_reconcile(mon, {"BTC": "BTCUSDT"})
    assert changed is False
    assert mon.coin_to_symbol == {"BTC": "BTCUSDT"}
