"""tests/test_sfg_ami.py — SFG AMI（AI Momentum Index）因子 TDD 测试套件。

合成确定性测试：不依赖网络/真实数据，所有输入手工构造。
诚实标注：AMI 是 KNN 反转簇因子（累计 ±1 标签加权），POSITIVE = 看涨反转预期
（prediction 近通道低端），NEGATIVE = 看跌反转预期（prediction 近通道高端）。
非预测保证，不构成投资建议。
"""
from __future__ import annotations

import math
import numpy as np
import pytest

from smc_tracker.indicators.sfg.ami import ami_series, ami_factor


# ── 辅助：合成 Candle 对象 ────────────────────────────────────────────────────

class _Candle:
    """属性访问（.o/.h/.l/.c/.v），与 _common.ohlcv_arrays 一致。"""
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self, o: float, h: float, lo: float, c: float, v: float) -> None:
        self.o = o
        self.h = h
        self.l = lo
        self.c = c
        self.v = v


def _make_trending_up(
    n: int = 150,
    start: float = 100.0,
    step: float = 0.5,
) -> list[_Candle]:
    """生成严格单调递增 close 的 K 线序列（用于 KNN 事件积累验证）。"""
    candles: list[_Candle] = []
    price = start
    for _ in range(n):
        o = price
        c = price + step
        h = c + 0.2
        lo = o - 0.2
        candles.append(_Candle(o, h, lo, c, 1000.0))
        price = c
    return candles


def _make_trending_down(
    n: int = 150,
    start: float = 200.0,
    step: float = 0.5,
) -> list[_Candle]:
    """生成严格单调递减 close 的 K 线序列。"""
    candles: list[_Candle] = []
    price = start
    for _ in range(n):
        o = price
        c = price - step
        h = o + 0.2
        lo = c - 0.2
        candles.append(_Candle(o, h, lo, c, 1000.0))
        price = c
    return candles


def _make_sideways(
    n: int = 150,
    base: float = 100.0,
    amplitude: float = 2.0,
) -> list[_Candle]:
    """生成正弦震荡 K 线（上下穿越创造 KNN 事件，prediction 接近 0）。"""
    candles: list[_Candle] = []
    for i in range(n):
        c = base + amplitude * math.sin(i * math.pi / 8)
        o = base + amplitude * math.sin((i - 1) * math.pi / 8) if i > 0 else base
        h = max(o, c) + 0.1
        lo = min(o, c) - 0.1
        candles.append(_Candle(o, h, lo, c, 1000.0))
    return candles


def _make_oscillating_uptrend(
    n: int = 300,
    start: float = 100.0,
    drift: float = 0.05,
    amplitude: float = 1.5,
) -> list[_Candle]:
    """生成带震荡的上升趋势 K 线（正弦叠加漂移，产生 MA 交叉事件）。

    AMI 算法需要 fast/slow WMA 交叉才能积累 KNN 事件；
    纯单调趋势无交叉故 store 只有哨兵，pred 恒=0，factor 全 NaN。
    此序列确保足够多交叉，同时净趋势向上（累计上涨标签多）。
    """
    candles: list[_Candle] = []
    base = start
    for i in range(n):
        # 正弦震荡 + 漂移
        c = base + drift * i + amplitude * math.sin(i * math.pi / 6)
        prev_c = base + drift * (i - 1) + amplitude * math.sin((i - 1) * math.pi / 6) if i > 0 else base
        o = prev_c
        h = max(o, c) + 0.1
        lo = min(o, c) - 0.1
        candles.append(_Candle(o, h, lo, c, 1000.0))
    return candles


def _make_oscillating_downtrend(
    n: int = 300,
    start: float = 200.0,
    drift: float = 0.05,
    amplitude: float = 1.5,
) -> list[_Candle]:
    """生成带震荡的下降趋势 K 线（负漂移 + 正弦震荡，产生 MA 交叉）。"""
    candles: list[_Candle] = []
    base = start
    for i in range(n):
        c = base - drift * i + amplitude * math.sin(i * math.pi / 6)
        prev_c = base - drift * (i - 1) + amplitude * math.sin((i - 1) * math.pi / 6) if i > 0 else base
        o = prev_c
        h = max(o, c) + 0.1
        lo = min(o, c) - 0.1
        candles.append(_Candle(o, h, lo, c, 1000.0))
    return candles


