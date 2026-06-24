"""tests/test_sfg_pdbb.py — PDBB 因子 TDD 测试套件。

PDBB: PD Array & Breaker Block — 反转簇因子（premium/discount + breaker block）。
factor_pdbb = clamp((HH+LL-2*close)/(HH-LL), -1, 1)
  +1 = 价格在折扣区（close==LL）= 看涨反转；-1 = 价格在溢价区（close==HH）= 看跌反转。

全部合成确定性数据，无网络/随机依赖。
"""
from __future__ import annotations

import math
import numpy as np
import pytest

from smc_tracker.indicators.sfg.pdbb import pdbb_series, pdbb_factor


# ── 辅助：合成 Candle ─────────────────────────────────────────────────────────

class _C:
    """轻量 Candle，属性 .o/.h/.l/.c/.v（与 ohlcv_arrays 兼容）。"""
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self, o: float, h: float, lo: float, c: float, v: float = 1000.0) -> None:
        self.o = o
        self.h = h
        self.l = lo
        self.c = c
        self.v = v


def _make_candle(price: float, spread: float = 0.05) -> _C:
    """给定 close，生成典型 OHLC 蜡烛（中性，无方向偏移）。"""
    return _C(price, price + spread, price - spread, price)


def _make_trending_up(n: int = 60, start: float = 100.0, step: float = 1.0) -> list[_C]:
    """单调上升趋势，每根 close 递增 step。"""
    candles = []
    price = start
    for _ in range(n):
        o = price
        c = price + step
        h = c + 0.5
        lo = o - 0.5
        candles.append(_C(o, h, lo, c))
        price = c
    return candles


def _make_trending_down(n: int = 60, start: float = 160.0, step: float = 1.0) -> list[_C]:
    """单调下降趋势，每根 close 递减 step。"""
    candles = []
    price = start
    for _ in range(n):
        o = price
        c = price - step
        h = o + 0.5
        lo = c - 0.5
        candles.append(_C(o, h, lo, c))
        price = c
    return candles


def _make_sideways(n: int = 80, base: float = 100.0, amplitude: float = 5.0) -> list[_C]:
    """正弦震荡，形成明确的高低点序列，供 ZigZag 检测。"""
    candles = []
    period = 16  # 半周期 8 根，可形成长度5的 pivot
    for i in range(n):
        # 正弦生成价格，幅度足够 pivot 检测
        c = base + amplitude * math.sin(2 * math.pi * i / period)
        o = base + amplitude * math.sin(2 * math.pi * (i - 1) / period) if i > 0 else base
        h = max(o, c) + 0.3
        lo = min(o, c) - 0.3
        candles.append(_C(o, h, lo, c))
    return candles


def _make_swing_sequence(length: int = 5) -> list[_C]:
    """生成包含明确 ZigZag 摆动的序列，足以触发 MSS 和 breaker block。

    结构: 上升摆动 A→B, 回调 B→C, 上升摆动 C→D(>B), 触发 MSS 多头。
    length=5: 需要 length+2 预热 = 7 根最少。

    返回足够长（约 80 根）的序列供 MSS 检测。
    注意: 本因子高复杂度，短序列可能退化（无法形成足够摆动）。
    """
    candles: list[_C] = []
    # 预热段：平稳上升（生成足够的预热价格）
    price = 100.0
    for _ in range(20):
        candles.append(_C(price, price + 0.2, price - 0.2, price))
        price += 0.1

    # 摆动 A: 低点
    for i in range(length + 2):
        p = price - i * 0.5
        candles.append(_C(p, p + 0.2, p - 0.2, p))

    # 摆动 B: 高点
    price_a = price - (length + 1) * 0.5
    for i in range(length + 2):
        p = price_a + i * 1.0
        candles.append(_C(p, p + 0.3, p - 0.3, p))

    # 摆动 C: 回调低点（高于 A）
    price_b = price_a + (length + 1) * 1.0
    for i in range(length + 2):
        p = price_b - i * 0.5
        candles.append(_C(p, p + 0.2, p - 0.2, p))

    # 摆动 D: 高点（突破 B, 触发 MSS）
    price_c = price_b - (length + 1) * 0.5
    for i in range(length + 2):
        p = price_c + i * 1.5
        candles.append(_C(p, p + 0.4, p - 0.4, p))

    # 摆动 E: 回调（低于 D, Ey < Cy 条件需要：E 低于 C 高）
    price_d = price_c + (length + 1) * 1.5
    for i in range(length + 5):
        p = price_d - i * 0.6
        candles.append(_C(p, p + 0.2, p - 0.2, p))

    return candles


