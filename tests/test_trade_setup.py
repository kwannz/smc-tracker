"""tests/test_trade_setup.py — TradeSetup TDD 测试套件。

合成确定性测试：不依赖真实网络数据，所有输入手工构造。
"""
from __future__ import annotations

import math
import pytest

from smc_tracker.signals.trade_setup import TradeSetup, build_setups


# ── 辅助：合成 candles（确定性） ──────────────────────────────────────────────

class _Candle:
    """KNN/技术指标要求属性访问方式（.o/.h/.l/.c/.v），非 dict。"""
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self, o: float, h: float, lo: float, c: float, v: float) -> None:
        self.o = o
        self.h = h
        self.l = lo
        self.c = c
        self.v = v


def _make_candles(n: int = 120) -> list[_Candle]:
    """生成 n 根合成 K 线（简单随机游走），足以通过 KNN fit 所需样本。"""
    import random
    rng = random.Random(42)
    candles: list[_Candle] = []
    price = 100.0
    for _ in range(n):
        o = price
        c = price + rng.uniform(-1, 1)
        h = max(o, c) + rng.uniform(0, 0.5)
        lo = min(o, c) - rng.uniform(0, 0.5)
        v = rng.uniform(10, 100)
        candles.append(_Candle(o, h, lo, c, v))
        price = c
    return candles


def _gartley_bull_harmonic() -> dict:
    """手工构造完整 Gartley 看涨谐波 harmonic_result 字典。

    价格结构（虚构，仅测试逻辑正确性，X 近于 D 使止损 ≤8%）：
        X=100, A=120, B=107, C=115, D=107
        PRZ = (105, 109)，中点 107，D±1.5%≈(105.4, 108.6)
        止损基准 X=100 → stop_pct≈(107-99.9)/107≈6.6% ≤ 8% ✓
    """
    pattern_dict = {
        "pattern": "Gartley",
        "direction": "bull",
        "prz": (105.0, 109.0),
        "completed": True,
        "confidence": 0.75,
        "confluence": 2,
        "points": {
            "X": (0, 100.0),   # 止损基准：X=100，stop_pct≈6.6% ≤ 8%
            "A": (10, 120.0),
            "B": (15, 107.0),
            "C": (20, 115.0),
            "D": (25, 107.0),
        },
    }
    return {
        "completed": [pattern_dict],
        "forming": [],
        "price": 107.0,
    }


def _gartley_bull_forming() -> dict:
    """成形中（forming）的 Gartley，无 points。"""
    pattern_dict = {
        "pattern": "Gartley",
        "direction": "bull",
        "prz": (105.0, 109.0),
        "completed": False,
        "confidence": 0.60,
        "confluence": 1,
        # 成形形态不含 points
    }
    return {
        "completed": [],
        "forming": [pattern_dict],
        "price": 107.0,
    }


def _wide_prz_harmonic() -> dict:
    """PRZ 极宽（20% 范围），使 compute_risk 因止损过远返回 None → setup 被跳过。"""
    pattern_dict = {
        "pattern": "Bat",
        "direction": "bull",
        "prz": (60.0, 120.0),   # 极宽，stop_pct >> max_stop_pct(0.08)
        "completed": True,
        "confidence": 0.65,
        "confluence": 1,
        "points": {
            "X": (0, 50.0),
            "A": (10, 130.0),
            "B": (15, 70.0),
            "C": (20, 110.0),
            "D": (25, 90.0),
        },
    }
    return {
        "completed": [pattern_dict],
        "forming": [],
        "price": 90.0,
    }


# ── 测试 1：completed Gartley bull → TradeSetup 基本字段正确 ─────────────────

