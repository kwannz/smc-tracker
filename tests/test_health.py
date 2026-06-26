"""系统健康检查单测：数据新鲜度判定 + 验证闭环积压（合成数据，无网络，确定性）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.health import fmt_health, system_health
from smc_tracker.review import PredictionReview
from smc_tracker.storage import Store

_HOUR = 3_600_000


def _store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "h.db")


def test_fresh_data_is_healthy():
    """有新鲜行情 + 无到期未评预测 → ok=True。"""
    s = _store()
    now = 1_000_000_000_000
    # bitget_oi 刚写入（新鲜）
    s.conn.execute(
        "INSERT INTO bitget_oi(symbol,coin,oi_size,oi_usd,mark_px,funding,ts)"
        " VALUES('BTCUSDT','BTC',1.0,1.0,60000.0,0.0,?)", (now - 60_000,))
    rep = system_health(s, now, stale_after_s=7200.0)
    assert rep["ok"] is True
    bo = next(f for f in rep["freshness"] if f["table"] == "bitget_oi")
    assert bo["stale"] is False and bo["n"] == 1
    s.close()


def test_stale_data_flags_unhealthy():
    """所有核心表都超过 stale 阈值 → ok=False（自动捕获采集停滞）。"""
    s = _store()
    now = 1_000_000_000_000
    s.conn.execute(
        "INSERT INTO bitget_oi(symbol,coin,oi_size,oi_usd,mark_px,funding,ts)"
        " VALUES('BTCUSDT','BTC',1.0,1.0,60000.0,0.0,?)", (now - 9 * _HOUR,))
    rep = system_health(s, now, stale_after_s=7200.0)
    assert rep["ok"] is False
    bo = next(f for f in rep["freshness"] if f["table"] == "bitget_oi")
    assert bo["stale"] is True and bo["age_s"] > 7200
    assert "告警" in fmt_health(rep)
    s.close()


def test_future_timestamp_clamped_to_nonneg_age_and_flagged():
    """未来 ts(时钟偏移/服务端同步数据)→ age_s 夹非负(不显示无意义负值)+ future_skew=True 诚实标注。"""
    s = _store()
    now = 1_000_000_000_000
    # 写一行 ts 在 now 之后 1 小时(模拟时钟偏移)
    s.conn.execute(
        "INSERT INTO bitget_oi(symbol,coin,oi_size,oi_usd,mark_px,funding,ts)"
        " VALUES('BTCUSDT','BTC',1.0,1.0,60000.0,0.0,?)", (now + 1 * _HOUR,))
    rep = system_health(s, now, stale_after_s=7200.0)
    bo = next(f for f in rep["freshness"] if f["table"] == "bitget_oi")
    assert bo["age_s"] >= 0.0, "未来 ts 不应产生负 age_s"
    assert bo["future_skew"] is True, "未来 ts 应标注 future_skew(诚实暴露时钟偏移)"
    assert bo["stale"] is False  # 数据本身是最新的(只是 ts 偏移)
    s.close()


def test_overdue_predictions_flag_unhealthy():
    """有新鲜数据但存在到期未评估预测 → ok=False（评估管线停滞信号）。"""
    s = _store()
    now = 1_000_000_000_000
    s.conn.execute(
        "INSERT INTO bitget_oi(symbol,coin,oi_size,oi_usd,mark_px,funding,ts)"
        " VALUES('BTCUSDT','BTC',1.0,1.0,60000.0,0.0,?)", (now - 60_000,))
    review = PredictionReview(s)
    # 2 小时前发出、1 小时水平线 → 现在已到期但未评估
    review.record(ts=now - 2 * _HOUR, coin="BTC", kind="共识", direction="long",
                  hl_px=60000.0, bg_px=0.0, horizon_ms=_HOUR)
    rep = system_health(s, now, stale_after_s=7200.0)
    assert rep["predictions"]["overdue"] == 1
    assert rep["ok"] is False
    assert "到期未评" in fmt_health(rep)
    s.close()


def test_pending_not_overdue_is_healthy():
    """预测刚记录、未到期（pending 但非 overdue）+ 新鲜数据 → ok=True。"""
    s = _store()
    now = 1_000_000_000_000
    s.conn.execute(
        "INSERT INTO bitget_oi(symbol,coin,oi_size,oi_usd,mark_px,funding,ts)"
        " VALUES('BTCUSDT','BTC',1.0,1.0,60000.0,0.0,?)", (now - 60_000,))
    review = PredictionReview(s)
    review.record(ts=now, coin="BTC", kind="共识", direction="long",
                  hl_px=60000.0, bg_px=0.0, horizon_ms=_HOUR)
    rep = system_health(s, now, stale_after_s=7200.0)
    assert rep["predictions"]["pending"] == 1
    assert rep["predictions"]["overdue"] == 0
    assert rep["ok"] is True
    s.close()


def test_leaderboard_cache_status_in_report():
    """report 含 leaderboard_cache 段（抓庄发现源新鲜度），且不门控总体 ok。"""
    s = _store()
    now = 1_000_000_000_000
    s.conn.execute(
        "INSERT INTO bitget_oi(symbol,coin,oi_size,oi_usd,mark_px,funding,ts)"
        " VALUES('BTCUSDT','BTC',1.0,1.0,60000.0,0.0,?)", (now - 60_000,))
    rep = system_health(s, now)
    assert "leaderboard_cache" in rep
    lb = rep["leaderboard_cache"]
    assert set(lb) >= {"exists", "age_s", "stale"}
    # 渲染含「抓庄发现源」段，不抛
    assert "抓庄发现源" in fmt_health(rep)
    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