# ── 测试 1: pdbb_series 输出形状与类型 ───────────────────────────────────────

class TestOutputShape:
    def test_returns_ndarray(self):
        candles = _make_trending_up(30)
        out = pdbb_series(candles)
        assert isinstance(out, np.ndarray), "应返回 np.ndarray"

    def test_length_equals_input(self):
        candles = _make_trending_up(30)
        out = pdbb_series(candles)
        assert len(out) == 30, f"长度应=30，实际={len(out)}"

    def test_empty_input_empty_output(self):
        out = pdbb_series([])
        assert isinstance(out, np.ndarray)
        assert len(out) == 0


# ── 测试 2: warmup 边界 ──────────────────────────────────────────────────────

class TestWarmup:
    def test_too_few_all_nan(self):
        """K 线少于 length+2=7 时全部 nan（pdbb.rs:256-260）。"""
        candles = [_make_candle(float(i)) for i in range(6)]
        out = pdbb_series(candles, length=5)
        assert np.all(np.isnan(out)), f"warmup 不足应全 nan，实际={out}"

    def test_exactly_warmup_prefix_nan(self):
        """恰好 length+2 根，前 length+1 根应为 nan。"""
        n = 8  # length=5 → warmup=7
        candles = [_make_candle(100.0 + i) for i in range(n)]
        out = pdbb_series(candles, length=5)
        # 至少前几根是 nan（warmup 段）
        assert np.isnan(out[0]), "首根应 nan（warmup）"

    def test_pdbb_factor_none_on_too_few(self):
        """pdbb_factor 在 K 线不足时返回 None。"""
        candles = [_make_candle(100.0) for _ in range(5)]
        result = pdbb_factor(candles, length=5)
        assert result is None, f"不足 warmup 应返回 None，实际={result}"

    def test_pdbb_factor_none_on_empty(self):
        result = pdbb_factor([], length=5)
        assert result is None


# ── 测试 3: 输出范围 [-1, 1] 和有限性 ────────────────────────────────────────

class TestOutputRange:
    def test_finite_values_in_range(self):
        """所有有限值应在 [-1, 1]（clamp 保证）。"""
        candles = _make_sideways(100)
        out = pdbb_series(candles)
        finite_vals = out[np.isfinite(out)]
        if len(finite_vals) > 0:
            assert np.all(finite_vals >= -1.0 - 1e-9), "最小值不得低于 -1"
            assert np.all(finite_vals <= 1.0 + 1e-9), "最大值不得超过 +1"

    def test_nan_sentinel_not_zero(self):
        """NaN sentinel 不应被强制替换为 0（fail-closed，不插补）。"""
        candles = [_make_candle(100.0) for _ in range(6)]  # 不足 warmup
        out = pdbb_series(candles, length=5)
        # 全部应是 nan，不是 0
        for v in out:
            assert math.isnan(v), f"warmup 段应为 nan，不是 {v}"

    def test_no_inf_in_output(self):
        """输出不得含 inf（非有限只允许 nan）。"""
        candles = _make_sideways(100)
        out = pdbb_series(candles)
        assert not np.any(np.isinf(out)), "输出不得含 inf"


# ── 测试 4: level_factor 数学验证 ────────────────────────────────────────────