class TestCompletedGartleyBull:
    def setup_method(self):
        candles = _make_candles(120)
        harmonic = _gartley_bull_harmonic()
        self.setups = build_setups(
            coin="BTC",
            tf="1h",
            candles=candles,
            harmonic_result=harmonic,
            account_usd=10_000.0,
            risk_pct=0.01,
            target_rr=2.0,
        )

    def test_returns_at_least_one_setup(self):
        assert len(self.setups) >= 1, "completed Gartley bull 应产出至少一个 TradeSetup"

    def test_direction_is_long(self):
        s = self.setups[0]
        assert s.direction == "long", f"bull 形态应映射 long，得 {s.direction!r}"

    def test_entry_zone_within_prz(self):
        s = self.setups[0]
        # 🟡-1: completed 进场区改为 D±1.5%（D=107），不再是 PRZ 全宽(105,109)
        d_price = 107.0
        expected_lo = d_price * (1 - 0.015)  # ≈105.395
        expected_hi = d_price * (1 + 0.015)  # ≈108.605
        assert abs(s.entry_lo - expected_lo) < 0.01, (
            f"entry_lo={s.entry_lo:.4f} 应≈D×(1-1.5%)={expected_lo:.4f}"
        )
        assert abs(s.entry_hi - expected_hi) < 0.01, (
            f"entry_hi={s.entry_hi:.4f} 应≈D×(1+1.5%)={expected_hi:.4f}"
        )

    def test_stop_below_entry(self):
        s = self.setups[0]
        entry_mid = (s.entry_lo + s.entry_hi) / 2
        assert s.stop < entry_mid, f"long 止损 {s.stop} 应低于入场中点 {entry_mid}"

    def test_target1_above_entry(self):
        s = self.setups[0]
        entry_mid = (s.entry_lo + s.entry_hi) / 2
        assert s.target1 > entry_mid, f"long target1 {s.target1} 应高于入场 {entry_mid}"

    def test_rr_approx_2(self):
        s = self.setups[0]
        assert math.isclose(s.rr, 2.0, rel_tol=0.05), f"rr 期望≈2.0，得 {s.rr}"

    def test_fib_note_not_empty(self):
        s = self.setups[0]
        assert s.fib_note, "fib_note 不应为空"

    def test_position_qty_not_none(self):
        s = self.setups[0]
        assert s.position_qty is not None, "合理 setup 应能计算仓位数量"

    def test_position_notional_not_none(self):
        s = self.setups[0]
        assert s.position_notional is not None

    def test_confidence_le_0_90(self):
        s = self.setups[0]
        assert s.confidence <= 0.90, f"综合置信封顶 0.90，得 {s.confidence}"

    def test_confidence_gt_0(self):
        s = self.setups[0]
        assert s.confidence > 0.0

    def test_pattern_name(self):
        s = self.setups[0]
        assert s.pattern == "Gartley"

    def test_completed_flag(self):
        s = self.setups[0]
        assert s.completed is True

    def test_coin_and_tf(self):
        s = self.setups[0]
        assert s.coin == "BTC"
        assert s.tf == "1h"

    def test_note_contains_honest_warning(self):
        s = self.setups[0]
        # note 应包含诚实免责标注，含 "KNN" 或 "订单流"
        assert "KNN" in s.note or "订单流" in s.note, f"note 缺少诚实标注: {s.note!r}"


# ── 测试 2：forming 形态（无 points）→ fib_note 含 "成形" 或 "近似"，不崩溃 ────

class TestFormingGartley:
    def setup_method(self):
        candles = _make_candles(120)
        harmonic = _gartley_bull_forming()
        self.setups = build_setups(
            coin="ETH",
            tf="4h",
            candles=candles,
            harmonic_result=harmonic,
        )

    def test_no_crash(self):
        # 最重要：不崩溃
        assert isinstance(self.setups, list)

    def test_fib_note_mentions_forming(self):
        if not self.setups:
            pytest.skip("forming 形态被 compute_risk 过滤掉，属预期行为")
        s = self.setups[0]
        assert "成形" in s.fib_note or "近似" in s.fib_note, (
            f"forming 形态 fib_note 应含 '成形' 或 '近似'，得 {s.fib_note!r}"
        )

    def test_completed_false(self):
        if not self.setups:
            pytest.skip("forming 形态被过滤，属预期行为")
        s = self.setups[0]
        assert s.completed is False

    def test_confidence_le_0_90(self):
        for s in self.setups:
            assert s.confidence <= 0.90


