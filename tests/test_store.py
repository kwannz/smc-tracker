"""SQLite 存储层单元测试（用临时库，无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store


def _store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


def test_contracts_upsert():
    s = _store()
    s.upsert_contract("PEPE", "ERC20", "0x6982", 1000)
    s.upsert_contract("PEPE", "ERC20", "0x6982NEW", 2000)   # 同链更新
    s.upsert_contract("PEPE", "SOL", "abc", 1500)
    rows = s.contracts("PEPE")
    assert ("PEPE", "ERC20", "0x6982NEW") in rows
    assert ("PEPE", "SOL", "abc") in rows
    assert len(rows) == 2
    s.close()


def test_oi_change():
    s = _store()
    s.insert_oi([("DOGEUSDT", "DOGE", 1000.0, 84.0, 0.084, 0.0001, 10_000)])
    s.insert_oi([("DOGEUSDT", "DOGE", 1200.0, 100.8, 0.084, 0.0001, 70_000)])
    latest, past = s.oi_change("DOGEUSDT", window_ms=60_000, now_ms=70_000)
    assert latest == 1200.0 and past == 1000.0
    s.close()


def test_hl_meme_trades_top_takers():
    s = _store()
    # A 主动买 100，再主动买 50；B 主动卖 200
    rows = [
        ("kPEPE", 0.0028, 1e6, 100.0, "B", "0xA", "0xM", "0xA", "h1", 1, 1),
        ("kPEPE", 0.0028, 5e5, 50.0,  "B", "0xA", "0xM", "0xA", "h2", 2, 2),
        ("kPEPE", 0.0028, 2e6, 200.0, "A", "0xM", "0xB", "0xB", "h3", 3, 3),
    ]
    s.insert_hl_meme_trades(rows)
    assert s.count("hl_meme_trades") == 3
    top = s.top_meme_takers("kPEPE", since_ms=0, limit=10)
    d = dict(top)
    assert abs(d["0xA"] - 150.0) < 1e-9     # 净买 +150
    assert abs(d["0xB"] - (-200.0)) < 1e-9  # 净卖 -200
    s.close()


def test_sm_event_insert():
    s = _store()
    s.insert_sm_event((1, "OPEN", "0xabc", "whale", "BTC", "BUY",
                       1.0, 100.0, 100.0, 0.0, 1.0, 0.0, 1))
    assert s.count("sm_events") == 1
    s.close()


def test_insert_flow_prediction_roundtrip():
    """前瞻预测落库+查回往返：7 字段完整写入后可正确读出。"""
    s = _store()
    row = (1000, "PEPE", "long", 0.72, 5000.0, 1500.0, 0.35)
    s.insert_flow_prediction(row)
    assert s.count("flow_predictions") == 1
    result = s.conn.execute(
        "SELECT ts,coin,direction,score,vel,accel,book_imb FROM flow_predictions"
    ).fetchone()
    assert result is not None
    ts, coin, direction, score, vel, accel, book_imb = result
    assert ts == 1000
    assert coin == "PEPE"
    assert direction == "long"
    assert abs(score - 0.72) < 1e-9
    assert abs(vel - 5000.0) < 1e-9
    assert abs(accel - 1500.0) < 1e-9
    assert abs(book_imb - 0.35) < 1e-9
    s.close()


def test_insert_flow_prediction_multiple_coins():
    """多条前瞻预测落库，按 coin+ts 索引可正确筛选。"""
    s = _store()
    s.insert_flow_prediction((100, "DOGE", "long",  0.50, 1000.0, 200.0, 0.20))
    s.insert_flow_prediction((200, "WIF",  "short", -0.60, -800.0, -300.0, -0.40))
    s.insert_flow_prediction((300, "DOGE", "long",  0.55, 1100.0, 250.0, 0.25))
    assert s.count("flow_predictions") == 3
    doge_rows = s.conn.execute(
        "SELECT coin, direction FROM flow_predictions WHERE coin=? ORDER BY ts",
        ("DOGE",),
    ).fetchall()
    assert len(doge_rows) == 2
    assert all(r[0] == "DOGE" and r[1] == "long" for r in doge_rows)
    s.close()


def test_prune_before_removes_old_and_keeps_new():
    """prune_before 删旧行、留新行，返回正确删除数。"""
    s = _store()
    now_ms = 8 * 86_400_000   # 模拟"现在"= 第 8 天（毫秒）
    day8_ago = 0               # 8 天前
    day1_ago = 7 * 86_400_000  # 1 天前
    # 插入两条 bitget_oi：8 天前 + 1 天前
    s.insert_oi([("DOGEUSDT", "DOGE", 1000.0, 84.0, 0.084, 0.0001, day8_ago)])
    s.insert_oi([("DOGEUSDT", "DOGE", 1200.0, 100.8, 0.084, 0.0001, day1_ago)])
    assert s.count("bitget_oi") == 2

    # prune_before(now - 7天) 应删 8 天前那条，留 1 天前那条
    cutoff = now_ms - 7 * 86_400_000
    deleted = s.prune_before("bitget_oi", "ts", cutoff)
    assert deleted == 1, f"期望删 1 行，实际 {deleted}"
    assert s.count("bitget_oi") == 1

    # 验证剩余行是 1 天前那条（ts=day1_ago）
    row = s.conn.execute("SELECT ts FROM bitget_oi").fetchone()
    assert row is not None and row[0] == day1_ago
    s.close()


def test_prune_before_missing_table_returns_zero():
    """prune_before 对不存在的表返回 0，不抛异常。"""
    s = _store()
    result = s.prune_before("nonexistent_table_xyz", "ts", 999_999_999)
    assert result == 0
    s.close()


def test_prune_before_all_new_rows_untouched():
    """cutoff 早于所有行时，prune_before 不删任何行（返回 0）。"""
    s = _store()
    s.insert_oi([("BTCUSDT", "BTC", 500.0, 30_000.0, 60_000.0, 0.0001, 10_000_000)])
    s.insert_oi([("BTCUSDT", "BTC", 510.0, 30_600.0, 60_100.0, 0.0001, 20_000_000)])
    deleted = s.prune_before("bitget_oi", "ts", 5_000)  # cutoff 早于所有行
    assert deleted == 0
    assert s.count("bitget_oi") == 2
    s.close()


def test_retain_predictions_long_retention():
    """_DB_RETAIN 含 predictions 表且保留 ≥ 90 天——MTF ×7 增长后仍长期保留评估闭环历史。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from smc_tracker.app import TradingSystem
    retain_map = {entry[0]: entry[2] for entry in TradingSystem._DB_RETAIN}
    assert "predictions" in retain_map, (
        "predictions 必须在 _DB_RETAIN 中！MTF 后需 90 天长保留覆盖全水平线评估闭环。"
    )
    assert retain_map["predictions"] >= 90 * 86_400_000, (
        f"predictions 保留期应 ≥ 90 天，实际 {retain_map['predictions'] // 86_400_000} 天。"
    )


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