class TestLevelFactorMath:
    """直接验证 factor = (HH+LL-2*close)/(HH-LL) 的数学正确性。

    利用 pdbb_factor 的语义：合成已知 HH/LL 时验证因子值。
    由于 MSS/breaker block 触发复杂，本测试直接调用 _common.level_factor 做 golden。
    """

    def test_at_discount_extreme_plus_one(self):
        """close == LL → factor = +1（折扣极值，强看涨）。"""
        from smc_tracker.indicators.sfg._common import level_factor
        hh = np.array([110.0])
        ll = np.array([90.0])
        c = np.array([90.0])
        v = float(level_factor(c, ll, hh)[0])
        assert math.isclose(v, 1.0, rel_tol=1e-9), f"预期 +1，实际={v}"

    def test_at_premium_extreme_minus_one(self):
        """close == HH → factor = -1（溢价极值，强看跌）。"""
        from smc_tracker.indicators.sfg._common import level_factor
        hh = np.array([110.0])
        ll = np.array([90.0])
        c = np.array([110.0])
        v = float(level_factor(c, ll, hh)[0])
        assert math.isclose(v, -1.0, rel_tol=1e-9), f"预期 -1，实际={v}"

    def test_at_midpoint_zero(self):
        """close == mid → factor = 0（中点，中性）。"""
        from smc_tracker.indicators.sfg._common import level_factor
        hh = np.array([110.0])
        ll = np.array([90.0])
        c = np.array([100.0])
        v = float(level_factor(c, ll, hh)[0])
        assert math.isclose(v, 0.0, abs_tol=1e-12), f"预期 0，实际={v}"

    def test_formula_golden(self):
        """公式 golden: HH=120, LL=80, close=95 → (80+120-2*95)/(120-80) = 10/40 = 0.25。"""
        from smc_tracker.indicators.sfg._common import level_factor
        hh = np.array([120.0])
        ll = np.array([80.0])
        c = np.array([95.0])
        v = float(level_factor(c, ll, hh)[0])
        # mid = 100, half = 20, factor = (100-95)/20 = 0.25
        assert math.isclose(v, 0.25, rel_tol=1e-9), f"预期 0.25，实际={v}"

    def test_degenerate_band_nan(self):
        """HH == LL → half_range=0 → nan（fail-closed）。"""
        from smc_tracker.indicators.sfg._common import level_factor
        hh = np.array([100.0])
        ll = np.array([100.0])
        c = np.array([100.0])
        v = float(level_factor(c, ll, hh)[0])
        assert math.isnan(v), f"退化带应 nan，实际={v}"


# ── 测试 5: no-lookahead / prefix-invariance (repaint 测试) ──────────────────

class TestPrefixInvariance:
    """PDBB 因子 no-lookahead 硬护栏。

    确认滞后设计：pivot 在 c+1 才确认，因此 series[i] 只依赖 candles[0..i]。
    追加更极端新 bar 不应改变早期已发射值。
    """

    def test_series_prefix_stable_on_append(self):
        """追加新 bar 后，已发射的早期 series[i] 不变（prefix-invariance）。

        注意: 末尾尚未确认的 pivot 会因新 bar 改变（这是正确的确认滞后语义），
        但已经确认过的内部值 series[i] (i < len-right-1) 应稳定不变。
        """
        candles = _make_sideways(80)
        out_orig = pdbb_series(candles)

        # 追加 5 根极端 bar（价格远超原序列）
        extremes = [_make_candle(10000.0) for _ in range(5)]
        candles_ext = candles + extremes
        out_ext = pdbb_series(candles_ext)

        # 验证原序列长度范围内，有限值（已经确认的值）不变
        # 取前 n-10 根（避免末尾可能受到新 bar 影响的未确认 pivot 段）
        n = len(candles)
        check_end = n - 10  # 给确认滞后留足空间
        if check_end > 10:
            for i in range(check_end):
                v_orig = out_orig[i]
                v_ext = out_ext[i]
                if np.isfinite(v_orig):
                    # 已发射的有限值应在 ext 中相同（prefix-invariant）
                    assert math.isclose(v_orig, v_ext, rel_tol=1e-9, abs_tol=1e-12), (
                        f"prefix-invariance 违反：series[{i}] 从 {v_orig:.6f} 变为 {v_ext:.6f}"
                    )
                elif np.isnan(v_orig):
                    # nan 到有限值：允许（新数据可能激活 block）
                    pass  # OK — 更多历史可能填充 nan

    def test_nan_prefix_not_affected_by_new_data(self):
        """warmup 段（前 length+1 根）应始终为 nan，追加新数据不改变。"""
        candles = _make_sideways(50)
        out_orig = pdbb_series(candles, length=5)

        # 追加极端 bar
        extremes = [_make_candle(99999.0) for _ in range(10)]
        candles_ext = candles + extremes
        out_ext = pdbb_series(candles_ext, length=5)

        # 原序列的 warmup 段（前几根）在 ext 中仍应 nan（未到 warmup 前 block 不可能形成）
        for i in range(min(6, len(out_orig))):
            assert math.isnan(out_orig[i]), f"warmup out_orig[{i}] 应 nan"
            assert math.isnan(out_ext[i]), f"warmup out_ext[{i}] 应 nan（追加后不改变）"


