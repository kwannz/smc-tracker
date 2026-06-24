"""harmonic_collected 表（「发现搜集」的币）读写单测。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from smc_tracker.storage import Store


def _store():
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


def test_add_and_get_collected():
    s = _store()
    s.add_harmonic_collected([("DOGE", "DOGEUSDT", 1000), ("LINK", "LINKUSDT", 1000)])
    got = s.get_harmonic_collected()
    assert got == {"DOGE": "DOGEUSDT", "LINK": "LINKUSDT"}


def test_add_is_idempotent_upsert():
    """同 coin 重复加 → 不重复（PK upsert）。"""
    s = _store()
    s.add_harmonic_collected([("DOGE", "DOGEUSDT", 1000)])
    s.add_harmonic_collected([("DOGE", "DOGEUSDT", 2000)])
    assert s.get_harmonic_collected() == {"DOGE": "DOGEUSDT"}


def test_empty_add_safe():
    s = _store()
    s.add_harmonic_collected([])
    assert s.get_harmonic_collected() == {}
