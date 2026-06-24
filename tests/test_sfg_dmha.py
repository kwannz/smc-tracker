"""tests/test_sfg_dmha.py — SFG DMHA 因子 TDD 测试套件。

基于 sfg_spec_dmha.json 规格的确定性合成测试：
  - trending-up / trending-down / sideways 合成 K 线，断言因子符号
  - 输出范围 [-1,1]、有限性、离散值 {-1,0,+1}
  - warmup 边界：不足 → nan/None
  - NaN 哨兵守卫

SIGN CONVENTION (趋势簇，非反转簇)：
  +1.0 = bullish MACD 动量（HA 蜡烛 green, ha_close > ha_open）
  -1.0 = bearish MACD 动量（HA 蜡烛 red,   ha_close < ha_open）
   0.0 = doji（ha_close == ha_open）
  NaN  = 不足 warmup / 数据缺失

诚实标注：dmha 是重度平滑（gf+HMA）动态 MACD 的 HA 方向，属于滞后动量跟踪指标；
它不是领先反转信号。KNN 特征仅辅助，不构成投资建议。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from smc_tracker.indicators.sfg.dmha import dmha_series, dmha_factor


# ─────────────────────────────────────────────────────────────────────────────
# 合成 Candle 辅助
# ─────────────────────────────────────────────────────────────────────────────

class _Candle:
    """属性访问方式（.o/.h/.l/.c/.v），与 _common.ohlcv_arrays 兼容。"""
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self, o: float, h: float, lo: float, c: float, v: float = 1000.0) -> None:
        self.o = o
        self.h = h
        self.l = lo
        self.c = c
        self.v = v


def _make_trending_up(n: int = 300, start: float = 100.0, step: float = 0.5) -> list[_Candle]:
    """生成上升趋势 K 线（close 每根递增 step）。

    注：DMHA 是「动态 MACD」的 HA 方向——它响应 MACD 的「加速度/变化率」，
    不是单纯趋势方向。恒定斜率趋势在 gf 收敛后产生常数 MACD → doji（0.0），
    这是正确行为，非实现 bug。

    此函数保留为 warmup/输出范围/离散性测试用；
    符号断言测试改用 _make_accel_up / _make_accel_down。
    """
    candles: list[_Candle] = []
    price = start
    for _ in range(n):
        c = price + step
        o = price
        h = c + abs(step) * 0.1
        lo = o - abs(step) * 0.1
        candles.append(_Candle(o, h, lo, c))
        price = c
    return candles


def _make_accel_up(n: int = 300, start: float = 100.0, base_step: float = 0.1,
                   accel_rate: float = 0.01) -> list[_Candle]:
    """生成加速上升趋势 K 线（每根 step 递增 accel_rate）。

    加速上升 → fast_gf 超过 slow_gf 的速率持续增大 → raw_macd 持续升高
    → MACD 序列 HA 蜡烛 green（ha_close > ha_open） → dmha = +1.0。
    """
    candles: list[_Candle] = []
    price = start
    step = base_step
    for _ in range(n):
        c = price + step
        o = price
        h = c + abs(step) * 0.1
        lo = o - abs(step) * 0.1
        candles.append(_Candle(o, h, lo, c))
        price = c
        step += accel_rate
    return candles


def _make_accel_down(n: int = 300, start: float = 300.0, base_step: float = 0.1,
                     accel_rate: float = 0.01) -> list[_Candle]:
    """生成加速下降趋势 K 线（每根 step 递增 accel_rate，close 每根递减更多）。

    加速下降 → fast_gf 低于 slow_gf 的差距持续扩大 → raw_macd 持续下降
    → MACD 序列 HA 蜡烛 red（ha_close < ha_open） → dmha = -1.0。
    """
    candles: list[_Candle] = []
    price = start
    step = base_step
    for _ in range(n):
        c = price - step
        o = price
        h = o + abs(step) * 0.1
        lo = c - abs(step) * 0.1
        candles.append(_Candle(o, h, lo, c))
        price = c
        step += accel_rate
    return candles


def _make_trending_down(n: int = 300, start: float = 300.0, step: float = 0.5) -> list[_Candle]:
    """生成下降趋势 K 线（close 每根递减 step）。

    恒定斜率下降在 gf 收敛后产生常数 MACD → doji，同 _make_trending_up 同理。
    保留为 warmup/范围/离散性测试用；符号断言测试改用 _make_accel_down。
    """
    candles: list[_Candle] = []
    price = start
    for _ in range(n):
        c = price - step
        o = price
        h = o + abs(step) * 0.1
        lo = c - abs(step) * 0.1
        candles.append(_Candle(o, h, lo, c))
        price = c
    return candles


def _make_sideways(n: int = 300, base: float = 100.0, amplitude: float = 0.3) -> list[_Candle]:
    """生成横盘震荡 K 线（正弦震荡，MACD 均值接近 0）。"""
    candles: list[_Candle] = []
    for i in range(n):
        c = base + amplitude * math.sin(i * math.pi / 8)
        o_val = base + amplitude * math.sin((i - 1) * math.pi / 8) if i > 0 else base
        h = max(o_val, c) + 0.02
        lo = min(o_val, c) - 0.02
        candles.append(_Candle(o_val, h, lo, c))
    return candles


# ─────────────────────────────────────────────────────────────────────────────
# 测试 1：dmha_series 基础属性（恒定斜率上升趋势：测结构属性）
# ─────────────────────────────────────────────────────────────────────────────

class TestDmhaSeriesUpTrend:
    def setup_method(self):
        self.candles = _make_trending_up(n=300, step=0.5)
        self.series = dmha_series(self.candles)

    def test_output_length(self):
        """输出长度应等于输入 K 线数量。"""
        assert len(self.series) == len(self.candles), (
            f"输出长度 {len(self.series)} != 输入 {len(self.candles)}"
        )

    def test_warmup_nan(self):
        """前 warmup 段（至少前 7 根）应为 NaN。"""
        # HMA(6) 首个非 NaN 在 idx=6，ha_close 首个非 NaN 在 idx=7
        # 故 series[:7] 应全为 nan
        for i in range(7):
            assert math.isnan(self.series[i]), (
                f"series[{i}]={self.series[i]} 应为 NaN（warmup）"
            )

    def test_output_range(self):
        """所有有限值应在 [-1, 1] 范围内。"""
        finite = self.series[np.isfinite(self.series)]
        assert len(finite) > 0, "应有有限值"
        assert np.all(finite >= -1.0) and np.all(finite <= 1.0), (
            f"有限值超出 [-1,1]: min={finite.min():.4f}, max={finite.max():.4f}"
        )

    def test_discrete_values(self):
        """有限值应只含 {-1.0, 0.0, +1.0}。"""
        finite = self.series[np.isfinite(self.series)]
        unique = np.unique(finite)
        for v in unique:
            assert v in (-1.0, 0.0, 1.0), (
                f"发现非离散值: {v}（应只含 {{-1,0,+1}}）"
            )

    def test_accel_up_last_value_positive(self):
        """加速上升趋势 300 根，末值应为 +1.0（上涨 MACD 动量）。

        诚实标注：DMHA 是 MACD 序列的 Heikin-Ashi 方向，响应 MACD「变化率」。
        恒定斜率 → gf 收敛后 MACD 常数 → doji（0.0），这是正确行为，非 bug。
        需要「加速上升」（step 递增）才能产生持续上升的 MACD → dmha=+1.0。
        """
        accel_series = dmha_series(_make_accel_up(n=300))
        last_finite = accel_series[np.isfinite(accel_series)][-1]
        assert last_finite == 1.0, (
            f"加速上升趋势末值应 +1.0（上涨动量），实际={last_finite}"
        )

    def test_accel_up_majority_positive_after_warmup(self):
        """加速上升趋势 warmup 后，超半数有限值应为 +1.0。"""
        accel_series = dmha_series(_make_accel_up(n=300))
        post_warmup = accel_series[50:]
        finite = post_warmup[np.isfinite(post_warmup)]
        assert len(finite) > 0
        frac_positive = np.mean(finite == 1.0)
        assert frac_positive > 0.5, (
            f"加速上升 warmup 后正值占比={frac_positive:.2%}，应 >50%"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 测试 2：下降趋势（恒定斜率：结构属性；加速斜率：符号断言）
# ─────────────────────────────────────────────────────────────────────────────

class TestDmhaSeriesDownTrend:
    def setup_method(self):
        self.candles = _make_trending_down(n=300, step=0.5)
        self.series = dmha_series(self.candles)

    def test_output_length(self):
        assert len(self.series) == 300

    def test_output_range(self):
        finite = self.series[np.isfinite(self.series)]
        assert len(finite) > 0
        assert np.all(finite >= -1.0) and np.all(finite <= 1.0)

    def test_accel_down_last_value_negative(self):
        """加速下降趋势末值应为 -1.0（下跌 MACD 动量）。

        同 TestDmhaSeriesUpTrend 的诚实标注：恒定斜率 → doji；加速斜率 → -1.0。
        """
        accel_series = dmha_series(_make_accel_down(n=300))
        last_finite = accel_series[np.isfinite(accel_series)][-1]
        assert last_finite == -1.0, (
            f"加速下降趋势末值应 -1.0（下跌动量），实际={last_finite}"
        )

    def test_accel_down_majority_negative_after_warmup(self):
        """加速下降趋势 warmup 后，超半数有限值应为 -1.0。"""
        accel_series = dmha_series(_make_accel_down(n=300))
        post_warmup = accel_series[50:]
        finite = post_warmup[np.isfinite(post_warmup)]
        assert len(finite) > 0
        frac_negative = np.mean(finite == -1.0)
        assert frac_negative > 0.5, (
            f"加速下降 warmup 后负值占比={frac_negative:.2%}，应 >50%"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 测试 3：方向对称性（加速上升 vs 加速下降应相反）
# ─────────────────────────────────────────────────────────────────────────────

class TestDmhaSymmetry:
    def test_opposite_directions(self):
        """加速上升和加速下降趋势，warmup 后末尾状态应相反（+1 vs -1）。

        诚实标注：对称性测试使用加速趋势（非恒定斜率），因为 DMHA 响应 MACD 变化率，
        恒定斜率趋势在 gf 收敛后产生常数 MACD → doji（0.0）。
        """
        up = dmha_series(_make_accel_up(n=300))
        down = dmha_series(_make_accel_down(n=300))
        up_last = up[np.isfinite(up)][-1]
        down_last = down[np.isfinite(down)][-1]
        assert up_last == 1.0, f"加速上升末值应 +1, 实际={up_last}"
        assert down_last == -1.0, f"加速下降末值应 -1, 实际={down_last}"
        assert up_last == -down_last, "加速上升和下降趋势末值应相反"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 4：横盘 → 有限值存在但动量较弱（不强制断言 0，但应有限）
# ─────────────────────────────────────────────────────────────────────────────

class TestDmhaSideways:
    def test_finite_values_exist(self):
        """横盘序列应有有限输出（warmup 后）。"""
        series = dmha_series(_make_sideways(n=300))
        finite = series[np.isfinite(series)]
        assert len(finite) > 0, "横盘序列应有有限输出"

    def test_output_range_sideways(self):
        """横盘有限值仍应在 [-1,1] 且为离散值。"""
        series = dmha_series(_make_sideways(n=300))
        finite = series[np.isfinite(series)]
        assert len(finite) > 0
        assert np.all(finite >= -1.0) and np.all(finite <= 1.0)
        for v in np.unique(finite):
            assert v in (-1.0, 0.0, 1.0), f"非离散值: {v}"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 5：warmup 边界（不足数据 → nan / None）
# ─────────────────────────────────────────────────────────────────────────────

class TestWarmupBoundary:
    def test_empty_candles_returns_empty(self):
        """空 candles → 空 series。"""
        s = dmha_series([])
        assert len(s) == 0, f"空输入应返回空 array，实际 len={len(s)}"

    def test_one_candle_all_nan(self):
        """单根 K 线 → series 全为 NaN。"""
        candles = _make_trending_up(n=1)
        s = dmha_series(candles)
        assert len(s) == 1
        assert math.isnan(s[0]), f"单根 K 线应为 NaN，实际={s[0]}"

    def test_short_series_all_nan(self):
        """少于 HMA+HA warmup 的序列，全部输出 NaN。"""
        candles = _make_trending_up(n=7)  # HMA(6) 首个 non-NaN 在 idx=6，HA 需 idx-1，故 idx=7 才有
        s = dmha_series(candles)
        assert len(s) == 7
        # idx=0..6 应全为 NaN（ha_close 首个非 NaN 在 idx=7）
        for i in range(7):
            assert math.isnan(s[i]), f"s[{i}]={s[i]} 应为 NaN"

    def test_factor_none_on_short_input(self):
        """dmha_factor 不足 warmup → None。"""
        candles = _make_trending_up(n=5)
        result = dmha_factor(candles)
        assert result is None, f"不足 warmup 应返回 None，实际={result}"

    def test_factor_returns_float_on_sufficient(self):
        """dmha_factor 足够根数 → 返回 float。"""
        candles = _make_trending_up(n=50)
        result = dmha_factor(candles)
        # 50 根应足以产生有限值（warmup 约 8-9 根，50>>8）
        assert result is not None, "50 根应有有限 dmha_factor 值"
        assert isinstance(result, float), f"应返回 float，实际={type(result)}"
        assert math.isfinite(result), f"dmha_factor 应有限，实际={result}"

    def test_factor_in_discrete_set(self):
        """dmha_factor 末值应在 {-1.0, 0.0, +1.0}。"""
        candles = _make_trending_up(n=50)
        result = dmha_factor(candles)
        assert result is not None
        assert result in (-1.0, 0.0, 1.0), f"末值应为 {{-1,0,+1}}，实际={result}"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 6：NaN 哨兵守卫（无数据不 impute 为 0）
# ─────────────────────────────────────────────────────────────────────────────

class TestNanSentinel:
    def test_leading_nan_not_imputed(self):
        """series 开头的 NaN 不能被替换为 0（fail-closed，非 impute）。"""
        candles = _make_trending_up(n=20)
        s = dmha_series(candles)
        # 前几个肯定是 NaN
        assert math.isnan(s[0]), "s[0] 应为 NaN，不应被 impute 为 0"

    def test_nan_input_candles_carry_forward(self):
        """K 线中有 NaN close 时，gf 应 carry-forward 前值，不崩溃。"""
        candles = _make_trending_up(n=50, step=0.5)
        # 注入 NaN close（模拟数据质量问题）
        bad = _Candle(candles[20].o, candles[20].h, candles[20].l, float("nan"))
        candles_with_nan = candles[:20] + [bad] + candles[21:]
        # 不应抛出异常，应正常返回
        s = dmha_series(candles_with_nan)
        assert len(s) == 50, "NaN 输入不应改变输出长度"
        # 注入 NaN 后后续值应逐步恢复（gf carry-forward），不应全变 NaN
        finite_after = s[25:]
        assert np.any(np.isfinite(finite_after)), "NaN 输入后应能恢复有限输出"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 7：因果性（prefix invariance 简单检查）
# ─────────────────────────────────────────────────────────────────────────────

class TestCausality:
    def test_prefix_invariance(self):
        """前缀不变性：series[:k] 应与对 candles[:k] 单独计算结果一致。

        验证无前视（no-lookahead）：追加未来 K 线不影响历史值。
        """
        candles = _make_trending_up(n=200, step=0.5)
        # 完整序列
        full = dmha_series(candles)
        # 前 100 根子集
        sub = dmha_series(candles[:100])
        # 前 100 根的结果应一致（无论后 100 根是否存在）
        np.testing.assert_array_equal(
            full[:100], sub,
            err_msg="追加未来 K 线不应改变历史输出（因果性违反）"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 测试 8：dmha_factor 末值一致性
# ─────────────────────────────────────────────────────────────────────────────

class TestFactorLastValue:
    def test_factor_equals_series_last_finite(self):
        """dmha_factor 应返回 series 中最后一个有限值。"""
        candles = _make_trending_up(n=100, step=0.5)
        series = dmha_series(candles)
        factor = dmha_factor(candles)

        finite = series[np.isfinite(series)]
        if len(finite) == 0:
            assert factor is None
        else:
            expected = float(finite[-1])
            assert factor == expected, (
                f"dmha_factor={factor} 应等于 series 末有限值={expected}"
            )

    def test_factor_none_when_all_nan(self):
        """series 全为 NaN 时 dmha_factor 应返回 None。"""
        candles = _make_trending_up(n=3)
        result = dmha_factor(candles)
        # 3 根不足 warmup（HMA+HA 至少需要 ~8 根），series 全 NaN
        assert result is None, f"全 NaN 序列应返回 None，实际={result}"