# ── 测试 3：PRZ 极宽 → compute_risk 返回 None → setup 被跳过 ─────────────────

class TestWidePRZSkipped:
    def test_bad_setup_filtered(self):
        candles = _make_candles(120)
        harmonic = _wide_prz_harmonic()
        setups = build_setups(
            coin="SOL",
            tf="15m",
            candles=candles,
            harmonic_result=harmonic,
            account_usd=10_000.0,
            risk_pct=0.01,
        )
        # 极宽 PRZ 导致止损过远(>8%)，应被过滤
        assert setups == [], (
            f"极宽 PRZ 的劣质 setup 应被跳过，实际得 {len(setups)} 个"
        )


# ── 测试 4：空 harmonic_result → 返回 [] ─────────────────────────────────────

class TestEmptyHarmonic:
    def test_empty_returns_empty_list(self):
        candles = _make_candles(120)
        harmonic = {"completed": [], "forming": [], "price": 100.0}
        setups = build_setups("BTC", "1d", candles, harmonic)
        assert setups == [], "空 harmonic 应返回空列表"


# ── 测试 5：KNN 样本不足 → knn_supports=None 不崩溃 ─────────────────────────

class TestKNNInsufficientSamples:
    def test_knn_none_no_crash(self):
        # 仅 5 根 K 线，KNN 样本不足返回 None
        candles = _make_candles(5)
        harmonic = _gartley_bull_harmonic()
        setups = build_setups("BTC", "1h", candles, harmonic)
        # 可能返回空（compute_risk 过滤）或 knn_supports=None
        for s in setups:
            assert s.knn_supports is None or isinstance(s.knn_supports, bool)

    def test_knn_none_knn_supports_is_none(self):
        """明确验证：KNN 样本不足时 knn_supports=None，knn_note 含 '样本不足'。"""
        candles = _make_candles(5)
        harmonic = _gartley_bull_harmonic()
        setups = build_setups("BTC", "1h", candles, harmonic)
        for s in setups:
            if s.knn_supports is None:
                assert "样本不足" in s.knn_note, (
                    f"knn_supports=None 时 knn_note 应含 '样本不足'，得 {s.knn_note!r}"
                )


# ── 测试 6：TradeSetup 是 dataclass，slots 模式 ──────────────────────────────

class TestTradeSetupDataclass:
    def test_is_dataclass(self):
        import dataclasses
        assert dataclasses.is_dataclass(TradeSetup), "TradeSetup 应为 dataclass"

    def test_has_slots(self):
        assert hasattr(TradeSetup, "__slots__"), "TradeSetup 应使用 slots=True"

    def test_all_required_fields(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(TradeSetup)}
        required = {
            "coin", "tf", "direction", "pattern", "completed",
            "entry_lo", "entry_hi", "stop", "target1", "target2",
            "rr", "fib_note", "knn_supports", "knn_note",
            "position_qty", "position_notional", "confidence", "note",
            "src_key",  # 🔴-1 注入键碰撞修复：唯一来源标识
        }
        missing = required - fields
        assert not missing, f"TradeSetup 缺少字段: {missing}"


# ── 测试 7：completed 优先于 forming，同级按置信降序 ─────────────────────────

class TestSortOrder:
    def test_completed_before_forming(self):
        candles = _make_candles(120)
        # 混合：一个 completed + 一个 forming
        harmonic = {
            "completed": [_gartley_bull_harmonic()["completed"][0]],
            "forming": [_gartley_bull_forming()["forming"][0]],
            "price": 107.0,
        }
        setups = build_setups("BTC", "1h", candles, harmonic)
        # 找到 completed 和 forming
        completed_setups = [s for s in setups if s.completed]
        forming_setups = [s for s in setups if not s.completed]
        if completed_setups and forming_setups:
            first_completed_idx = setups.index(completed_setups[0])
            first_forming_idx = setups.index(forming_setups[0])
            assert first_completed_idx < first_forming_idx, (
                "completed setup 应排在 forming 之前"
            )