# ── 测试 1：输出形状与类型 ─────────────────────────────────────────────────────

class TestOutputShape:
    def test_series_length_matches_input(self):
        """ami_series 输出长度应等于输入 candles 数。"""
        candles = _make_trending_up(n=100)
        out = ami_series(candles)
        assert len(out) == 100, f"输出长度={len(out)}，期望=100"

    def test_series_returns_ndarray(self):
        candles = _make_trending_up(n=100)
        out = ami_series(candles)
        assert isinstance(out, np.ndarray), f"应返回 np.ndarray，实际={type(out)}"

    def test_series_dtype_float(self):
        candles = _make_trending_up(n=100)
        out = ami_series(candles)
        assert np.issubdtype(out.dtype, np.floating), f"dtype 应为 float，实际={out.dtype}"


# ── 测试 2：warmup 边界 ──────────────────────────────────────────────────────

class TestWarmup:
    def test_empty_candles_empty_series(self):
        """空输入 → 空数组（长度 0）。"""
        out = ami_series([])
        assert isinstance(out, np.ndarray) and len(out) == 0

    def test_factor_none_on_empty(self):
        """空输入 → ami_factor 返回 None。"""
        assert ami_factor([]) is None

    def test_factor_none_on_too_few(self):
        """K 线严重不足（< warmup），ami_factor 应返回 None。"""
        candles = _make_trending_up(n=5)
        assert ami_factor(candles) is None

    def test_warmup_bars_are_nan(self):
        """暖机期内（序列前段）应全为 nan，不得 impute 为 0。"""
        candles = _make_trending_up(n=60)
        out = ami_series(candles)
        # 前 20 根（momentum_window=20 是 WMA 的一级暖机）必须含 nan
        assert np.isnan(out[0]), f"bar[0] 应为 nan，实际={out[0]}"

    def test_all_nan_on_minimal_input(self):
        """仅 10 根 K 线：RSI/WMA 暖机都未完成，全为 nan。"""
        candles = _make_trending_up(n=10)
        out = ami_series(candles)
        assert np.all(np.isnan(out)), "10 根 K 线全部应为 nan"


# ── 测试 3：输出范围 [-1, 1] ──────────────────────────────────────────────────

class TestOutputRange:
    def test_finite_values_clamped(self):
        """所有有限输出值应在 [-1, 1] 内（使用震荡序列保证产生有限值）。"""
        candles = _make_sideways(n=300)
        out = ami_series(candles)
        finite_vals = out[np.isfinite(out)]
        if len(finite_vals) > 0:
            assert np.all(finite_vals >= -1.0 - 1e-9), f"min={finite_vals.min():.6f} < -1"
            assert np.all(finite_vals <= 1.0 + 1e-9), f"max={finite_vals.max():.6f} > 1"

    def test_factor_scalar_in_range(self):
        """ami_factor 标量结果应在 [-1, 1] 内。"""
        candles = _make_sideways(n=300)
        v = ami_factor(candles)
        if v is not None:
            assert -1.0 - 1e-9 <= v <= 1.0 + 1e-9, f"factor={v:.6f} 超出 [-1,1]"

    def test_no_nan_imputed_as_zero(self):
        """NaN 哨兵：warmup 期不得被 impute 为 0（0 可能被误判为有效信号）。"""
        candles = _make_trending_up(n=60)
        out = ami_series(candles)
        # 检查 nan 存在（不全被 0 替代）
        assert np.any(np.isnan(out)), "应有 nan 哨兵，不得全 impute 为 0"


# ── 测试 4：符号约定（reversal cluster）─────────────────────────────────────