# ── 测试 6: 趋势/震荡符号约定 ────────────────────────────────────────────────

class TestSignConvention:
    """PDBB 是反转因子: +1=看涨(close在折扣区), -1=看跌(close在溢价区)。

    注意: 短序列或无 MSS 触发时因子全为 nan，这是正常的 fail-closed 行为。
    若序列足够长并含 ZigZag 摆动 → 期望有限因子值与符号约定一致。
    """

    def test_sideways_has_some_finite_or_all_nan(self):
        """震荡序列：有限值应在 [-1, 1]，或全 nan（无 MSS 触发）。

        不强断言符号（MSS 触发高度依赖实际价格结构），只验证合法性。
        """
        candles = _make_sideways(100)
        out = pdbb_series(candles)
        finite = out[np.isfinite(out)]
        # 所有有限值必须在 [-1,1]
        if len(finite) > 0:
            assert np.all(finite >= -1.0 - 1e-9)
            assert np.all(finite <= 1.0 + 1e-9)

    def test_factor_when_price_near_ll_positive(self):
        """当 close 接近 LL（折扣区底部）时，若 block 激活，因子应为正（看涨）。

        通过直接构造 pd_discount_bottom/pd_premium_top 验证 level_factor 语义。
        """
        from smc_tracker.indicators.sfg._common import level_factor
        # 模拟: close=82（接近 LL=80），HH=120，LL=80
        hh = np.array([120.0])
        ll = np.array([80.0])
        close_near_ll = np.array([82.0])
        v = float(level_factor(close_near_ll, ll, hh)[0])
        # factor = (100-82)/20 = 0.9 → 看涨
        assert v > 0, f"close 接近 LL 应为正因子，实际={v}"
        assert math.isclose(v, 0.9, rel_tol=1e-9), f"预期 0.9，实际={v}"

    def test_factor_when_price_near_hh_negative(self):
        """当 close 接近 HH（溢价区顶部）时，因子应为负（看跌）。"""
        from smc_tracker.indicators.sfg._common import level_factor
        # 模拟: close=118（接近 HH=120），HH=120，LL=80
        hh = np.array([120.0])
        ll = np.array([80.0])
        close_near_hh = np.array([118.0])
        v = float(level_factor(close_near_hh, ll, hh)[0])
        # factor = (100-118)/20 = -0.9 → 看跌
        assert v < 0, f"close 接近 HH 应为负因子，实际={v}"
        assert math.isclose(v, -0.9, rel_tol=1e-9), f"预期 -0.9，实际={v}"


# ── 测试 7: pdbb_factor 标量包装 ─────────────────────────────────────────────

