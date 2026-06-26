"""tests/test_atr2_signals.py — ATR2 动量确认信号 TDD 测试套件。

合成确定性测试：不依赖网络/真实数据，所有输入手工构造。
诚实标注：ATR2 是动量确认辅助指标，非预测保证。
"""
from __future__ import annotations

import math
import pytest

from smc_tracker.indicators.atr2_signals import atr2_confirmation


# ── 辅助：合成 Candle 对象 ────────────────────────────────────────────────────


class _Candle:
    """属性访问方式（.o/.h/.l/.c/.v），与 technical.ohlcv_arrays 一致。"""
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self, o: float, h: float, lo: float, c: float, v: float) -> None:
        self.o = o
        self.h = h
        self.l = lo
        self.c = c
        self.v = v


def _make_trending_up(n: int = 60, start: float = 100.0, step: float = 0.5) -> list[_Candle]:
    """生成上升趋势 K 线（每根 close 递增 step，用于验证 confirmation>0）。"""
    candles: list[_Candle] = []
    price = start
    for _ in range(n):
        o = price
        c = price + step
        h = c + 0.1
        lo = o - 0.1
        candles.append(_Candle(o, h, lo, c, 1000.0))
        price = c
    return candles


def _make_trending_down(n: int = 60, start: float = 130.0, step: float = 0.5) -> list[_Candle]:
    """生成下降趋势 K 线（每根 close 递减 step，用于验证 confirmation<0）。"""
    candles: list[_Candle] = []
    price = start
    for _ in range(n):
        o = price
        c = price - step
        h = o + 0.1
        lo = c - 0.1
        candles.append(_Candle(o, h, lo, c, 1000.0))
        price = c
    return candles


def _make_sideways(n: int = 60, base: float = 100.0, amplitude: float = 0.05) -> list[_Candle]:
    """生成横盘 K 线（close 在 base±amplitude 之间震荡，动量趋近于零）。"""
    import math as _math
    candles: list[_Candle] = []
    for i in range(n):
        # 正弦震荡，平均动量极小
        c = base + amplitude * _math.sin(i * _math.pi / 4)
        o = base + amplitude * _math.sin((i - 1) * _math.pi / 4) if i > 0 else base
        h = max(o, c) + 0.01
        lo = min(o, c) - 0.01
        candles.append(_Candle(o, h, lo, c, 1000.0))
    return candles


# ── 测试 1：上升趋势 → confirmation>0, bias="long" ────────────────────────────


class TestUpTrend:
    def setup_method(self):
        candles = _make_trending_up(n=60, step=0.5)
        self.result = atr2_confirmation(candles)

    def test_returns_dict(self):
        assert isinstance(self.result, dict), "应返回 dict"

    def test_confirmation_positive(self):
        r = self.result
        assert r is not None
        assert r["confirmation"] > 0, (
            f"上升趋势 confirmation 应>0，实际={r['confirmation']:.4f}"
        )

    def test_bias_long(self):
        r = self.result
        assert r is not None
        assert r["bias"] == "long", (
            f"上升趋势 bias 应='long'，实际={r['bias']!r}"
        )

    def test_atr_positive_finite(self):
        r = self.result
        assert r is not None
        atr_val = r["atr"]
        assert atr_val > 0 and math.isfinite(atr_val), (
            f"atr 应为正有限数，实际={atr_val}"
        )

    def test_atr_pct_in_reasonable_range(self):
        r = self.result
        assert r is not None
        atr_pct = r["atr_pct"]
        assert 0.0 < atr_pct < 1.0, (
            f"atr_pct 应在 (0, 1) 范围内，实际={atr_pct:.6f}"
        )


# ── 测试 2：下降趋势 → confirmation<0, bias="short" ─────────────────────────


class TestDownTrend:
    def setup_method(self):
        candles = _make_trending_down(n=60, step=0.5)
        self.result = atr2_confirmation(candles)

    def test_returns_dict(self):
        assert isinstance(self.result, dict), "应返回 dict"

    def test_confirmation_negative(self):
        r = self.result
        assert r is not None
        assert r["confirmation"] < 0, (
            f"下降趋势 confirmation 应<0，实际={r['confirmation']:.4f}"
        )

    def test_bias_short(self):
        r = self.result
        assert r is not None
        assert r["bias"] == "short", (
            f"下降趋势 bias 应='short'，实际={r['bias']!r}"
        )

    def test_atr_positive_finite(self):
        r = self.result
        assert r is not None
        assert r["atr"] > 0 and math.isfinite(r["atr"])


# ── 测试 3：横盘 → bias="neutral" ────────────────────────────────────────────


class TestSideways:
    def test_neutral_bias(self):
        """横盘震荡，magnified 动量应接近零 → bias='neutral'。"""
        candles = _make_sideways(n=60)
        r = atr2_confirmation(candles)
        assert r is not None
        # 横盘时 confirmation 绝对值应很小（threshold=1.0），bias='neutral'
        assert r["bias"] == "neutral", (
            f"横盘 bias 应='neutral'，实际={r['bias']!r}，"
            f"confirmation={r['confirmation']:.4f}"
        )


# ── 测试 4：K 线不足 → 返回 None ──────────────────────────────────────────────