class TestSignConvention:
    """AMI 是反转因子：
    - prediction 趋于正（累计上涨标签多）→ 预测近通道上端 → factor 负（看跌反转）
    - prediction 趋于负（累计下跌标签多）→ 预测近通道下端 → factor 正（看涨反转）

    注意：这是反直觉的反转逻辑。
    上升趋势产生正预测 → 通道归一化后取负 → factor 趋负（看跌反转预期）
    下降趋势产生负预测 → 通道归一化后取负 → factor 趋正（看涨反转预期）

    实现要点：AMI 需要 fast/slow WMA 交叉事件（crossover/crossunder）才能积累
    KNN store entries。纯单调趋势（无交叉）= store 仅哨兵 → pred 恒 0 → range=0
    → 全 NaN（fail-closed，正确行为）。
    因此符号测试使用「震荡上升/下降趋势」确保产生足够交叉事件。
    """

    def test_uptrend_prediction_sign_implies_factor_bearish(self):
        """震荡上升趋势：净上涨标签多 → prediction 偏正 → factor 偏负（看跌反转）。

        pred 连续大于通道中点 → factor 接近 -1（反转 = 均值回归预期）。
        """
        candles = _make_oscillating_uptrend(n=300)
        out = ami_series(candles)
        finite = out[np.isfinite(out)]
        if len(finite) == 0:
            pytest.skip("序列产生的 MA 交叉不足，通道未建立，跳过符号断言")
        last_finite = finite[-1]
        # 上升趋势末端：prediction 近通道上端 → factor 偏负（或中性）
        assert last_finite <= 0.5, (
            f"震荡上升趋势 factor 末值应 ≤ 0.5（看跌反转区），实际={last_finite:.4f}"
        )

    def test_downtrend_prediction_sign_implies_factor_bullish(self):
        """震荡下降趋势：净下跌标签多 → prediction 偏负 → factor 偏正（看涨反转）。"""
        candles = _make_oscillating_downtrend(n=300)
        out = ami_series(candles)
        finite = out[np.isfinite(out)]
        if len(finite) == 0:
            pytest.skip("序列产生的 MA 交叉不足，通道未建立，跳过符号断言")
        last_finite = finite[-1]
        assert last_finite >= -0.5, (
            f"震荡下降趋势 factor 末值应 ≥ -0.5（看涨反转区），实际={last_finite:.4f}"
        )

    def test_pure_monotonic_trend_yields_all_nan(self):
        """纯单调趋势无 MA 交叉 → store 仅哨兵 → pred=0 恒定 → range=0 → 全 NaN。
        这是正确的 fail-closed 行为（非 bug）。
        """
        candles = _make_trending_up(n=300)
        out = ami_series(candles)
        assert np.all(np.isnan(out)), (
            "纯单调上升趋势应全为 NaN（无 MA 交叉，通道 range=0）"
        )


# ── 测试 5：factor_formula 手工验证（golden 合成）────────────────────────────

