"""谐波 review 闭环：completed setup → 预测记录构建器单测（纯函数）。

QA 修复落地：
- 只记 **completed**（kind="谐波-反应式"，诚实标注反转后延续）；forming **不在投影时记**（H1：
  投影时价离 PRZ 任意远，方向被符号反转，测的是随机漂移）——forming 留给后续"逼近 PRZ"事件。
- 用**结构指纹**(coin,tf,pattern,direction,D_idx) + SetupDedup 去重（H3：避免每 15min 重记自相关）。
- 携带 bg_px（来自 row["price"]，Bitget 价）修价格覆盖（H_price：不走 meme-only coin_to_symbol）。
"""
from __future__ import annotations

from smc_tracker.signals.harmonic_dedup import SetupDedup
from smc_tracker.signals.harmonic_review import build_harmonic_predictions


def _row(coin="BTC", tf="4H", price=60000.0, completed=None, forming=None):
    return {
        "coin": coin, "symbol": coin + "USDT", "tf": tf, "price": price,
        "completed": completed or [], "forming": forming or [],
    }


def _hit(pattern="Gartley", direction="bull", d_idx=42):
    return {"pattern": pattern, "direction": direction,
            "points": {"D": (d_idx, 61000.0)}, "confidence": 0.8}


def test_completed_setup_recorded_as_reactive():
    """completed → 一条记录，kind=谐波-反应式，bull→long，bg_px=row price。"""
    rows = [_row(completed=[_hit(direction="bull")])]
    recs = build_harmonic_predictions(rows, SetupDedup(), now_ms=1000)
    assert len(recs) == 1
    assert recs[0]["coin"] == "BTC"
    assert recs[0]["kind"] == "谐波-反应式"
    assert recs[0]["direction"] == "long"
    assert recs[0]["bg_px"] == 60000.0


def test_bear_maps_short():
    """bear → short。"""
    rows = [_row(completed=[_hit(direction="bear")])]
    recs = build_harmonic_predictions(rows, SetupDedup(), now_ms=1000)
    assert recs[0]["direction"] == "short"


def test_forming_not_recorded():
    """forming **不**在投影时记录（QA H1）。"""
    rows = [_row(completed=[], forming=[_hit(direction="bull")])]
    recs = build_harmonic_predictions(rows, SetupDedup(), now_ms=1000)
    assert recs == []


def test_dedup_blocks_repeat_within_ttl():
    """同一 completed setup（结构指纹相同）TTL 内只记一次。"""
    dedup = SetupDedup(ttl_ms=3_600_000)
    rows = [_row(completed=[_hit(d_idx=42)])]
    first = build_harmonic_predictions(rows, dedup, now_ms=1000)
    second = build_harmonic_predictions(rows, dedup, now_ms=1000 + 60_000)
    assert len(first) == 1
    assert second == []


def test_distinct_d_idx_recorded_separately():
    """不同 D 枢轴下标 = 不同 setup → 各记一次。"""
    rows = [_row(completed=[_hit(d_idx=42), _hit(d_idx=99)])]
    recs = build_harmonic_predictions(rows, SetupDedup(), now_ms=1000)
    assert len(recs) == 2


def test_invalid_direction_skipped():
    """非法方向（既非 bull 非 bear）跳过，不崩。"""
    rows = [_row(completed=[_hit(direction="")])]
    recs = build_harmonic_predictions(rows, SetupDedup(), now_ms=1000)
    assert recs == []