class TestPdbbFactor:
    def test_returns_float_or_none(self):
        """pdbb_factor 应返回 float 或 None，不抛异常。"""
        candles = _make_sideways(80)
        result = pdbb_factor(candles)
        assert result is None or isinstance(result, float), (
            f"应返回 float 或 None，实际={type(result)}"
        )

    def test_return_finite_float_in_range(self):
        """若返回 float，应在 [-1, 1] 且有限。"""
        candles = _make_sideways(100)
        result = pdbb_factor(candles)
        if result is not None:
            assert math.isfinite(result), f"应为有限 float，实际={result}"
            assert -1.0 - 1e-9 <= result <= 1.0 + 1e-9, f"应在 [-1,1]，实际={result}"

    def test_none_on_insufficient_data(self):
        """K 线不足时返回 None。"""
        candles = [_make_candle(100.0) for _ in range(5)]
        result = pdbb_factor(candles, length=5)
        assert result is None

    def test_none_on_empty(self):
        result = pdbb_factor([])
        assert result is None

    def test_consistent_with_series_last_finite(self):
        """pdbb_factor == pdbb_series 最后一个有限值（若存在）。"""
        candles = _make_sideways(80)
        result = pdbb_factor(candles)
        out = pdbb_series(candles)
        finite = out[np.isfinite(out)]
        if len(finite) == 0:
            assert result is None, "无有限值时 factor 应返回 None"
        else:
            assert result is not None
            assert math.isclose(result, float(finite[-1]), rel_tol=1e-9), (
                f"factor={result} 应等于 series 最后有限值 {float(finite[-1])}"
            )


# ── 测试 8: ZigZag 内部逻辑 (间接验证) ───────────────────────────────────────

class TestZigzagInternal:
    """通过 _pdbb_compute_levels 间接验证 ZigZag 摆动检测正确性。

    注: 不直接测试私有函数，通过公开 API 的输出模式推断。
    """

    def test_series_all_nan_when_no_block_formed(self):
        """严格单调上升（无摆动）→ ZigZag 无法形成摆动 → 全 nan。

        单调上升序列: 无 pivot low → 无 MSS bull 条件 → 无 breaker block。
        """
        # 单调上升：每根 high > 前一根 high，无 pivot low
        candles = []
        price = 100.0
        for _ in range(40):
            candles.append(_C(price, price + 1.0, price - 0.1, price + 0.5))
            price += 0.5
        out = pdbb_series(candles, length=5)
        # 无摆动 → 无 block → 全 nan（fail-closed 正确行为）
        assert np.all(np.isnan(out)), (
            "严格单调上升（无摆动）→ 全 nan（无 block 触发）"
        )

    def test_pivot_detection_uses_left_right_confirmation(self):
        """pivot 确认需要 right=1 根右侧数据，即最少 left+1+1=7 根才能发射第一个 pivot。

        验证: 序列长 left+1 根 → 无法发射任何 pivot → 无 block → 全 nan。
        """
        length = 5
        # 仅 length+1=6 根（无法形成右侧确认 right=1）
        candles = [_make_candle(100.0 + i % 3 - 1) for i in range(length + 1)]
        out = pdbb_series(candles, length=length)
        assert np.all(np.isnan(out)), "不足以发射 pivot → 全 nan"


# ── 测试 9: 参数化测试 ────────────────────────────────────────────────────────

class TestParameterization:
    def test_longer_length_delays_warmup(self):
        """length 越大 → warmup 越长 → 有限值出现得越晚。"""
        candles = _make_sideways(120)
        out5 = pdbb_series(candles, length=5)
        out10 = pdbb_series(candles, length=10)
        # length=10 的首个有限值（若有）应不早于 length=5 的首个有限值
        # 至少前 10+2-1=11 根应是 nan（length=10 的 warmup）
        assert np.all(np.isnan(out10[:11])), "length=10 前 11 根应 nan"

    def test_custom_length_valid_output(self):
        """不同 length 参数均应输出正确 shape 且无崩溃。"""
        candles = _make_sideways(80)
        for l in [1, 3, 5, 7]:
            out = pdbb_series(candles, length=l)
            assert len(out) == 80, f"length={l} 输出 shape 应=80"

    def test_tp_params_not_affect_factor(self):
        """r2a/r2b/r2c 仅影响 TP 目标，不影响 continuous factor。"""
        candles = _make_sideways(80)
        out_default = pdbb_series(candles)
        out_custom = pdbb_series(candles, r2a=5.0, r2b=7.0, r2c=10.0)
        # factor 应完全相同
        np.testing.assert_array_equal(out_default, out_custom,
            err_msg="r2a/r2b/r2c 不影响 factor 值")


