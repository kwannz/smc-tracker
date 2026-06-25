"""dashboard 监控清单 API 纯逻辑单测（tmp db，无 HTTP）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.dashboard_monitored import apply_monitored_action, render_monitored_page
from smc_tracker.storage import Store


def test_monitored_page_self_contained():
    """迷你页自包含：含表单 + fetch /api/monitored，无 CDN 外链。"""
    html = render_monitored_page()
    assert "/api/monitored" in html
    assert "doAdd" in html and "doRm" in html
    assert "http://" not in html and "https://" not in html  # 无外链依赖


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
