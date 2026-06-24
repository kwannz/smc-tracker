"""forming PRZ 实时逼近检测器单测（纯内存判定，确定性）。

QA H6/H7 修复：
- 纯内存判定（price 是否进入缓存 forming PRZ 带），**不写库**——热路径只调 check，
  事件由调用方入队、周期 worker 落库（21 条同步 SQL 移出 WS 回调）。
- per-entry TTL（陈旧 PRZ 不触发，修 H7 陈旧假告警）。
- 冷却（同一 PRZ 不刷屏）。
- 穿越作废（价格越过 PRZ 远侧=形态失效，不再告警）。
"""
from __future__ import annotations

from smc_tracker.monitor.forming_approach import FormingApproachTracker


def _row(coin="BTC", tf="4H", prz=(60000.0, 61000.0), direction="bull", d_idx=42):
    return {
        "coin": coin, "tf": tf,
        "forming": [{"pattern": "Gartley", "direction": direction,
                     "prz": prz, "points": {"D": (d_idx, 60500.0)}}],
        "completed": [],
    }


def test_price_inside_prz_triggers_approach():
    """价格进入 forming PRZ 带 → 逼近事件。"""
    t = FormingApproachTracker()
    t.update([_row()], now_ms=1000)
    evs = t.check("BTC", 60500.0, now_ms=2000)
    assert len(evs) == 1
    assert evs[0]["coin"] == "BTC"
    assert evs[0]["direction"] == "long"   # bull→long
    assert evs[0]["prz_lo"] == 60000.0


def test_price_outside_prz_no_trigger():
    """价格在 PRZ 带外 → 无事件。"""
    t = FormingApproachTracker()
    t.update([_row()], now_ms=1000)
    assert t.check("BTC", 62000.0, now_ms=2000) == []


def test_cooldown_blocks_repeat():
    """同一 PRZ 冷却内不重复告警（防刷屏）。"""
    t = FormingApproachTracker(cooldown_ms=600_000)
    t.update([_row()], now_ms=1000)
    first = t.check("BTC", 60500.0, now_ms=2000)
    second = t.check("BTC", 60500.0, now_ms=2000 + 60_000)
    assert len(first) == 1
    assert second == []


def test_ttl_expired_not_triggered():
    """陈旧 PRZ（超 TTL）不触发（修 H7 陈旧假告警）。"""
    t = FormingApproachTracker(ttl_ms=1_800_000)
    t.update([_row()], now_ms=1000)
    # 距 update 已超 TTL
    assert t.check("BTC", 60500.0, now_ms=1000 + 1_900_000) == []


def test_crossed_through_invalidates():
    """bull forming 价格跌破 PRZ 远侧（lo 下方较多）=形态失效 → 不告警。"""
    t = FormingApproachTracker(invalidate_pct=0.01)
    t.update([_row(prz=(60000.0, 61000.0), direction="bull")], now_ms=1000)
    # 价格远低于 lo（60000*0.98=58800 < 60000*0.99）→ 穿越作废
    assert t.check("BTC", 58000.0, now_ms=2000) == []


def test_distinct_coin_isolated():
    """不同币互不触发。"""
    t = FormingApproachTracker()
    t.update([_row(coin="BTC")], now_ms=1000)
    assert t.check("ETH", 60500.0, now_ms=2000) == []


def test_update_overwrites_stale_cache():
    """新一轮 update 覆盖旧 PRZ（同币）。"""
    t = FormingApproachTracker()
    t.update([_row(prz=(60000.0, 61000.0))], now_ms=1000)
    t.update([_row(prz=(50000.0, 51000.0))], now_ms=2000)   # 覆盖
    assert t.check("BTC", 60500.0, now_ms=3000) == []        # 旧带已不在
    assert len(t.check("BTC", 50500.0, now_ms=3000)) == 1    # 新带命中


def test_check_no_sync_db_write():
    """QA H6 热路径安全：check() 是纯内存判定，绝不调用任何 store/DB 写方法。

    用 mock store 注入 FormingApproachTracker，check 时断言 store 无任何方法被调用——
    保证 WS 热回调里调 check 不阻塞 event loop（同步写库是 H6 的根因）。
    """
    from unittest.mock import MagicMock
    store = MagicMock()   # 任何方法调用都会被记录

    t = FormingApproachTracker()
    t.update([_row()], now_ms=1000)
    evs = t.check("BTC", 60500.0, now_ms=2000)

    # check() 触发了逼近事件
    assert len(evs) == 1

    # 热路径：check 内部不调用任何 store 方法（也没有 store 属性——此断言确认零 DB 写）
    assert not store.called, "check() 不得调用 store（热路径同步写库违纪）"
    store.assert_not_called()


def test_band_pct_extends_trigger_zone():
    """band_pct 容差：价格在 PRZ 带外但在 ±band_pct 扩展区内 → 逼近事件（前瞻量）。"""
    t = FormingApproachTracker(band_pct=0.01)  # ±1%
    t.update([_row(prz=(60000.0, 61000.0))], now_ms=1000)
    # 价格比 hi(61000) 高 0.5%（在 band_pct=1% 扩展区内）
    evs = t.check("BTC", 61305.0, now_ms=2000)
    assert len(evs) == 1


def test_bear_invalidation_above_hi():
    """bear forming：价格穿越 PRZ 远侧（hi 上方）= 形态失效 → 不告警。"""
    t = FormingApproachTracker(invalidate_pct=0.01)
    t.update([_row(prz=(60000.0, 61000.0), direction="bear")], now_ms=1000)
    # 价格远高于 hi（61000×1.02=62220 < 63000）→ 穿越失效
    assert t.check("BTC", 63000.0, now_ms=2000) == []