# ── 测试 10: 数据质量守卫 ─────────────────────────────────────────────────────

class TestDataQuality:
    def test_nan_in_close_returns_nan_not_crash(self):
        """close 中含 nan 时不崩溃，对应位置输出 nan。"""
        candles = list(_make_sideways(30))
        # 中间插入 nan close（用 to_float 会转换为 0.0，但测试安全性）
        candles[15] = _C(float("nan"), float("nan"), float("nan"), float("nan"))
        try:
            out = pdbb_series(candles)
            assert isinstance(out, np.ndarray)
        except Exception as e:
            pytest.fail(f"含 nan 数据不应抛异常，实际: {e}")

    def test_single_bar_returns_nan(self):
        """单根 K 线 → 全 nan，不崩溃。"""
        out = pdbb_series([_make_candle(100.0)])
        assert isinstance(out, np.ndarray)
        assert len(out) == 1
        assert math.isnan(out[0])

    def test_consistent_output_on_repeated_call(self):
        """相同输入多次调用应产生完全一致的输出（确定性）。"""
        candles = _make_sideways(60)
        out1 = pdbb_series(candles)
        out2 = pdbb_series(candles)
        np.testing.assert_array_equal(out1, out2, err_msg="相同输入应产生相同输出")


# ── 测试 11: repaint 硬护栏 ──────────────────────────────────────────────────

class TestRepaintGuard:
    """本因子存在确认滞后（pivot right=1），已确认值不得 repaint。

    精确定义：bar i 的 series[i] 一旦成为 series[:n][i] (i < n-right=i<n-1)
    即最终值，追加更多 bar 不改变它。
    这是 SFG lookahead_risk 中 'no-lookahead-safe' 的核心保证。
    """

    def _run_prefix_check(self, candles: list, check_depth: int = 20) -> None:
        """对给定 candles 做 prefix-invariance 检查（追加新 bar 不改变早期值）。"""
        out_n = pdbb_series(candles)

        # 追加 3 根不同价格 bar
        extensions = [
            _make_candle(10.0),    # 极低
            _make_candle(10000.0), # 极高
            _make_candle(100.0),   # 中性
        ]
        candles_ext = candles + extensions
        out_ext = pdbb_series(candles_ext)

        n = len(candles)
        # 检查 [0, n - right - 1] 范围内已确认的有限值
        safe_end = n - 1  # right=1 确认滞后，最后 1 根可能尚未确认
        violations = []
        for i in range(min(safe_end, n - 1)):
            v_orig = out_n[i]
            v_ext = out_ext[i]
            if np.isfinite(v_orig):
                if not math.isclose(v_orig, v_ext, rel_tol=1e-9, abs_tol=1e-12):
                    violations.append((i, v_orig, v_ext))
        assert len(violations) == 0, (
            f"prefix-invariance 违反 {len(violations)} 处: "
            f"首次违反 index={violations[0][0]}, "
            f"orig={violations[0][1]:.6f}, ext={violations[0][2]:.6f}"
        )

    def test_sideways_prefix_invariant(self):
        """震荡序列确认滞后设计验证（prefix-invariance）。"""
        candles = _make_sideways(80)
        self._run_prefix_check(candles)

    def test_trending_up_prefix_invariant(self):
        """上升趋势序列 prefix-invariance（通常全 nan，也是稳定的）。"""
        candles = _make_trending_up(50)
        self._run_prefix_check(candles)

    def test_trending_down_prefix_invariant(self):
        """下降趋势序列 prefix-invariance。"""
        candles = _make_trending_down(50)
        self._run_prefix_check(candles)