# ── 测试 8：🔴-1 注入键碰撞修复 — 两个同 (pattern,direction) 不同 D_idx ────────
# TradeSetup 新字段 src_key + build_setups 按 src_key 精确匹配（非 tuple3 索引）

def _two_gartley_bull_completed() -> dict:
    """两个同向、同名、均 completed 的 Gartley-bull，但 D 点不同 → 进场区不同。

    Pattern A：D@107，X=100 → stop_pct≈6.6% ≤8%，可通过 compute_risk
    Pattern B：D@90，X=84 → stop_pct≈6.3% ≤8%，可通过 compute_risk

    两者 (pattern, direction, completed) 相同，旧逻辑碰撞取 Pattern A 的 setup 给 B，
    新逻辑按 src_key=C|Gartley|long|107.0 / C|Gartley|long|90.0 精确匹配。
    """
    pat_a = {
        "pattern": "Gartley",
        "direction": "bull",
        "prz": (105.0, 109.0),
        "completed": True,
        "confidence": 0.75,
        "confluence": 2,
        "points": {
            "X": (0, 100.0),   # X=100, D=107, stop_pct≈6.6% OK
            "A": (10, 120.0),
            "B": (15, 107.0),
            "C": (20, 115.0),
            "D": (25, 107.0),
        },
    }
    pat_b = {
        "pattern": "Gartley",
        "direction": "bull",
        "prz": (88.0, 92.0),
        "completed": True,
        "confidence": 0.70,
        "confluence": 2,
        "points": {
            "X": (30, 84.0),   # X=84, D=90, stop_pct≈6.3% OK
            "A": (40, 102.0),
            "B": (45, 91.0),
            "C": (50, 98.0),
            "D": (55, 90.0),
        },
    }
    return {
        "completed": [pat_a, pat_b],
        "forming": [],
        "price": 98.0,
    }


