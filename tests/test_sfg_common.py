"""tests/test_sfg_common.py — SFG 公共基座原语确定性 golden 测试。

TDD 先行，全部合成数据，无网络/随机依赖。
验证：trailing-only（无前视）、前缀不变性（no-repaint 金标准）、数值精度。
"""
from __future__ import annotations

import math
import numpy as np
import pytest

from smc_tracker.indicators.sfg._common import (
    clamp,
    level_factor,
    first_obs_ema,
    sma_series,
    wma_series,
    hma_series,
    rolling_max_series,
    rolling_min_series,
    pivot_high_series,
    pivot_low_series,
    forward_fill,
    ohlcv_arrays,
)


# ── 辅助：构造最小 Candle 对象 ────────────────────────────────────────────────

class _C:
    """轻量 Candle —— 属性 .o/.h/.l/.c/.v，匹配 ohlcv_arrays 约定。"""
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self, o: float, h: float, lo: float, c: float, v: float) -> None:
        self.o = o; self.h = h; self.l = lo; self.c = c; self.v = v


# ─────────────────────────────────────────────────────────────────────────────
# 1. clamp
# ─────────────────────────────────────────────────────────────────────────────

class TestClamp:
    def test_within_range(self):
        a = clamp(np.array([0.5, -0.3, 0.0]))
        assert np.allclose(a, [0.5, -0.3, 0.0])

    def test_above_one(self):
        a = clamp(np.array([2.0, 1.0, 1.5]))
        assert np.allclose(a, [1.0, 1.0, 1.0])

    def test_below_neg_one(self):
        a = clamp(np.array([-2.0, -1.0, -3.0]))
        assert np.allclose(a, [-1.0, -1.0, -1.0])

    def test_nan_inf_become_nan(self):
        a = clamp(np.array([np.nan, np.inf, -np.inf, 0.5]))
        assert np.isnan(a[0])
        assert np.isnan(a[1])
        assert np.isnan(a[2])
        assert math.isclose(a[3], 0.5)

    def test_empty(self):
        a = clamp(np.array([]))
        assert len(a) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. level_factor
# ─────────────────────────────────────────────────────────────────────────────

class TestLevelFactor:
    """6 个反转因子共享内核：(mid - close) / half_range → clamp[-1,1]。"""

    def _lf(self, close: float, lower: float = 95.0, upper: float = 105.0) -> float:
        c = np.array([close])
        lo = np.array([lower])
        hi = np.array([upper])
        return float(level_factor(c, lo, hi)[0])

    def test_at_lower_bound_plus_one(self):
        """close = lower → +1（价格在支撑位 = 强看涨反转信号）。"""
        v = self._lf(95.0)
        assert math.isclose(v, 1.0, rel_tol=1e-9), f"expected 1.0 got {v}"

    def test_at_upper_bound_minus_one(self):
        """close = upper → -1（价格在压力位 = 强看跌反转信号）。"""
        v = self._lf(105.0)
        assert math.isclose(v, -1.0, rel_tol=1e-9), f"expected -1.0 got {v}"

    def test_at_midpoint_zero(self):
        """close = mid → 0（价格在中点 = 中性）。"""
        v = self._lf(100.0)
        assert math.isclose(v, 0.0, abs_tol=1e-12), f"expected 0 got {v}"

    def test_clamped_above_range(self):
        """close = 200 >> upper → 输出 clamp 到 -1（不超出 [-1,1]）。"""
        v = self._lf(200.0)
        assert math.isclose(v, -1.0, rel_tol=1e-9), f"expected -1.0 got {v}"

    def test_half_range_zero_nan(self):
        """lower == upper → half_range=0 → 返回 nan（去掉退化情形，无噪信号）。"""
        v = self._lf(100.0, lower=100.0, upper=100.0)
        assert math.isnan(v), f"expected nan got {v}"

    def test_nonfinite_close_nan(self):
        c = np.array([np.nan])
        result = level_factor(c, np.array([95.0]), np.array([105.0]))
        assert math.isnan(result[0])

    def test_nonfinite_bounds_nan(self):
        c = np.array([100.0])
        result = level_factor(c, np.array([np.inf]), np.array([105.0]))
        assert math.isnan(result[0])

    def test_vectorized_shape(self):
        """level_factor 输出 shape 与输入相同。"""
        n = 5
        c = np.array([95.0, 97.0, 100.0, 103.0, 105.0])
        lo = np.full(n, 95.0)
        hi = np.full(n, 105.0)
        out = level_factor(c, lo, hi)
        assert out.shape == (n,)


