"""reconcile_universe 对账纯函数单测（热载入增删逻辑核心）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import reconcile_universe


def test_add_only():
    added, removed = reconcile_universe({"BTC": "BTCUSDT"}, {"BTC": "BTCUSDT", "ETH": "ETHUSDT"})
    assert added == {"ETH": "ETHUSDT"}
    assert removed == set()


def test_remove_only():
    added, removed = reconcile_universe({"BTC": "BTCUSDT", "ETH": "ETHUSDT"}, {"BTC": "BTCUSDT"})
    assert added == {}
    assert removed == {"ETH"}


def test_add_and_remove():
    added, removed = reconcile_universe({"BTC": "BTCUSDT"}, {"ETH": "ETHUSDT"})
    assert added == {"ETH": "ETHUSDT"}
    assert removed == {"BTC"}


def test_symbol_change_is_add():
    added, removed = reconcile_universe({"BTC": "BTCUSDT"}, {"BTC": "BTCUSDT_NEW"})
    assert added == {"BTC": "BTCUSDT_NEW"}
    assert removed == set()


def test_empty_target_removes_all():
    added, removed = reconcile_universe({"BTC": "BTCUSDT", "ETH": "ETHUSDT"}, {})
    assert added == {}
    assert removed == {"BTC", "ETH"}


def test_no_change():
    added, removed = reconcile_universe({"BTC": "BTCUSDT"}, {"BTC": "BTCUSDT"})
    assert added == {} and removed == set()