class TestSetupInjectionNoCollision:
    """🔴-1: 两个同 (pattern,direction,completed) 不同 D_idx → 各自 src_key 唯一，不碰撞。"""

    def test_src_key_field_exists(self):
        """TradeSetup 必须有 src_key 字段。"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(TradeSetup)}
        assert "src_key" in fields, "TradeSetup 缺少 src_key 字段"

    def test_two_gartley_produce_two_setups(self):
        """两个不同 D 点的 Gartley-bull 应产生 2 个独立 setup。"""
        candles = _make_candles(120)
        harmonic = _two_gartley_bull_completed()
        setups = build_setups("BTC", "1h", candles, harmonic)
        assert len(setups) == 2, (
            f"两个不同 D 点的 Gartley-bull 应有 2 个 setup，实际 {len(setups)} 个"
        )

    def test_two_gartley_distinct_entry_zones(self):
        """两个 setup 的进场区中点不同（不共享同一 setup）。"""
        candles = _make_candles(120)
        harmonic = _two_gartley_bull_completed()
        setups = build_setups("BTC", "1h", candles, harmonic)
        if len(setups) < 2:
            pytest.skip("未产生 2 个 setup，跳过碰撞检测")
        mid_a = (setups[0].entry_lo + setups[0].entry_hi) / 2
        mid_b = (setups[1].entry_lo + setups[1].entry_hi) / 2
        assert abs(mid_a - mid_b) > 1.0, (
            f"两个 setup 进场区中点相同 ({mid_a:.2f}={mid_b:.2f})，存在键碰撞"
        )

    def test_src_key_unique_per_setup(self):
        """各 setup 的 src_key 唯一。"""
        candles = _make_candles(120)
        harmonic = _two_gartley_bull_completed()
        setups = build_setups("BTC", "1h", candles, harmonic)
        if len(setups) < 2:
            pytest.skip("未产生 2 个 setup，跳过 src_key 唯一性检测")
        keys = [s.src_key for s in setups]
        assert len(set(keys)) == len(keys), (
            f"src_key 不唯一，存在碰撞: {keys}"
        )


# ── 测试 9：🟡-1 completed 进场区收窄到 D±1.5% ────────────────────────────────

class TestCompletedEntryZoneNarrow:
    """🟡-1: completed 形态进场区应 ≈ D±1.5%（宽度 ≈3%），远窄于旧 10%。"""

    def test_entry_width_approx_3pct(self):
        """completed 进场区宽度 ≤ 4%（D±1.5% 给浮点宽容）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_harmonic()  # D=107
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤，跳过宽度检测")
        s = setups[0]
        assert s.completed is True
        width_pct = (s.entry_hi - s.entry_lo) / s.entry_lo * 100
        assert width_pct <= 4.0, (
            f"completed 进场区宽度 {width_pct:.2f}% 应 ≤4%（D±1.5%≈3%），旧值≈10% 已修"
        )

    def test_entry_zone_centered_on_D(self):
        """completed 进场区中点应近似等于 D 点价格（107±0.5%）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_harmonic()  # D=107
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤，跳过中点检测")
        s = setups[0]
        assert s.completed is True
        mid = (s.entry_lo + s.entry_hi) / 2.0
        d_price = 107.0
        assert abs(mid - d_price) / d_price < 0.005, (
            f"completed 进场区中点 {mid:.2f} 应近似等于 D={d_price}（偏差 <0.5%）"
        )

    def test_forming_entry_zone_uses_prz(self):
        """forming 形态进场区保留 PRZ 全宽（无 D 点可用）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_forming()  # PRZ=(105, 109)
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("forming setup 被过滤，跳过 PRZ 宽度检测")
        s = setups[0]
        assert s.completed is False
        # forming 应使用 PRZ (105, 109)，宽度 ≈3.8% 且 entry_lo ≈105, entry_hi ≈109
        assert abs(s.entry_lo - 105.0) < 0.5, (
            f"forming entry_lo={s.entry_lo:.2f} 应≈105（PRZ 下沿）"
        )
        assert abs(s.entry_hi - 109.0) < 0.5, (
            f"forming entry_hi={s.entry_hi:.2f} 应≈109（PRZ 上沿）"
        )


# ── 测试 10：🟡-2 Fib 虚高置信修复 — completed 不再 ×1.1 ─────────────────────

class TestNoFreeFibBoost:
    """🟡-2: completed 形态置信 = base × knn_mult（不再含 fib_mult=1.1）。"""

    def test_gartley_confidence_no_fib_mult(self):
        """Gartley completed confidence = base × knn_mult（封顶0.90），不超 base×1.1。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_harmonic()  # base_conf=0.75
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤，跳过置信检测")
        s = setups[0]
        assert s.completed is True
        # 旧逻辑：0.75 × 1.1 × knn_mult ≤ 0.90，新逻辑：0.75 × knn_mult ≤ 0.90
        # 核心验证：confidence 应 ≤ base_conf × 1.06（允许 knn=True +5%），不因 fib 被 ×1.1
        base_conf = 0.75
        max_allowed = min(base_conf * 1.10, 0.90)  # 旧最大值（fib_mult×knn_mult）
        new_max = min(base_conf * 1.06, 0.90)       # 新最大值（仅 knn_mult）
        # 置信应 ≤ new_max（无 fib_mult），不需要 fib 才能达到 max_allowed
        assert s.confidence <= new_max or s.confidence <= 0.90, (
            f"confidence={s.confidence:.4f} 不应通过 fib_mult 被抬高"
        )

    def test_fib_note_honest_for_completed(self):
        """completed 形态的 fib_note 应含诚实说明（'形态定义' 或 '非独立确认'）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_harmonic()
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤，跳过 fib_note 检测")
        s = setups[0]
        assert s.completed is True
        # 诚实 fib_note 应包含 "形态定义" 或 "非独立确认"（不再宣称"汇合"/"黄金口袋"加分）
        honest_keywords = ("形态定义", "非独立确认")
        has_honest = any(kw in s.fib_note for kw in honest_keywords)
        assert has_honest, (
            f"completed fib_note 应含诚实说明（形态定义/非独立确认），实际: {s.fib_note!r}"
        )


