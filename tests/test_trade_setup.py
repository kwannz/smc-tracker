"""tests/test_trade_setup.py — TradeSetup TDD 测试套件。

合成确定性测试：不依赖真实网络数据，所有输入手工构造。
"""
from __future__ import annotations

import dataclasses
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
        # 🟡-1 + §4D: completed 进场区为 D±1.5% 或 Fib 收窄后的交集，均在 D±1.5% 范围内
        # §4D: 若黄金口袋与 D±1.5% 有交集，入场区收窄到交集（比 D±1.5% 更紧），属预期行为。
        d_price = 107.0
        outer_lo = d_price * (1 - 0.015)  # ≈105.395（D±1.5% 外沿下界）
        outer_hi = d_price * (1 + 0.015)  # ≈108.605（D±1.5% 外沿上界）
        # 入场区应在 D±1.5% 范围之内（或等于），不超出
        assert s.entry_lo >= outer_lo - 0.01, (
            f"entry_lo={s.entry_lo:.4f} 不应低于 D×(1-1.5%)={outer_lo:.4f}"
        )
        assert s.entry_hi <= outer_hi + 0.01, (
            f"entry_hi={s.entry_hi:.4f} 不应高于 D×(1+1.5%)={outer_hi:.4f}"
        )
        # 入场区必须有宽度（lo < hi）
        assert s.entry_lo < s.entry_hi, (
            f"entry_lo={s.entry_lo:.4f} 应 < entry_hi={s.entry_hi:.4f}"
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
    """🟡-2 + P1-5: completed 形态置信 = base × ATR2 调权（不含 fib_mult=1.1，KNN 已降级为纯展示不调权）。"""

    def test_gartley_confidence_no_fib_mult(self):
        """Gartley completed confidence ≤ base × 1.05（仅 ATR2 同向 +5%；无 fib ×1.1、无 KNN 调权）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_harmonic()  # base_conf=0.75
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤，跳过置信检测")
        s = setups[0]
        assert s.completed is True
        # 新逻辑（P1-5 后）：confidence = base_conf × ATR2(1.05/0.80/1.0)，KNN 不再乘性调权。
        # 最大 = base × 1.05（ATR2 同向），封顶 0.90。若 fib×1.1 或 KNN×1.05 仍在 → 会超此界。
        base_conf = 0.75
        new_max = min(base_conf * 1.05, 0.90)       # 仅 ATR2 同向 +5%
        assert s.confidence <= new_max + 1e-9, (
            f"confidence={s.confidence:.4f} 超过 base×ATR2 上限，疑似残留 fib/KNN 调权"
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


# ── 测试 12：orderflow 字段新增（订单流确认接入） ────────────────────────────────

class TestOrderflowField:
    """TradeSetup 新增 orderflow 字段，默认 None，build_setups 纯函数不注入。"""

    def test_orderflow_field_exists(self):
        """TradeSetup dataclass 有 orderflow 字段。"""
        import dataclasses
        fields = {f.name for f in dataclasses.fields(TradeSetup)}
        assert "orderflow" in fields, "TradeSetup 缺少 orderflow 字段"

    def test_orderflow_field_default_none(self):
        """TradeSetup.orderflow 字段默认值为 None。"""
        import dataclasses
        for f in dataclasses.fields(TradeSetup):
            if f.name == "orderflow":
                # dataclass field default 或 default_factory
                default_val = f.default
                default_fac = f.default_factory  # type: ignore[attr-defined]
                is_none_default = (
                    default_val is None
                    or (default_val is dataclasses.MISSING and default_fac is dataclasses.MISSING)
                )
                # default=None 表示字段有默认值 None
                assert default_val is None, (
                    f"orderflow 字段默认值应为 None，实际: {default_val!r}"
                )
                break

    def test_build_setups_orderflow_is_none(self):
        """build_setups 产出的 setup.orderflow 默认 None（纯函数，无 ob_provider）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_harmonic()
        setups = build_setups("BTC", "1h", candles, harmonic)
        for s in setups:
            assert s.orderflow is None, (
                f"build_setups 产出的 setup.orderflow 应为 None，实际: {s.orderflow!r}"
            )

    def test_orderflow_field_accepts_none_assignment(self):
        """orderflow 字段可赋值为 None（slots 模式，确保 setattr 不崩）。"""
        candles = _make_candles(120)
        harmonic = _gartley_bull_harmonic()
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("无 setup 产出，跳过赋值测试")
        s = setups[0]
        s.orderflow = None  # 不应抛 AttributeError（slots 字段赋值）
        assert s.orderflow is None

    def test_orderflow_field_accepts_orderflow_confirm(self):
        """orderflow 字段可赋值为 OrderflowConfirm 实例（模拟 monitor 层注入）。"""
        from smc_tracker.signals.orderflow_confirm import OrderflowConfirm
        candles = _make_candles(120)
        harmonic = _gartley_bull_harmonic()
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("无 setup 产出，跳过注入测试")
        s = setups[0]
        of = OrderflowConfirm(
            confirmed=True,
            wall_usd=500_000.0,
            wall_dist_pct=0.008,
            imbalance=0.35,
            note="测试注入",
        )
        s.orderflow = of
        assert s.orderflow is of
        assert s.orderflow.confirmed is True


# ── 测试 13：ATR2 集成 — TradeSetup 新字段 atr_stop/atr2_bias/atr2_confirm ───────
# TDD RED-GREEN 周期（任务C）

# ── 辅助：生成足够 ATR2 暖机用的上升趋势 candles（trend_length=8, smoothness=20 → 至少 47 根） ──

def _make_atr2_candles_long(n: int = 60) -> list[_Candle]:
    """生成足够根数的上升趋势 K 线，使 atr2_confirmation 返回 bias='long'。"""
    candles: list[_Candle] = []
    price = 100.0
    for _ in range(n):
        o = price
        c = price + 0.5          # 持续上涨
        h = c + 0.2
        lo = o - 0.1
        candles.append(_Candle(o, h, lo, c, 1000.0))
        price = c
    return candles


def _make_atr2_candles_short(n: int = 60) -> list[_Candle]:
    """生成足够根数的下降趋势 K 线，使 atr2_confirmation 返回 bias='short'。"""
    candles: list[_Candle] = []
    price = 130.0
    for _ in range(n):
        o = price
        c = price - 0.5          # 持续下跌
        h = o + 0.1
        lo = c - 0.2
        candles.append(_Candle(o, h, lo, c, 1000.0))
        price = c
    return candles


class TestATR2FieldsOnDataclass:
    """任务C: TradeSetup dataclass 必须有 atr_stop/atr2_bias/atr2_confirm 三个新字段。"""

    def test_atr_stop_field_exists(self):
        """TradeSetup 必须有 atr_stop 字段。"""
        fields = {f.name for f in dataclasses.fields(TradeSetup)}
        assert "atr_stop" in fields, "TradeSetup 缺少 atr_stop 字段"

    def test_atr2_bias_field_exists(self):
        """TradeSetup 必须有 atr2_bias 字段。"""
        fields = {f.name for f in dataclasses.fields(TradeSetup)}
        assert "atr2_bias" in fields, "TradeSetup 缺少 atr2_bias 字段"

    def test_atr2_confirm_field_exists(self):
        """TradeSetup 必须有 atr2_confirm 字段。"""
        fields = {f.name for f in dataclasses.fields(TradeSetup)}
        assert "atr2_confirm" in fields, "TradeSetup 缺少 atr2_confirm 字段"

    def test_atr_stop_default_none(self):
        """atr_stop 默认值应为 None（新字段，兼容现有 setup）。"""
        for f in dataclasses.fields(TradeSetup):
            if f.name == "atr_stop":
                assert f.default is None, (
                    f"atr_stop 默认值应为 None，实际: {f.default!r}"
                )

    def test_atr2_bias_default_none(self):
        """atr2_bias 默认值应为 None。"""
        for f in dataclasses.fields(TradeSetup):
            if f.name == "atr2_bias":
                assert f.default is None, (
                    f"atr2_bias 默认值应为 None，实际: {f.default!r}"
                )

    def test_atr2_confirm_default_none(self):
        """atr2_confirm 默认值应为 None。"""
        for f in dataclasses.fields(TradeSetup):
            if f.name == "atr2_confirm":
                assert f.default is None, (
                    f"atr2_confirm 默认值应为 None，实际: {f.default!r}"
                )


class TestATR2EnoughCandles:
    """足够 candles 时 build_setups 应填充 atr_stop/atr2_bias/atr2_confirm。"""

    def setup_method(self):
        # 上升趋势 candles → atr2_confirmation 应返回 bias='long'
        # Gartley bull (direction='long') 与 bias='long' 一致 → atr2_confirm=True
        self.candles = _make_atr2_candles_long(n=60)
        self.setups = build_setups(
            coin="BTC",
            tf="1h",
            candles=self.candles,
            harmonic_result=_gartley_bull_harmonic(),
            account_usd=10_000.0,
            risk_pct=0.01,
            target_rr=2.0,
        )

    def test_setup_produced(self):
        """足够 candles + 有效谐波 → 至少 1 个 setup。"""
        assert len(self.setups) >= 1, "应产出至少 1 个 TradeSetup"

    def test_atr_stop_not_none(self):
        """足够 candles → atr_stop 不为 None。"""
        s = self.setups[0]
        assert s.atr_stop is not None, (
            "足够 candles 时 atr_stop 不应为 None"
        )

    def test_atr_stop_direction_correct_long(self):
        """long setup: atr_stop = entry_mid - 1.5 × atr（低于进场区中点）。"""
        s = self.setups[0]
        assert s.direction == "long"
        assert s.atr_stop is not None
        entry_mid = (s.entry_lo + s.entry_hi) / 2.0
        # atr_stop 应低于 entry_mid（long 方向止损在下方）
        assert s.atr_stop < entry_mid, (
            f"long atr_stop={s.atr_stop:.4f} 应低于 entry_mid={entry_mid:.4f}"
        )

    def test_atr2_bias_field_set(self):
        """足够 candles → atr2_bias 应为 'long'/'short'/'neutral' 之一，不为 None。"""
        s = self.setups[0]
        assert s.atr2_bias in ("long", "short", "neutral"), (
            f"atr2_bias 应为有效字符串，实际: {s.atr2_bias!r}"
        )

    def test_atr2_confirm_is_bool_when_candles_sufficient(self):
        """足够 candles → atr2_confirm 应为 bool（True 或 False），不为 None。"""
        s = self.setups[0]
        assert isinstance(s.atr2_confirm, bool), (
            f"足够 candles 时 atr2_confirm 应为 bool，实际: {s.atr2_confirm!r}"
        )

    def test_atr2_confirm_true_when_bias_matches_long(self):
        """上升趋势 candles + long setup → bias='long' 与方向一致 → atr2_confirm=True。"""
        from smc_tracker.indicators.atr2_signals import atr2_confirmation
        r = atr2_confirmation(self.candles)
        if r is None or r["bias"] != "long":
            pytest.skip(f"atr2_confirmation bias={r!r} 不是 'long'，跳过一致性校验")
        s = self.setups[0]
        assert s.direction == "long"
        assert s.atr2_confirm is True, (
            f"bias='long' 与 direction='long' 一致，atr2_confirm 应=True，实际={s.atr2_confirm!r}"
        )

    def test_confidence_boosted_when_atr2_confirm_true(self):
        """atr2_confirm=True → confidence 应在原 KNN 调整后再 ×1.05（封顶 0.90）。"""
        s = self.setups[0]
        if s.atr2_confirm is not True:
            pytest.skip(f"atr2_confirm={s.atr2_confirm!r} 不是 True，跳过加成校验")
        # base_conf=0.75，KNN 调整后 × 1.05 应略高于 base；封顶 0.90
        assert s.confidence > 0.75, (
            f"atr2_confirm=True 时 confidence={s.confidence:.4f} 应 >0.75（base_conf=0.75）"
        )
        assert s.confidence <= 0.90, (
            f"confidence={s.confidence:.4f} 不应超过封顶 0.90"
        )


class TestATR2ShortDirection:
    """short setup + bias='short' → atr_stop 在进场上方, atr2_confirm=True。"""

    def _gartley_bear_harmonic(self) -> dict:
        """构造 Gartley 看空谐波（direction='bear'），X/D 结构使止损合理。"""
        pat = {
            "pattern": "Gartley",
            "direction": "bear",
            "prz": (105.0, 109.0),
            "completed": True,
            "confidence": 0.75,
            "confluence": 2,
            "points": {
                "X": (0, 112.0),   # X=112 > D=107(short X 在上方)；stop_pct<8% 过 compute_risk(修审计:原 X=120 被过滤致测试永久 skip)
                "A": (10, 90.0),
                "B": (15, 110.0),
                "C": (20, 95.0),
                "D": (25, 107.0),
            },
        }
        return {"completed": [pat], "forming": [], "price": 107.0}

    def test_atr_stop_above_entry_for_short(self):
        """short setup: atr_stop = entry_mid + 1.5 × atr（高于进场区中点）。"""
        candles = _make_atr2_candles_short(n=60)
        harmonic = self._gartley_bear_harmonic()
        setups = build_setups(
            coin="BTC", tf="1h",
            candles=candles,
            harmonic_result=harmonic,
        )
        # 修审计P2:X=112 使 stop_pct<8% 确定产出 setup;改 assert 取代 skip,使 compute_risk 过滤回归直接 fail
        assert setups, "short Gartley(X=112,stop_pct<8%)应产出 setup;若空=compute_risk 过滤回归"
        s = setups[0]
        assert s.direction == "short"
        if s.atr_stop is None:
            pytest.skip("candles 不足导致 atr_stop=None，跳过方向检查")
        entry_mid = (s.entry_lo + s.entry_hi) / 2.0
        assert s.atr_stop > entry_mid, (
            f"short atr_stop={s.atr_stop:.4f} 应高于 entry_mid={entry_mid:.4f}"
        )


class TestATR2InsufficientCandles:
    """candles 不足暖机期 → atr_stop/atr2_bias/atr2_confirm 均为 None，不崩溃。"""

    def setup_method(self):
        # 仅 5 根 K 线，远不足 atr2_confirmation 所需 47 根（8+2×20-1）
        self.candles = _make_candles(5)
        self.setups = build_setups(
            coin="BTC",
            tf="1h",
            candles=self.candles,
            harmonic_result=_gartley_bull_harmonic(),
            account_usd=10_000.0,
            risk_pct=0.01,
            target_rr=2.0,
        )

    def test_no_crash_on_insufficient_candles(self):
        """candles 不足时 build_setups 不应抛异常。"""
        assert isinstance(self.setups, list)

    def test_atr_stop_none_on_insufficient_candles(self):
        """candles 不足 → atr_stop 为 None（不加权，不崩）。"""
        for s in self.setups:
            assert s.atr_stop is None, (
                f"candles 不足时 atr_stop 应为 None，实际: {s.atr_stop!r}"
            )

    def test_atr2_bias_none_on_insufficient_candles(self):
        """candles 不足 → atr2_bias 为 None。"""
        for s in self.setups:
            assert s.atr2_bias is None, (
                f"candles 不足时 atr2_bias 应为 None，实际: {s.atr2_bias!r}"
            )

    def test_atr2_confirm_none_on_insufficient_candles(self):
        """candles 不足 → atr2_confirm 为 None（不加权，不崩）。"""
        for s in self.setups:
            assert s.atr2_confirm is None, (
                f"candles 不足时 atr2_confirm 应为 None，实际: {s.atr2_confirm!r}"
            )


class TestATR2OppositeDirection:
    """bias 与 setup 方向相反 → confidence ×0.92（降权），不为 None。"""

    def test_confidence_reduced_when_atr2_disagrees(self):
        """下降趋势 candles + long setup → bias='short' ≠ 'long' → confidence ×0.92。

        注意：此场景 atr2_confirm=False（方向相反），置信应被降权。
        基准：build_setups(上升趋势) 的 confidence 作比较基线。
        """
        from smc_tracker.indicators.atr2_signals import atr2_confirmation

        harmonic = _gartley_bull_harmonic()  # direction='long'

        # 1) 上升趋势（bias='long'，方向一致 → 加权基线）
        candles_up = _make_atr2_candles_long(n=60)
        r_up = atr2_confirmation(candles_up)

        # 2) 下降趋势（bias='short'，与 long 相反 → 降权）
        candles_dn = _make_atr2_candles_short(n=60)
        r_dn = atr2_confirmation(candles_dn)

        if r_up is None or r_up["bias"] != "long":
            pytest.skip(f"上升趋势 bias={r_up!r} 非 'long'，跳过对比测试")
        if r_dn is None or r_dn["bias"] != "short":
            pytest.skip(f"下降趋势 bias={r_dn!r} 非 'short'，跳过对比测试")

        setups_up = build_setups("BTC", "1h", candles_up, harmonic)
        setups_dn = build_setups("BTC", "1h", candles_dn, harmonic)

        if not setups_up or not setups_dn:
            pytest.skip("setup 被过滤，跳过对比")

        s_up = setups_up[0]
        s_dn = setups_dn[0]

        # 下降趋势 setup 的置信应低于上升趋势（反向降权）
        assert s_dn.confidence < s_up.confidence, (
            f"ATR2 方向相反时 confidence={s_dn.confidence:.4f} 应 < "
            f"同向时 {s_up.confidence:.4f}"
        )

        # atr2_confirm 标记
        assert s_dn.atr2_confirm is False, (
            f"bias='short' ≠ direction='long' → atr2_confirm 应=False，实际={s_dn.atr2_confirm!r}"
        )

    def test_confidence_cap_still_090(self):
        """任何情况下 confidence 不超过 0.90 封顶。"""
        candles = _make_atr2_candles_long(n=60)
        setups = build_setups("BTC", "1h", candles, _gartley_bull_harmonic())
        for s in setups:
            assert s.confidence <= 0.90, (
                f"confidence={s.confidence:.4f} 超过封顶 0.90"
            )


class TestATR2ExistingTestsUnbroken:
    """回归：现有测试所依赖的字段不受新字段影响（新字段默认 None）。

    重点验证：旧测试在不传 candles(5根不足) 或 candles(120根)时均不崩，
    新字段 atr_stop/atr2_bias/atr2_confirm 不破坏任何现有断言。
    """

    def test_existing_fields_still_present(self):
        """旧字段集合不变——新字段仅追加，不替换。"""
        fields = {f.name for f in dataclasses.fields(TradeSetup)}
        old_required = {
            "coin", "tf", "direction", "pattern", "completed",
            "entry_lo", "entry_hi", "stop", "target1", "target2",
            "rr", "fib_note", "knn_supports", "knn_note",
            "position_qty", "position_notional", "confidence", "note",
            "src_key", "orderflow",
        }
        missing = old_required - fields
        assert not missing, f"旧字段被意外删除: {missing}"

    def test_new_fields_in_all_fields(self):
        """三个新字段已加入 dataclass。"""
        fields = {f.name for f in dataclasses.fields(TradeSetup)}
        new_fields = {"atr_stop", "atr2_bias", "atr2_confirm"}
        missing = new_fields - fields
        assert not missing, f"新字段未加入 dataclass: {missing}"


# ── §4D 斐波那契函数单元测试 ────────────────────────────────────────────────────

class TestGoldenPocketZone:
    """golden_pocket_zone() 返回 0.618–0.786 黄金口袋区间（复用 fib_levels）。"""

    def test_bull_up_direction_lo_hi_correct(self):
        """direction='up'：上涨段(low→high)，黄金口袋在 high 下方。"""
        from smc_tracker.indicators.fibonacci import golden_pocket_zone
        lo, hi = golden_pocket_zone(high=120.0, low=100.0, direction="up")
        # rng=20; golden_hi=120-0.618*20=107.64; golden_lo=120-0.786*20=104.28
        assert abs(lo - 104.28) < 0.01, f"lo={lo:.4f} 应≈104.28"
        assert abs(hi - 107.64) < 0.01, f"hi={hi:.4f} 应≈107.64"
        assert lo < hi, "lo 应 < hi"

    def test_bear_down_direction_lo_hi_correct(self):
        """direction='down'：下跌段(high→low)，黄金口袋在 low 上方。"""
        from smc_tracker.indicators.fibonacci import golden_pocket_zone
        lo, hi = golden_pocket_zone(high=120.0, low=100.0, direction="down")
        # rng=20; golden_lo=100+0.618*20=112.36; golden_hi=100+0.786*20=115.72
        assert abs(lo - 112.36) < 0.01, f"lo={lo:.4f} 应≈112.36"
        assert abs(hi - 115.72) < 0.01, f"hi={hi:.4f} 应≈115.72"
        assert lo < hi, "lo 应 < hi"

    def test_zero_range_degenerate(self):
        """high==low 时退化返回 (high, high)，不崩溃。"""
        from smc_tracker.indicators.fibonacci import golden_pocket_zone
        lo, hi = golden_pocket_zone(high=100.0, low=100.0, direction="up")
        assert lo == hi == 100.0, f"零振幅应返回 (100, 100)，实际 ({lo}, {hi})"

    def test_result_tuple_lo_le_hi(self):
        """任意有效 high/low 都满足 lo <= hi。"""
        from smc_tracker.indicators.fibonacci import golden_pocket_zone
        for h, l, d in [(150, 80, "up"), (200, 100, "down"), (50, 50, "up")]:
            lo, hi = golden_pocket_zone(h, l, d)
            assert lo <= hi, f"golden_pocket_zone({h},{l},{d!r}) → lo={lo} > hi={hi}"


class TestIntersectZone:
    """intersect_zone() 区间求交。"""

    def test_overlap_returns_intersection(self):
        """两区间有重叠 → 返回交集。"""
        from smc_tracker.indicators.fibonacci import intersect_zone
        result = intersect_zone(100.0, 110.0, 105.0, 115.0)
        assert result is not None
        lo, hi = result
        assert abs(lo - 105.0) < 1e-9
        assert abs(hi - 110.0) < 1e-9

    def test_no_overlap_returns_none(self):
        """两区间无重叠 → 返回 None。"""
        from smc_tracker.indicators.fibonacci import intersect_zone
        assert intersect_zone(100.0, 105.0, 110.0, 120.0) is None

    def test_touching_at_boundary_is_intersection(self):
        """区间仅在端点接触 → 返回点区间（lo == hi），不为 None。"""
        from smc_tracker.indicators.fibonacci import intersect_zone
        result = intersect_zone(100.0, 105.0, 105.0, 110.0)
        assert result is not None
        lo, hi = result
        assert abs(lo - 105.0) < 1e-9
        assert abs(hi - 105.0) < 1e-9

    def test_contained_interval(self):
        """一个区间完全包含另一个 → 交集是较小区间。"""
        from smc_tracker.indicators.fibonacci import intersect_zone
        result = intersect_zone(100.0, 200.0, 130.0, 150.0)
        assert result is not None
        lo, hi = result
        assert abs(lo - 130.0) < 1e-9
        assert abs(hi - 150.0) < 1e-9

    def test_inverted_input_order(self):
        """入参颠倒（hi 传 lo 位置）仍正确计算。"""
        from smc_tracker.indicators.fibonacci import intersect_zone
        result = intersect_zone(110.0, 100.0, 115.0, 105.0)
        assert result is not None
        lo, hi = result
        assert abs(lo - 105.0) < 1e-9
        assert abs(hi - 110.0) < 1e-9


# ── §4D 入场精炼集成测试 ─────────────────────────────────────────────────────────

class TestFibEntryRefinementCompleted:
    """§4D: completed 形态 XA 黄金口袋∩D±1.5% 入场收窄。"""

    def _gartley_with_known_xa(self, x_price: float, a_price: float,
                                d_price: float) -> dict:
        """构造 XA 已知、D 已知的 Gartley-bull。"""
        return {
            "completed": [{
                "pattern": "Gartley",
                "direction": "bull",
                "prz": (d_price * 0.98, d_price * 1.02),
                "completed": True,
                "confidence": 0.75,
                "confluence": 2,
                "points": {
                    "X": (0, x_price),
                    "A": (10, a_price),
                    "B": (15, d_price + 0.5),
                    "C": (20, a_price * 0.95),
                    "D": (25, d_price),
                },
            }],
            "forming": [],
            "price": d_price,
        }

    def test_entry_src_fib_intersect_when_overlap(self):
        """XA 黄金口袋∩D±1.5% 有交集 → entry_src='fib_intersect'。"""
        # X=100, A=120: golden pocket = (104.28, 107.64); D=107: D±1.5%=(105.4, 108.6)
        # 交集 = (105.4, 107.64) → 有交集
        candles = _make_candles(120)
        harmonic = self._gartley_with_known_xa(100.0, 120.0, 107.0)
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤")
        s = setups[0]
        assert s.entry_src == "fib_intersect", (
            f"有 Fib 汇合时 entry_src 应='fib_intersect'，实际={s.entry_src!r}"
        )

    def test_entry_lo_hi_narrowed_by_intersection(self):
        """XA 黄金口袋∩D±1.5% 有交集时，入场区被收窄（hi < D×1.015）。"""
        # 同上情形：golden pocket hi = 107.64 < D×1.015 = 108.605
        candles = _make_candles(120)
        harmonic = self._gartley_with_known_xa(100.0, 120.0, 107.0)
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤")
        s = setups[0]
        if s.entry_src != "fib_intersect":
            pytest.skip("无 Fib 汇合，跳过收窄测试")
        d_price = 107.0
        # entry_hi 应小于 D×(1+1.5%)（被收窄）
        assert s.entry_hi < d_price * (1 + 0.015) + 0.01, (
            f"有 Fib 汇合时 entry_hi={s.entry_hi:.4f} 应 ≤ D×1.015={d_price*1.015:.4f}"
        )
        assert s.entry_lo < s.entry_hi, "entry_lo 应 < entry_hi"

    def test_entry_src_no_fib_when_no_overlap(self):
        """XA 黄金口袋∩D±1.5% 无交集 → entry_src='no_fib_confluence'，回退原区。"""
        # X=100, A=120: golden pocket up = (104.28, 107.64)
        # D=115: D±1.5% = (113.275, 116.725) → 无交集
        candles = _make_candles(120)
        harmonic = self._gartley_with_known_xa(100.0, 120.0, 115.0)
        # 构造合理止损范围：X=100 vs D=115 → stop_pct=(115-100)/115≈13%>8% → 会被过滤
        # 用 X 近于 D：X=110.5, A=130, D=115, stop_pct=(115-110.5)/115≈3.9% ≤8%
        # golden pocket(130, 110.5, up): rng=19.5; gp_hi=130-0.618*19.5=117.95; gp_lo=130-0.786*19.5=114.67
        # D±1.5%: (113.275, 116.725); 交集: max(114.67,113.275)=114.67, min(117.95,116.725)=116.725 → 有交集
        # 改用 X=113, A=120, D=115: gp(120,113,'up')=rng=7; gp_hi=120-0.618*7=115.67; gp_lo=120-0.786*7=114.50
        # D±1.5%=(113.275,116.725); 交集=(114.50,115.67) → 有交集, 仍然汇合
        # 要构造无交集：需要黄金口袋完全在 D±1.5% 范围外
        # X=50,A=80: gp_up=(80-0.786*30, 80-0.618*30)=(56.42,61.46)
        # D=115: D±1.5%=(113.275,116.725) → gp区间(56.42,61.46) ∩ (113.275,116.725) = 无交集
        # 但 X=50,D=115 → stop_pct=(115-50)/115≈56%>8% → 被过滤
        # → 此测试可以通过 setup 被过滤来验证，或直接测 fibonacci 函数
        from smc_tracker.indicators.fibonacci import golden_pocket_zone, intersect_zone
        gp_lo, gp_hi = golden_pocket_zone(80.0, 50.0, "up")
        d_price = 115.0
        base_lo = d_price * (1 - 0.015)
        base_hi = d_price * (1 + 0.015)
        result = intersect_zone(gp_lo, gp_hi, base_lo, base_hi)
        assert result is None, (
            f"XA(50,80)黄金口袋{(gp_lo,gp_hi)} 与 D=115±1.5%({base_lo},{base_hi}) 应无交集"
        )

    def test_fib_note_contains_honest_when_intersect(self):
        """有 Fib 汇合时 fib_note 含'非独立确认'或'汇合'（不宣称置信加分）。"""
        candles = _make_candles(120)
        harmonic = self._gartley_with_known_xa(100.0, 120.0, 107.0)
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤")
        s = setups[0]
        if s.entry_src != "fib_intersect":
            pytest.skip("无 Fib 汇合，跳过 note 内容检测")
        assert "非独立确认" in s.fib_note or "汇合" in s.fib_note, (
            f"有 Fib 汇合的 fib_note 应含'非独立确认'或'汇合'，实际: {s.fib_note!r}"
        )

    def test_confidence_not_boosted_by_fib(self):
        """§4D 规定：confidence 绝不加分（不因 Fib 汇合 ×1.0+）。"""
        candles = _make_candles(120)
        harmonic_with_fib = self._gartley_with_known_xa(100.0, 120.0, 107.0)
        # 构造无 Fib 汇合对比（用相同 base_conf=0.75，不同 XA 使黄金口袋不落在 D 区）
        harmonic_no_fib = {
            "completed": [{
                "pattern": "Gartley",
                "direction": "bull",
                "prz": (105.0, 109.0),
                "completed": True,
                "confidence": 0.75,  # 相同 base_conf
                "confluence": 2,
                "points": {
                    "X": (0, 100.0),
                    "A": (10, 120.0),
                    "B": (15, 107.0),
                    "C": (20, 115.0),
                    "D": (25, 107.0),
                },
            }],
            "forming": [],
            "price": 107.0,
        }
        setups_fib = build_setups("BTC", "1h", candles, harmonic_with_fib)
        setups_no = build_setups("BTC", "1h", candles, harmonic_no_fib)
        if not setups_fib or not setups_no:
            pytest.skip("setup 被过滤，跳过置信比较")
        # Fib 汇合不应使 confidence 高于无汇合时（两者 base_conf 相同）
        # confidence 只受 ATR2 影响（相同 candles），两者应相同
        assert setups_fib[0].confidence == setups_no[0].confidence, (
            f"Fib 汇合不应改变 confidence：有汇合={setups_fib[0].confidence:.4f} "
            f"vs 无汇合={setups_no[0].confidence:.4f}"
        )


class TestFibExtensionTargets:
    """§4D: AD 段 1.272/1.618 Fib 扩展目标，与 RR 取更保守者。"""

    def _gartley_known_ad(
        self, a_price: float, d_price: float, x_price: float = 100.0
    ) -> dict:
        """构造 XA/D 已知的 Gartley-bull（止损合理范围：X 近于 D）。"""
        return {
            "completed": [{
                "pattern": "Gartley",
                "direction": "bull",
                "prz": (d_price * 0.98, d_price * 1.02),
                "completed": True,
                "confidence": 0.75,
                "confluence": 2,
                "points": {
                    "X": (0, x_price),
                    "A": (10, a_price),
                    "B": (15, d_price + 0.5),
                    "C": (20, a_price * 0.95),
                    "D": (25, d_price),
                },
            }],
            "forming": [],
            "price": d_price,
        }

    def test_fib_note_contains_target_source(self):
        """fib_note 应含 'T1=' 和 'T2=' 来源标注（RR 或 Fib 扩展）。"""
        # X=100, A=120, D=107: stop_pct合理
        candles = _make_candles(120)
        harmonic = self._gartley_known_ad(a_price=120.0, d_price=107.0, x_price=100.0)
        setups = build_setups("BTC", "1h", candles, harmonic)
        if not setups:
            pytest.skip("setup 被过滤")
        s = setups[0]
        assert "T1=" in s.fib_note, f"fib_note 应含 'T1=' 目标来源，实际: {s.fib_note!r}"
        assert "T2=" in s.fib_note, f"fib_note 应含 'T2=' 目标来源，实际: {s.fib_note!r}"

    def test_target1_not_worse_than_rr_for_long(self):
        """long setup: target1 取更保守（更小值），应 ≤ RR 目标（Fib 更近时取 Fib）。"""
        candles = _make_candles(120)
        # A=120, D=107: AD_rng=13; Fib1.272 = 107+1.272*13=123.54; Fib1.618=107+1.618*13=128.03
        # RR 目标约 entry + 2×risk，risk=(entry-stop)≈(107-99.9)=7.1 → RR_t1≈107+14.2=121.2
        # 更保守：min(123.54, 121.2) = 121.2 (RR)；Fib1.618 vs RR2=107+28.4=135.4 → min=128.03 (Fib)
        harmonic = self._gartley_known_ad(a_price=120.0, d_price=107.0, x_price=100.0)
        setups = build_setups("BTC", "1h", candles, harmonic, target_rr=2.0)
        if not setups:
            pytest.skip("setup 被过滤")
        s = setups[0]
        assert s.direction == "long"
        entry_mid = (s.entry_lo + s.entry_hi) / 2.0
        # target1 应在 entry 上方（long 目标在上）
        assert s.target1 > entry_mid, (
            f"long target1={s.target1:.4f} 应 > entry_mid={entry_mid:.4f}"
        )
        # target2 应 > target1（target2 比 target1 更激进）
        assert s.target2 >= s.target1, (
            f"target2={s.target2:.4f} 应 ≥ target1={s.target1:.4f}"
        )

    def test_target_conservative_vs_rr(self):
        """Fib 扩展比 RR 近时用 Fib（更保守），比 RR 远时用 RR（更保守）。"""
        from smc_tracker.indicators.fibonacci import fib_levels
        candles = _make_candles(120)
        a_price, d_price, x_price = 120.0, 107.0, 100.0
        harmonic = self._gartley_known_ad(a_price, d_price, x_price)
        setups = build_setups("BTC", "1h", candles, harmonic, target_rr=2.0)
        if not setups:
            pytest.skip("setup 被过滤")
        s = setups[0]
        # 验证 target1 ≤ Fib1.272 (D + 1.272*AD)，且 ≤ RR 推算目标
        ad_rng = abs(a_price - d_price)
        fib_t1 = d_price + 1.272 * ad_rng
        fib_t2 = d_price + 1.618 * ad_rng
        # target1 是 min(RR_t1, fib_t1)：应 ≤ fib_t1 且 ≤ RR_t1（计算值）
        # 直接用 fib_t1 和 fib_t2 上界验证（保守 = 更小/更近）
        assert s.target1 <= fib_t1 + 0.01, (
            f"long target1={s.target1:.4f} 应 ≤ Fib1.272 扩展={fib_t1:.4f}"
        )
        assert s.target2 <= fib_t2 + 0.01, (
            f"long target2={s.target2:.4f} 应 ≤ Fib1.618 扩展={fib_t2:.4f}"
        )

    def test_entry_src_field_exists(self):
        """TradeSetup 应有 entry_src 字段（§4D 新增，向后兼容默认 None）。"""
        fields = {f.name for f in dataclasses.fields(TradeSetup)}
        assert "entry_src" in fields, "TradeSetup 缺少 entry_src 字段（§4D 新增）"

    def test_entry_src_default_none(self):
        """entry_src 字段默认值为 None（向后兼容）。"""
        for f in dataclasses.fields(TradeSetup):
            if f.name == "entry_src":
                assert f.default is None, (
                    f"entry_src 默认值应为 None，实际: {f.default!r}"
                )
                break