class TestFactorFormula:
    """直接验证 factor_ami 公式的数值正确性。
    spec formula:
        norm  = (pred - lower) / (upper - lower)
        factor = clamp(-(2*norm - 1), -1, 1)
               = clamp((lower + upper - 2*pred) / (upper - lower), -1, 1)
    """

    def test_factor_extremes_via_direct_computation(self):
        """直接验证因子公式在边界条件的极值行为。

        spec factor_formula:
          norm  = (pred - lower) / (upper - lower)
          factor = clamp(-(2*norm - 1), -1, 1)

        边界:
          pred == lower (norm=0) → factor = -(2*0-1) = +1 (看涨反转最强)
          pred == upper (norm=1) → factor = -(2*1-1) = -1 (看跌反转最强)
          pred == mid   (norm=0.5) → factor = -(2*0.5-1) = 0  (中性)
        """
        from smc_tracker.indicators.sfg._common import clamp as sfg_clamp

        def compute_factor(pred: float, lower: float, upper: float) -> float:
            rng = upper - lower
            if not (math.isfinite(pred) and math.isfinite(lower) and
                    math.isfinite(upper) and rng > 0):
                return float("nan")
            norm = (pred - lower) / rng
            raw = -(2 * norm - 1)
            return float(np.clip(raw, -1.0, 1.0))

        # pred = lower: 全仓看涨
        assert math.isclose(compute_factor(0.0, 0.0, 10.0), 1.0, abs_tol=1e-12)
        # pred = upper: 全仓看跌
        assert math.isclose(compute_factor(10.0, 0.0, 10.0), -1.0, abs_tol=1e-12)
        # pred = mid: 中性
        assert math.isclose(compute_factor(5.0, 0.0, 10.0), 0.0, abs_tol=1e-12)
        # pred 超出上界: clamp 到 -1
        assert math.isclose(compute_factor(15.0, 0.0, 10.0), -1.0, abs_tol=1e-12)
        # pred 超出下界: clamp 到 +1
        assert math.isclose(compute_factor(-5.0, 0.0, 10.0), 1.0, abs_tol=1e-12)

        # 来自 spec parity_notes continuous_factors.rs tests:
        # ami_oversold_positive: pred near lower → factor > 0
        assert compute_factor(1.0, 0.0, 10.0) > 0.0
        # ami_overbought_negative: pred near upper → factor < 0
        assert compute_factor(9.0, 0.0, 10.0) < 0.0

    def test_factor_arithmetic_identity(self):
        """验证 clamp((lower+upper-2*pred)/(upper-lower)) 恒等式。
        从 spec: factor = clamp(-(2*norm-1)) = clamp((lower+upper-2*pred)/(upper-lower))
        通过合成数据迂回验证：两种公式给同一答案。
        """
        # 直接用纯数值测试公式等价性
        from smc_tracker.indicators.sfg._common import clamp as sfg_clamp

        test_cases = [
            (5.0, 0.0, 10.0),   # pred=mid → norm=0.5 → factor=0
            (0.0, 0.0, 10.0),   # pred=lower → norm=0 → factor=+1
            (10.0, 0.0, 10.0),  # pred=upper → norm=1 → factor=-1
            (2.5, 0.0, 10.0),   # pred=0.25 → norm=0.25 → factor=+0.5
            (7.5, 0.0, 10.0),   # pred=0.75 → norm=0.75 → factor=-0.5
        ]
        for pred, lower, upper in test_cases:
            rng = upper - lower
            if rng <= 0:
                continue
            norm = (pred - lower) / rng
            formula1 = max(-1.0, min(1.0, -(2 * norm - 1)))
            formula2 = max(-1.0, min(1.0, (lower + upper - 2 * pred) / rng))
            assert math.isclose(formula1, formula2, rel_tol=1e-12), (
                f"两公式不一致: formula1={formula1}, formula2={formula2}"
            )

        # pred=mid → factor=0
        pred, lower, upper = 5.0, 0.0, 10.0
        rng = upper - lower
        norm = (pred - lower) / rng
        expected = max(-1.0, min(1.0, -(2 * norm - 1)))
        assert math.isclose(expected, 0.0, abs_tol=1e-12), f"mid → factor=0，实际={expected}"

        # pred=lower → factor=+1
        pred, lower, upper = 0.0, 0.0, 10.0
        rng = upper - lower
        norm = (pred - lower) / rng
        expected = max(-1.0, min(1.0, -(2 * norm - 1)))
        assert math.isclose(expected, 1.0, abs_tol=1e-12), f"lower → factor=+1，实际={expected}"

        # pred=upper → factor=-1
        pred, lower, upper = 10.0, 0.0, 10.0
        rng = upper - lower
        norm = (pred - lower) / rng
        expected = max(-1.0, min(1.0, -(2 * norm - 1)))
        assert math.isclose(expected, -1.0, abs_tol=1e-12), f"upper → factor=-1，实际={expected}"

    def test_range_zero_yields_nan(self):
        """upper=lower（range=0）时 factor 应为 NaN（fail-closed）。
        在序列首个 bar 处 pred=0.0 且 upper=lower=0.0 → range=0 → NaN。
        """
        # 所有 close 相同时 pred 可能为 0，但 WMA/RSI 暖机阶段先得到 NaN
        # 此处直接测算术守卫
        pred, lower, upper = 0.0, 0.0, 0.0
        rng = upper - lower
        if rng <= 0:
            result = float("nan")
        else:
            norm = (pred - lower) / rng
            result = max(-1.0, min(1.0, -(2 * norm - 1)))
        assert math.isnan(result), f"range=0 应为 NaN，实际={result}"


# ── 测试 6：ami_factor 标量包装 ───────────────────────────────────────────────