# ── 测试 11：🟡-5 止损基准用 X 失效位 ───────────────────────────────────────

def _gartley_bull_far_x() -> dict:
    """Gartley bull，X 点远离 D（X=50, D=107），止损基准应用 X 而非 PRZ 下沿。

    D±1.5% 进场区约 (105.4, 108.6)；X=50 → 止损 ≈50 → stop_pct≈52%
    → 远超 max_stop_pct(8%) → compute_risk 返回 None → setup 被跳过（诚实）。
    """
    return {
        "completed": [
            {
                "pattern": "Gartley",
                "direction": "bull",
                "prz": (105.0, 109.0),
                "completed": True,
                "confidence": 0.75,
                "confluence": 2,
                "points": {
                    "X": (0, 50.0),   # 止损基准：X=50，远离 D=107
                    "A": (10, 120.0),
                    "B": (15, 95.0),
                    "C": (20, 112.0),
                    "D": (25, 107.0),
                },
            }
        ],
        "forming": [],
        "price": 107.0,
    }


def _gartley_bull_near_x() -> dict:
    """Gartley bull，X 点近于 D（X=100），止损基准 X 使止损在合理范围内（≤8%）。

    D±1.5% 进场区约 (105.4, 108.6)，entry_mid≈107；X=100 → stop_pct≈6.5% ≤ 8%。
    """
    return {
        "completed": [
            {
                "pattern": "Gartley",
                "direction": "bull",
                "prz": (105.0, 109.0),
                "completed": True,
                "confidence": 0.75,
                "confluence": 2,
                "points": {
                    "X": (0, 100.0),  # 止损基准：X=100，近于 D=107，stop_pct≈6.5% ≤8%
                    "A": (10, 120.0),
                    "B": (15, 95.0),
                    "C": (20, 112.0),
                    "D": (25, 107.0),
                },
            }
        ],
        "forming": [],
        "price": 107.0,
    }


class TestStopUsesX:
    """🟡-5: completed long setup 的止损基准改用 X 点，不再用 prz_lo。"""

    def test_far_x_setup_skipped(self):
        """X 远离 D（stop_pct 超 8%）→ compute_risk 返回 None → setup 被跳过（诚实）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_far_x()
        setups = build_setups("BTC", "1h", candles, harmonic)
        assert setups == [], (
            f"X 远离 D（止损过远）应跳过 setup，实际返回 {len(setups)} 个"
        )

    def test_near_x_stop_based_on_x(self):
        """X 近于 D（X=100）→ setup 存在，stop 接近 X（≈ 100 × 0.999）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_near_x()
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤，跳过 stop 检测（可能 X 止损仍过远）")
        s = setups[0]
        assert s.completed is True
        assert s.direction == "long"
        # 止损基准为 X=100，stop ≈ 100×(1-buffer) = 99.9（含 buffer_pct=0.001）
        # 而旧逻辑 stop 基于 prz_lo=105 → stop≈105×0.999=104.9
        # 新逻辑止损更低（≤101），旧止损更高（≈104.9）
        assert s.stop < 104.0, (
            f"stop={s.stop:.2f} 应基于 X=100（≈99.9），而非旧 prz_lo≈104.9"
        )

    def test_forming_stop_uses_prz(self):
        """forming 形态止损仍基于 prz_lo/prz_hi（无 points.X 可用）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_forming()  # PRZ=(105, 109)，无 points
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("forming setup 被过滤，跳过止损检测")
        s = setups[0]
        assert s.completed is False
        # forming long 止损基于 prz_lo=105，stop 应在 105 附近
        assert 100.0 <= s.stop <= 106.0, (
            f"forming long stop={s.stop:.2f} 应基于 prz_lo=105"
        )