# ─────────────────────────────────────────────────────────────────────────────
# 3. first_obs_ema
# ─────────────────────────────────────────────────────────────────────────────

class TestFirstObsEma:
    def test_seed_at_first_finite(self):
        """首个有限值作种子，之后 EMA 递推（GPI 需要）。"""
        arr = np.array([np.nan, np.nan, 10.0, 12.0, 11.0])
        out = first_obs_ema(arr, span=3)
        # 前两个 NaN 输出 NaN
        assert np.isnan(out[0]) and np.isnan(out[1])
        # 索引 2：种子 = 10.0
        assert math.isclose(out[2], 10.0, rel_tol=1e-9)
        # 之后正常 EMA 递推
        a = 2.0 / (3 + 1)
        expected_3 = out[2] * (1 - a) + 12.0 * a
        assert math.isclose(out[3], expected_3, rel_tol=1e-9)

    def test_nan_carry_forward(self):
        """输入中间出现 NaN → 保持上一个有限 EMA 值（carry-forward）。"""
        arr = np.array([5.0, np.nan, np.nan, 5.0])
        out = first_obs_ema(arr, span=2)
        # 索引 0 seed = 5.0
        assert math.isclose(out[0], 5.0)
        # 索引 1,2 输入 NaN → carry-forward
        assert math.isclose(out[1], out[0])
        assert math.isclose(out[2], out[1])

    def test_all_nan_stays_nan(self):
        arr = np.array([np.nan, np.nan])
        out = first_obs_ema(arr, span=3)
        assert np.all(np.isnan(out))

    def test_single_element(self):
        arr = np.array([7.0])
        out = first_obs_ema(arr, span=5)
        assert math.isclose(out[0], 7.0)

    def test_span_min_one(self):
        """span<=0 → 强制 span=1 → alpha=1 → EMA = 最新值。"""
        arr = np.array([3.0, 5.0, 7.0])
        out = first_obs_ema(arr, span=0)
        assert math.isclose(out[2], 7.0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. sma_series
# ─────────────────────────────────────────────────────────────────────────────

class TestSmaSeries:
    def test_golden(self):
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        out = sma_series(x, 3)
        assert np.isnan(out[0]) and np.isnan(out[1])
        assert math.isclose(out[2], 2.0)
        assert math.isclose(out[3], 3.0)
        assert math.isclose(out[4], 4.0)

    def test_trailing_not_centered(self):
        """SMA(n=3) 的 out[2] 用 x[0..2]，而非居中窗口 x[1..3]。"""
        x = np.array([10.0, 20.0, 30.0, 40.0])
        out = sma_series(x, 3)
        # trailing: out[2] = mean(10,20,30) = 20
        assert math.isclose(out[2], 20.0)

    def test_warmup_nan(self):
        out = sma_series(np.array([1.0, 2.0]), 5)
        assert np.all(np.isnan(out))


# ─────────────────────────────────────────────────────────────────────────────
# 5. wma_series — no-lookahead 金标准测试
# ─────────────────────────────────────────────────────────────────────────────

class TestWmaSeries:
    def test_golden_n3(self):
        """wma_series([1,2,3,4], 3) golden value（手算）。

        WMA(3) trailing weights: 1,2,3（最新权最大）
        out[2] = (1·1 + 2·2 + 3·3)/(1+2+3) = 14/6 = 2.333...
        out[3] = (1·2 + 2·3 + 3·4)/(1+2+3) = 20/6 = 3.333...
        """
        x = np.array([1.0, 2.0, 3.0, 4.0])
        out = wma_series(x, 3)
        assert np.isnan(out[0]) and np.isnan(out[1])
        assert math.isclose(out[2], 14.0 / 6.0, rel_tol=1e-9), f"got {out[2]}"
        assert math.isclose(out[3], 20.0 / 6.0, rel_tol=1e-9), f"got {out[3]}"

    def test_trailing_not_centered(self):
        """核心 no-lookahead 验证：out[i] 只用 x[i-n+1..i]，不读 x[i+1]。

        若 out[2] = wma(x[0..2]) ≠ wma(x[1..3])，说明真正 trailing。
        """
        x = np.array([1.0, 2.0, 3.0, 4.0])
        out = wma_series(x, 3)
        # trailing out[2] 用 [1,2,3]，居中会用 [2,3,4]
        centered_would_be = (1.0 * 2.0 + 2.0 * 3.0 + 3.0 * 4.0) / 6.0  # 20/6
        assert not math.isclose(out[2], centered_would_be, rel_tol=1e-6), (
            f"out[2]={out[2]} 不应等于居中值 {centered_would_be}"
        )

    def test_n1_identity(self):
        x = np.array([3.0, 7.0, 2.0])
        out = wma_series(x, 1)
        assert np.allclose(out, x)

    def test_warmup_nan(self):
        out = wma_series(np.arange(3.0), 5)
        assert np.all(np.isnan(out))


# ─────────────────────────────────────────────────────────────────────────────
# 6. hma_series
# ─────────────────────────────────────────────────────────────────────────────

class TestHmaSeries:
    def test_shape_and_finite_tail(self):
        """hma_series 输出等长，末尾应有有限值（足够数据时）。"""
        x = np.arange(50.0)
        out = hma_series(x, 9)
        assert out.shape == x.shape
        # 末尾几个值应有限
        assert np.isfinite(out[-1]), f"末尾应有有限值，got {out[-1]}"

    def test_warmup_nan_prefix(self):
        """前几个值（暖机期）应为 nan。"""
        x = np.arange(50.0)
        out = hma_series(x, 9)
        assert np.isnan(out[0]), "首个值应 nan（暖机）"

    def test_small_input_all_nan(self):
        """数据不足时全部 nan，不崩溃。"""
        out = hma_series(np.array([1.0, 2.0]), 9)
        assert np.all(np.isnan(out))

    def test_linear_trend_direction(self):
        """完美线性上升趋势 → HMA 末值应大于中间值（跟随趋势）。"""
        x = np.arange(60.0)
        out = hma_series(x, 9)
        finite = out[np.isfinite(out)]
        assert len(finite) >= 2
        # HMA 在完美上升趋势应单调增（至少末值 > 首个有限值）
        assert finite[-1] > finite[0]


# ─────────────────────────────────────────────────────────────────────────────
# 7. rolling_max_series / rolling_min_series
# ─────────────────────────────────────────────────────────────────────────────

class TestRollingMaxMin:
    def test_rolling_max_golden(self):
        x = np.array([1.0, 3.0, 2.0, 5.0, 4.0])
        out = rolling_max_series(x, 3)
        assert np.isnan(out[0]) and np.isnan(out[1])
        assert math.isclose(out[2], 3.0)  # max(1,3,2)
        assert math.isclose(out[3], 5.0)  # max(3,2,5)
        assert math.isclose(out[4], 5.0)  # max(2,5,4)

    def test_rolling_min_golden(self):
        x = np.array([5.0, 3.0, 4.0, 1.0, 2.0])
        out = rolling_min_series(x, 3)
        assert np.isnan(out[0]) and np.isnan(out[1])
        assert math.isclose(out[2], 3.0)  # min(5,3,4)
        assert math.isclose(out[3], 1.0)  # min(3,4,1)
        assert math.isclose(out[4], 1.0)  # min(4,1,2)

    def test_min_periods_1(self):
        """min_periods=1 → 首个即有值（单元素窗口）。"""
        x = np.array([7.0, 3.0, 9.0])
        out = rolling_max_series(x, 3, min_periods=1)
        assert math.isclose(out[0], 7.0)
        assert math.isclose(out[1], 7.0)
        assert math.isclose(out[2], 9.0)

    def test_trailing_not_future(self):
        """out[i] 不读 x[i+1]：out[1] 的 max 不应包含 x[2]。"""
        x = np.array([1.0, 2.0, 100.0])
        out = rolling_max_series(x, 2)
        # out[1] = max(x[0],x[1]) = 2, 不应为 100
        assert math.isclose(out[1], 2.0)


# ─────────────────────────────────────────────────────────────────────────────
# 8. pivot_high_series / pivot_low_series
# ─────────────────────────────────────────────────────────────────────────────

class TestPivotHighSeries:
    """关键 no-lookahead / no-repaint 测试。"""

    def _make_pivot_high(self, left: int = 2, right: int = 2) -> tuple[np.ndarray, int]:
        """构造包含中心高点的价格序列（center bar = 索引 center）。

        序列: [10, 12, 20, 14, 11]  center=2 (left=2,right=2)
        pivot_high 要求左边 left 根严格低于中心，右边 right 根严格低于中心。
        发射延迟：结果在 center+right 之后才可知。
        """
        prices = np.array([10.0, 12.0, 20.0, 14.0, 11.0])
        center = 2
        return prices, center

    def test_confirmation_at_center_plus_right(self):
        """中心高点 pivot 必须等到 center+right 才发射，之前均为 nan。"""
        left, right = 2, 2
        prices, center = self._make_pivot_high(left, right)
        out = pivot_high_series(prices, left=left, right=right)
        # center=2, right=2 → 发射位置 = center+right = 4
        emit_idx = center + right
        # [0, 1, 2, 3] 均应 nan
        for i in range(emit_idx):
            assert np.isnan(out[i]), f"out[{i}] 应 nan，实际={out[i]}"
        # out[4] 应非 nan（已发射 pivot）
        assert np.isfinite(out[emit_idx]), f"out[{emit_idx}] 应有效，实际={out[emit_idx]}"
        assert math.isclose(out[emit_idx], 20.0), f"pivot 高点应为 20，实际={out[emit_idx]}"

    def test_no_repaint_prefix_invariance(self):
        """前缀不变性（no-repaint 金标准）：序列末尾追加更高 bar 后，早期 pivot 值不变。

        若实现有前视（look-ahead），追加数据会影响早期结果。
        """
        left, right = 2, 2
        prices_orig, center = self._make_pivot_high(left, right)
        out_orig = pivot_high_series(prices_orig, left=left, right=right)

        # 追加一个更高的 bar（破坏 pivot bar 后的右边 right 约束 → 可能抑制或不影响已发射的 pivot）
        prices_extended = np.append(prices_orig, 25.0)
        out_ext = pivot_high_series(prices_extended, left=left, right=right)

        # 已在 prices_orig 上发射（emit_idx=4）的 pivot 值在 out_ext 的对应位置应保持不变
        emit_idx = center + right  # = 4
        assert math.isclose(out_orig[emit_idx], out_ext[emit_idx], rel_tol=1e-9), (
            f"追加数据后 out[{emit_idx}] 应保持 {out_orig[emit_idx]}，"
            f"实际变为 {out_ext[emit_idx]}（发生 repaint！）"
        )

    def test_no_pivot_flat(self):
        """无中心极值的序列 → 全 nan。"""
        prices = np.array([1.0, 2.0, 3.0, 4.0, 5.0])  # 单调上升，无 pivot high
        out = pivot_high_series(prices, left=2, right=2)
        # 最多只有最后一个有效 emit，但单调升无 pivot high
        # 验证全为 nan 或没有错误的 pivot
        # 实际上严格不等条件：中心必须严格大于左右 → 单调升无 pivot
        assert np.all(np.isnan(out)), f"单调升无 pivot high，out={out}"

    def test_short_sequence_all_nan(self):
        """序列太短（< left+right+1）→ 全 nan，不崩溃。"""
        out = pivot_high_series(np.array([5.0, 3.0]), left=2, right=2)
        assert np.all(np.isnan(out))


class TestPivotLowSeries:
    def test_confirmation_at_center_plus_right(self):
        """低点 pivot：中心最低，left 根和 right 根均严格高于中心。"""
        prices = np.array([20.0, 15.0, 5.0, 12.0, 18.0])
        # center=2, left=2, right=2
        out = pivot_low_series(prices, left=2, right=2)
        # 发射位置 = 2+2 = 4
        assert np.isfinite(out[4]), f"out[4] 应有有效 pivot low，实际={out[4]}"
        assert math.isclose(out[4], 5.0), f"pivot low 应为 5，实际={out[4]}"
        # 之前全 nan
        for i in range(4):
            assert np.isnan(out[i])

    def test_no_repaint_low(self):
        """prefix-invariance 同样适用于 pivot_low_series。"""
        left, right = 2, 2
        prices_orig = np.array([20.0, 15.0, 5.0, 12.0, 18.0])
        out_orig = pivot_low_series(prices_orig, left=left, right=right)

        prices_ext = np.append(prices_orig, 1.0)  # 追加更低 bar
        out_ext = pivot_low_series(prices_ext, left=left, right=right)

        emit_idx = 4
        assert math.isclose(out_orig[emit_idx], out_ext[emit_idx], rel_tol=1e-9), (
            f"追加数据后 pivot low 值改变（repaint）：{out_orig[emit_idx]} → {out_ext[emit_idx]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9. forward_fill
# ─────────────────────────────────────────────────────────────────────────────

class TestForwardFill:
    def test_basic(self):
        arr = np.array([1.0, np.nan, np.nan, 4.0, np.nan])
        out = forward_fill(arr)
        assert math.isclose(out[0], 1.0)
        assert math.isclose(out[1], 1.0)
        assert math.isclose(out[2], 1.0)
        assert math.isclose(out[3], 4.0)
        assert math.isclose(out[4], 4.0)

    def test_leading_nan_preserved(self):
        """前导 nan（无历史可填）→ 保持 nan。"""
        arr = np.array([np.nan, np.nan, 3.0])
        out = forward_fill(arr)
        assert np.isnan(out[0])
        assert np.isnan(out[1])
        assert math.isclose(out[2], 3.0)

    def test_all_finite_unchanged(self):
        arr = np.array([1.0, 2.0, 3.0])
        out = forward_fill(arr)
        assert np.allclose(out, arr)

    def test_all_nan_stays_nan(self):
        arr = np.array([np.nan, np.nan])
        out = forward_fill(arr)
        assert np.all(np.isnan(out))

    def test_no_lookahead(self):
        """forward_fill 只能向后填，不能用未来值填过去的 nan。"""
        arr = np.array([np.nan, 5.0])
        out = forward_fill(arr)
        assert np.isnan(out[0]), f"前导 nan 不应被 5.0 向前填充，got {out[0]}"


# ─────────────────────────────────────────────────────────────────────────────
# 10. ohlcv_arrays
# ─────────────────────────────────────────────────────────────────────────────

class TestOhlcvArrays:
    def test_basic_fields(self):
        candles = [_C(1.0, 2.0, 0.5, 1.5, 1000.0), _C(1.5, 2.5, 1.0, 2.0, 500.0)]
        arrs = ohlcv_arrays(candles)
        assert set(arrs.keys()) == {"o", "h", "l", "c", "v"}
        assert np.allclose(arrs["o"], [1.0, 1.5])
        assert np.allclose(arrs["h"], [2.0, 2.5])
        assert np.allclose(arrs["l"], [0.5, 1.0])
        assert np.allclose(arrs["c"], [1.5, 2.0])
        assert np.allclose(arrs["v"], [1000.0, 500.0])

    def test_nan_on_non_finite(self):
        """非有限字段值 → fail-closed → NaN（SFG 语义：缺数据诚实弃权，不伪造 0.0）。"""
        candles = [_C(float("nan"), float("inf"), 0.5, 1.5, 1000.0)]
        arrs = ohlcv_arrays(candles)
        # fail-closed：to_float(nan, default=nan)=nan，to_float(inf, default=nan)=nan
        assert math.isnan(arrs["o"][0])
        assert math.isnan(arrs["h"][0])
        # 有限字段不受影响
        assert math.isclose(arrs["l"][0], 0.5)
        assert math.isclose(arrs["c"][0], 1.5)

    def test_empty_candles(self):
        arrs = ohlcv_arrays([])
        for k in ("o", "h", "l", "c", "v"):
            assert arrs[k].shape == (0,)
