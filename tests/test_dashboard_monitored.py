"""dashboard 监控清单 API 纯逻辑单测（tmp db，无 HTTP）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.dashboard import apply_monitored_action
from smc_tracker.storage import Store


def _store():
    return Store(Path(tempfile.mkdtemp()) / "t.db")


def test_add_then_list():
    s = _store()
    r = apply_monitored_action(s, "add", ["BTC", "eth"], "core", 123)
    assert r["changed"] == 2
    r2 = apply_monitored_action(s, "list", [], "", 0)
    coins = {row["coin"] for row in r2["monitored"]}
    assert coins == {"BTC", "ETH"}  # 大写归一


def test_rm():
    s = _store()
    apply_monitored_action(s, "add", ["BTC", "ETH"], "", 1)
    r = apply_monitored_action(s, "rm", ["BTC"], "", 0)
    assert r["changed"] == 1
    coins = {row["coin"] for row in r["monitored"]}
    assert coins == {"ETH"}


def test_list_empty():
    s = _store()
    r = apply_monitored_action(s, "list", [], "", 0)
    assert r["monitored"] == []
    assert r["changed"] == 0
