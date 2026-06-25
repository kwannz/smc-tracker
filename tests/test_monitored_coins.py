"""monitored_coins 表（监控币种清单）读写 + 迁移单测。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store


def _store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


def test_add_and_get():
    s = _store()
    s.add_monitored_coins([("BTC", "BTCUSDT", 1000, "core"),
                           ("ETH", "ETHUSDT", 1000, "")])
    assert s.get_monitored_coins() == {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


def test_add_idempotent_upsert():
    s = _store()
    s.add_monitored_coins([("BTC", "BTCUSDT", 1000, "a")])
    s.add_monitored_coins([("BTC", "BTCUSDT", 2000, "b")])  # 同 coin 覆盖
    assert s.get_monitored_coins() == {"BTC": "BTCUSDT"}
    rows = s.list_monitored_coins()
    assert len(rows) == 1
    assert rows[0][3] == "b"  # note 被更新


def test_remove_returns_count():
    s = _store()
    s.add_monitored_coins([("BTC", "BTCUSDT", 1, ""), ("ETH", "ETHUSDT", 1, "")])
    assert s.remove_monitored_coins(["BTC", "NOPE"]) == 1  # 只 BTC 命中
    assert s.get_monitored_coins() == {"ETH": "ETHUSDT"}


def test_list_sorted_by_added_ts():
    s = _store()
    s.add_monitored_coins([("ETH", "ETHUSDT", 200, ""), ("BTC", "BTCUSDT", 100, "")])
    coins = [r[0] for r in s.list_monitored_coins()]
    assert coins == ["BTC", "ETH"]  # added_ts 升序


def test_empty_ops_safe():
    s = _store()
    s.add_monitored_coins([])
    assert s.remove_monitored_coins([]) == 0
    assert s.get_monitored_coins() == {}
    assert s.list_monitored_coins() == []


def test_migration_from_harmonic_collected():
    """旧库有 harmonic_collected、monitored_coins 空 → 迁移拷入。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "t.db"
    s = Store(p)
    s.add_harmonic_collected([("DOGE", "DOGEUSDT", 5)])
    s.close()
    # 重开库触发迁移（monitored_coins 仍空）
    s2 = Store(p)
    assert s2.get_monitored_coins() == {"DOGE": "DOGEUSDT"}
    rows = s2.list_monitored_coins()
    assert rows[0][3] == "migrated:harmonic_collected"


def test_migration_not_retriggered_after_clear():
    """P2-2：迁移后用户清空清单 → 重开库不复活旧币（user_version 哨兵）。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "t.db"
    s = Store(p)
    s.add_harmonic_collected([("DOGE", "DOGEUSDT", 5)])
    s.close()
    s2 = Store(p)                       # 迁移：DOGE 进 monitored_coins + 置哨兵
    assert s2.get_monitored_coins() == {"DOGE": "DOGEUSDT"}
    s2.remove_monitored_coins(["DOGE"])  # 用户清空（合法操作）
    s2.close()
    s3 = Store(p)                       # 重开：哨兵已置 → 不再迁移
    assert s3.get_monitored_coins() == {}  # DOGE 未复活