class TestInsufficientCandles:
    def test_none_on_too_few(self):
        """K 线少于 trend_length+smoothness×2 时应返回 None，不崩溃。"""
        candles = _make_trending_up(n=5)
        r = atr2_confirmation(candles)
        assert r is None, f"K 线不足时应返回 None，实际={r!r}"

    def test_none_on_empty(self):
        r = atr2_confirmation([])
        assert r is None, "空 candles 应返回 None"

    def test_none_on_exactly_min_minus_one(self):
        """恰好不够（trend_length=8, smoothness=20，需 8+20-1=27 根，测 26 根）。"""
        # 最小需求: trend_length + smoothness - 1 = 8 + 20 - 1 = 27
        # 再加 ATR 14，最小 max(27, 14+1) = 27 根才能有 SMA(normMom, 20) 末值
        # 实际 smoothness*2 保证更安全；测试 26 根确认不足时返回 None
        candles = _make_trending_up(n=26)
        r = atr2_confirmation(candles)
        assert r is None, f"26 根 K 线应返回 None（不足），实际={r!r}"


# ── 测试 5：ATR/atr_pct 数值合理性 ───────────────────────────────────────────


class TestAtrValues:
    def test_atr_less_than_price(self):
        """ATR 应小于价格（不合理的放大意味着实现错误）。"""
        candles = _make_trending_up(n=60, start=1000.0, step=1.0)
        r = atr2_confirmation(candles)
        assert r is not None
        # 修审计 nit:原 atr < atr*10+1000 恒真=无断言。改有意义:ATR 应远小于末价(<10%)且为正。
        last_close = candles[-1].c
        assert 0.0 < r["atr"] < last_close * 0.1, (
            f"ATR={r['atr']:.4f} 应为正且 <末价{last_close:.1f}的10%(否则放大bug)"
        )
        # atr_pct = atr/price，应在合理范围
        assert r["atr_pct"] < 0.5, f"atr_pct={r['atr_pct']:.4f} 不应大于50%"

    def test_atr_pct_equals_atr_div_price(self):
        """atr_pct 应等于 atr / 末根 close（浮点精度内）。"""
        candles = _make_trending_up(n=60, start=100.0, step=0.5)
        r = atr2_confirmation(candles)
        assert r is not None
        # 末根 close
        last_close = candles[-1].c
        expected_pct = r["atr"] / last_close
        assert math.isclose(r["atr_pct"], expected_pct, rel_tol=1e-6), (
            f"atr_pct={r['atr_pct']:.8f} 应 ≈ atr/close={expected_pct:.8f}"
        )


# ── 测试 6：参数化 — 自定义 trend_length/smoothness/magnify ──────────────────


class TestCustomParams:
    def test_larger_magnify_amplifies_confirmation(self):
        """magnify 越大，|confirmation| 越大（线性关系）。"""
        candles = _make_trending_up(n=60, step=0.5)
        r1 = atr2_confirmation(candles, magnify=1.0)
        r3 = atr2_confirmation(candles, magnify=3.0)
        assert r1 is not None and r3 is not None
        # magnify=3 的 confirmation 绝对值应 ≈ magnify=1 的 3 倍
        if abs(r1["confirmation"]) > 1e-10:
            ratio = r3["confirmation"] / r1["confirmation"]
            assert math.isclose(ratio, 3.0, rel_tol=1e-6), (
                f"magnify 3/1 比值应≈3.0，实际={ratio:.4f}"
            )

    def test_custom_threshold_changes_bias(self):
        """threshold 很大时，中等动量的 bias 应变为 'neutral'。"""
        candles = _make_trending_up(n=60, step=0.5)
        # 默认 threshold=1.0，上升趋势应为 'long'
        r_low = atr2_confirmation(candles, threshold=1.0)
        # 极大 threshold=999，即使有动量也是 'neutral'
        r_high = atr2_confirmation(candles, threshold=999.0)
        assert r_low is not None and r_high is not None
        assert r_low["bias"] == "long"
        assert r_high["bias"] == "neutral", (
            f"极大 threshold 时 bias 应='neutral'，实际={r_high['bias']!r}"
        )


# ── 测试 7：返回字段完整性 ───────────────────────────────────────────────────


class TestReturnFields:
    def test_all_fields_present(self):
        """返回 dict 应包含 confirmation/bias/atr/atr_pct 四个字段。"""
        candles = _make_trending_up(n=60)
        r = atr2_confirmation(candles)
        assert r is not None
        for field in ("confirmation", "bias", "atr", "atr_pct"):
            assert field in r, f"返回 dict 缺少字段: {field!r}"

    def test_confirmation_is_float(self):
        candles = _make_trending_up(n=60)
        r = atr2_confirmation(candles)
        assert r is not None
        assert isinstance(r["confirmation"], float), (
            f"confirmation 应为 float，实际={type(r['confirmation'])}"
        )

    def test_bias_is_string(self):
        candles = _make_trending_up(n=60)
        r = atr2_confirmation(candles)
        assert r is not None
        assert isinstance(r["bias"], str)
        assert r["bias"] in ("long", "short", "neutral"), (
            f"bias 应为 'long'/'short'/'neutral'，实际={r['bias']!r}"
        )

    def test_confirmation_is_finite(self):
        """confirmation 应为有限浮点数，不得 NaN/inf。"""
        candles = _make_trending_up(n=60)
        r = atr2_confirmation(candles)
        assert r is not None
        assert math.isfinite(r["confirmation"]), (
            f"confirmation 不得为 NaN/inf，实际={r['confirmation']}"
        )