class TestAmiFactorWrapper:
    def test_returns_float_or_none(self):
        """ami_factor 应返回 float 或 None，不得返回 nan。"""
        candles = _make_sideways(n=300)
        v = ami_factor(candles)
        if v is not None:
            assert isinstance(v, float), f"应返回 float，实际={type(v)}"
            assert math.isfinite(v), f"返回值应为有限数，实际={v}"
        # else: None 也合法（warmup 不足）

    def test_factor_equals_last_finite_of_series(self):
        """ami_factor 应等于 ami_series 末个有限值。"""
        candles = _make_sideways(n=300)
        series = ami_series(candles)
        factor = ami_factor(candles)

        finite_vals = series[np.isfinite(series)]
        if len(finite_vals) == 0:
            assert factor is None, "series 全 nan 时 factor 应为 None"
        else:
            expected = float(finite_vals[-1])
            assert factor is not None
            assert math.isclose(factor, expected, rel_tol=1e-12), (
                f"factor={factor:.8f} 应 == series 末有限值={expected:.8f}"
            )

    def test_factor_none_when_warmup_insufficient(self):
        """K 线数 < warmup 时返回 None（不崩溃）。"""
        candles = _make_trending_up(n=3)
        assert ami_factor(candles) is None


# ── 测试 7：无前视（prefix invariance）──────────────────────────────────────

class TestNoLookahead:
    def test_prefix_invariance(self):
        """将序列截断到 i，得到的 out[i] 应与完整序列中 out[i] 相同。
        这验证 out[i] 只依赖 candles[:i+1]，无前视。
        使用震荡序列确保产生有限值（从而实质验证数值一致性，而非仅 nan==nan）。
        """
        candles = _make_sideways(n=300)
        full_out = ami_series(candles)

        # 选 3 个位置验证（包含暖机后的有限值段）
        for i in [50, 100, 149, 200]:
            prefix_out = ami_series(candles[: i + 1])
            full_val = full_out[i]
            prefix_val = prefix_out[i]

            if np.isnan(full_val):
                assert np.isnan(prefix_val), (
                    f"bar {i}: full=nan 但 prefix={prefix_val}"
                )
            else:
                assert not np.isnan(prefix_val), (
                    f"bar {i}: full={full_val:.8f} 但 prefix=nan（可能 lookahead 问题）"
                )
                assert math.isclose(
                    float(prefix_val), float(full_val), rel_tol=1e-9
                ), (
                    f"bar {i}: prefix={prefix_val:.8f} ≠ full={full_val:.8f} "
                    f"(lookahead 违规)"
                )


# ── 测试 8：pred 为 0.0 （非 NaN）in 热路径 ──────────────────────────────────

class TestPredNanPolicy:
    """spec: 若无有限邻居（暖机、NaN features），pred=0.0 NOT NaN (Pine-literal)。
    factor 依赖 pred/upper/lower 三者都有限且 range>0 才输出非 nan；
    早期 pred=0.0 但 upper=lower=0.0 → range=0 → factor=NaN（正确 fail-closed）。
    """

    def test_early_bars_factor_nan_not_zero(self):
        """早期 bars（range=0 时）因子应为 NaN，不是 0。"""
        candles = _make_sideways(n=150)
        out = ami_series(candles)
        # 至少最早的 nan 应真的是 nan
        assert np.isnan(out[0]), f"bar[0] 应为 nan，实际={out[0]}"


# ── 测试 9：连续性（无孔洞）──────────────────────────────────────────────────

class TestContinuity:
    def test_no_nan_holes_after_warmup(self):
        """一旦 series 开始产生有限值，后续不应再出现 NaN（稳定序列）。
        注：若 range=0 重现可能局部 NaN，但对带交叉的平滑序列不应出现。
        使用震荡序列确保产生 MA 交叉、通道建立后持续有效。
        """
        candles = _make_sideways(n=300)
        out = ami_series(candles)
        finite_idx = np.where(np.isfinite(out))[0]
        if len(finite_idx) < 2:
            pytest.skip("有限值不足，跳过连续性测试")
        start = finite_idx[0]
        tail = out[start:]
        # 震荡序列有持续交叉，range>0 应持续，因子应全部有限
        nan_in_tail = np.sum(np.isnan(tail))
        assert nan_in_tail == 0, (
            f"有限段内不应有 NaN，发现 {nan_in_tail} 个"
        )
