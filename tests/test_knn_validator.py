"""测试 KNN 方向验证器（TDD RED→GREEN）。

真实调用 KNNPredictor（不 mock），使用合成确定性 candles。
"""
from __future__ import annotations

import math

import pytest

from smc_tracker.models import Candle
from smc_tracker.signals import KNNVerdict, validate_direction


# ---------------------------------------------------------------------------
# 辅助构造函数
# ---------------------------------------------------------------------------

def make_candles(n: int, *, trend: str = "up") -> list[Candle]:
    """生成 n 根合成 K 线，trend='up' 为上升趋势，'down' 为下降趋势。"""
    candles: list[Candle] = []
    base_price = 100.0
    for i in range(n):
        if trend == "up":
            c = base_price + i * 0.5
        else:
            c = base_price - i * 0.5
        o = c - 0.2
        h = c + 0.3
        l = c - 0.3
        candles.append(Candle(
            coin="BTC",
            interval="5m",
            open_time_ms=i * 300_000,
            close_time_ms=(i + 1) * 300_000 - 1,
            o=o,
            h=h,
            l=l,
            c=c,
            v=float(100 + i),
            n=10,
        ))
    return candles


# ---------------------------------------------------------------------------
# 正常场景：样本充足，上升趋势 candles，方向 "bull"
# ---------------------------------------------------------------------------

class TestValidateDirectionBull:
    def test_returns_knn_verdict(self) -> None:
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, "bull")
        assert result is not None
        assert isinstance(result, KNNVerdict)

    def test_p_up_in_range(self) -> None:
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, "bull")
        assert result is not None
        assert 0.0 <= result.p_up <= 1.0

    def test_knn_confidence_in_range(self) -> None:
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, "bull")
        assert result is not None
        assert 0.0 <= result.knn_confidence <= 1.0
        # confidence = |p_up - 0.5| * 2
        assert math.isclose(result.knn_confidence, abs(result.p_up - 0.5) * 2, abs_tol=1e-9)

    def test_samples_positive(self) -> None:
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, "bull")
        assert result is not None
        assert result.samples > 0

    def test_note_mentions_random_or_auxiliary(self) -> None:
        """诚实标注：note 必须含 '随机' 或 '辅助'（CLAUDE.md §二）。"""
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, "bull")
        assert result is not None
        assert "随机" in result.note or "辅助" in result.note

    def test_supports_is_bool(self) -> None:
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, "bull")
        assert result is not None
        assert isinstance(result.supports, bool)


# ---------------------------------------------------------------------------
# 方向归一化：多种别名均等价
# ---------------------------------------------------------------------------

class TestDirectionNormalization:
    @pytest.mark.parametrize("direction", ["bull", "long", "up", "bullish"])
    def test_bullish_aliases_return_verdict(self, direction: str) -> None:
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, direction)
        assert result is not None
        assert isinstance(result, KNNVerdict)

    @pytest.mark.parametrize("direction", ["bear", "short", "down", "bearish"])
    def test_bearish_aliases_return_verdict(self, direction: str) -> None:
        candles = make_candles(200, trend="down")
        result = validate_direction(candles, direction)
        assert result is not None
        assert isinstance(result, KNNVerdict)

    @pytest.mark.parametrize("direction", ["sideways", "neutral", "unknown", "", "BULL", "Long"])
    def test_invalid_direction_returns_none(self, direction: str) -> None:
        """非法方向（大小写敏感，不在白名单内）→ None，不崩溃。"""
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, direction)
        assert result is None


# ---------------------------------------------------------------------------
# 样本不足：短 candles → None，不崩溃
# ---------------------------------------------------------------------------

class TestInsufficientSamples:
    def test_too_few_candles_returns_none(self) -> None:
        """k=15 默认，candles < k + horizon 有效特征 → KNNPredictor.fit 返回 False → None。"""
        candles = make_candles(5, trend="up")
        result = validate_direction(candles, "bull")
        assert result is None

    def test_empty_candles_returns_none(self) -> None:
        result = validate_direction([], "bull")
        assert result is None

    def test_borderline_short_returns_none(self) -> None:
        """刚好不够 k 个有效样本（加上 warmup，20 根远不够）。"""
        candles = make_candles(20, trend="up")
        result = validate_direction(candles, "bull")
        assert result is None


# ---------------------------------------------------------------------------
# supports 字段语义正确性
# ---------------------------------------------------------------------------

class TestSupportsSemantics:
    def test_supports_long_when_knn_predicts_long(self) -> None:
        """当 KNN 预测方向=long 且 setup=bull 时，supports=True。"""
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, "bull")
        assert result is not None
        # supports 的值由 KNN 预测决定，验证语义一致性
        if result.p_up > 0.5:
            assert result.supports is True
        else:
            assert result.supports is False

    def test_supports_false_when_direction_mismatch(self) -> None:
        """下降趋势 candles 做 bull setup：KNN 若预测 short，则 supports=False。"""
        candles = make_candles(200, trend="down")
        result = validate_direction(candles, "bull")
        # 即使 KNN 预测 long，也验证 supports 语义正确
        assert result is not None
        if result.p_up <= 0.5:
            assert result.supports is False

    def test_bearish_setup_supports_semantics(self) -> None:
        """setup 方向 short：KNN 预测 short(p_up<0.5) → supports=True。"""
        candles = make_candles(200, trend="down")
        result = validate_direction(candles, "short")
        assert result is not None
        if result.p_up <= 0.5:
            assert result.supports is True
        else:
            assert result.supports is False


# ---------------------------------------------------------------------------
# 自定义 k / horizon 参数
# ---------------------------------------------------------------------------

class TestCustomParameters:
    def test_custom_k_and_horizon(self) -> None:
        candles = make_candles(200, trend="up")
        result = validate_direction(candles, "long", k=5, horizon=3)
        assert result is not None
        assert isinstance(result, KNNVerdict)
        assert result.samples > 0
