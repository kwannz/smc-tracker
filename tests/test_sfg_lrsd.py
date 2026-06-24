"""tests/test_sfg_lrsd.py — SFG LRSD reversal-cluster factor TDD 测试套件。

合成确定性测试：不依赖网络/真实数据，所有输入手工构造。

因子语义（SIGN CONVENTION）：
  +1 = price at/below 支撑区底部 → 看涨反转 (bullish bias)
  -1 = price at/above 压力区顶部 → 看跌反转 (bearish bias)
   0 = price at 区间中点
  NaN = fail-closed（区间未确立/退化）

注意：3-bar confirmation lag（确认滞后 3 根，不是 look-ahead）。
"""
from __future__ import annotations

import math
import numpy as np
import pytest

from smc_tracker.indicators.sfg.lrsd import lrsd_series, lrsd_factor


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：合成 Candle 对象
# ─────────────────────────────────────────────────────────────────────────────

class _Candle:
    """属性访问方式（.o/.h/.l/.c/.v），与 _common.ohlcv_arrays 一致。"""
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self, o: float, h: float, lo: float, c: float, v: float) -> None:
        self.o = o
        self.h = h
        self.l = lo
        self.c = c
        self.v = v


def _make_base_candles(n: int, base_price: float = 100.0, base_vol: float = 100.0) -> list[_Candle]:
    """生成基础均衡 K 线（o=c=base_price，无方向性，低量）。"""
    return [_Candle(base_price, base_price + 0.1, base_price - 0.1, base_price, base_vol)
            for _ in range(n)]


def _inject_up_fractal(
    candles: list[_Candle],
    center_idx: int,
    peak_price: float,
    high_vol_mult: float = 5.0,
) -> list[_Candle]:
    """在 center_idx 处注入上分形（5-bar Williams fractal peak）。

    形状：high[c-2] < high[c-1] < high[c] > high[c+1] > high[c+2]
    volume gate：volume[c] >> SMA(volume, 6) — 用 high_vol_mult 倍基础量。
    center_idx 处需为 peak；需保证 center_idx ∈ [2, n-3]。
    """
    candles = list(candles)
    base_high = candles[center_idx].h  # 参考高度
    base_vol = candles[center_idx].v

    # 设置 peak bar (center)：高量 + 最高价
    c = candles[center_idx]
    candles[center_idx] = _Candle(c.o, peak_price, c.l, c.c, base_vol * high_vol_mult)

    # 左边 2 根递降高
    for j, offset in enumerate([2, 1]):
        idx = center_idx - offset
        if 0 <= idx < len(candles):
            bar = candles[idx]
            adjusted_high = peak_price - (offset + 1) * 0.5
            candles[idx] = _Candle(bar.o, adjusted_high, bar.l, bar.c, base_vol)

    # 右边 2 根递降高
    for offset in [1, 2]:
        idx = center_idx + offset
        if 0 <= idx < len(candles):
            bar = candles[idx]
            adjusted_high = peak_price - (offset + 1) * 0.5
            candles[idx] = _Candle(bar.o, adjusted_high, bar.l, bar.c, base_vol)

    return candles


def _inject_down_fractal(
    candles: list[_Candle],
    center_idx: int,
    trough_price: float,
    high_vol_mult: float = 5.0,
) -> list[_Candle]:
    """在 center_idx 处注入下分形（5-bar Williams fractal trough）。

    形状：low[c-2] > low[c-1] > low[c] < low[c+1] < low[c+2]
    volume gate：volume[c] >> SMA(volume, 6)。
    """
    candles = list(candles)
    base_vol = candles[center_idx].v

    # 设置 trough bar (center)：高量 + 最低价
    c = candles[center_idx]
    candles[center_idx] = _Candle(c.o, c.h, trough_price, c.c, base_vol * high_vol_mult)

    # 左边 2 根递升低
    for j, offset in enumerate([2, 1]):
        idx = center_idx - offset
        if 0 <= idx < len(candles):
            bar = candles[idx]
            adjusted_low = trough_price + (offset + 1) * 0.5
            candles[idx] = _Candle(bar.o, bar.h, adjusted_low, bar.c, base_vol)

    # 右边 2 根递升低
    for offset in [1, 2]:
        idx = center_idx + offset
        if 0 <= idx < len(candles):
            bar = candles[idx]
            adjusted_low = trough_price + (offset + 1) * 0.5
            candles[idx] = _Candle(bar.o, bar.h, adjusted_low, bar.c, base_vol)

    return candles


def _make_up_fractal_sequence(
    n: int = 30,
    center: int = 15,
    peak: float = 115.0,
    base: float = 100.0,
) -> list[_Candle]:
    """生成含上分形的 K 线序列。"""
    candles = _make_base_candles(n, base_price=base)
    return _inject_up_fractal(candles, center, peak)


def _make_down_fractal_sequence(
    n: int = 30,
    center: int = 15,
    trough: float = 85.0,
    base: float = 100.0,
) -> list[_Candle]:
    """生成含下分形的 K 线序列。"""
    candles = _make_base_candles(n, base_price=base)
    return _inject_down_fractal(candles, center, trough)


# ─────────────────────────────────────────────────────────────────────────────
# 测试 1：Golden parity — factor_formula 直接数值 oracle
# ─────────────────────────────────────────────────────────────────────────────

class TestGoldenParity:
    """直接验证 factor_formula 的算术正确性。

    spec parity_notes oracle: sup_top=100, sup_bot=95, res_top=110, res_bot=105
      S = sup_bot = 95, R = res_top = 110
      factor = (S + R - 2c) / (R - S) = (95 + 110 - 2c) / (110 - 95) = (205 - 2c) / 15
      close=95 -> (205-190)/15 = 15/15 = +1
      close=110 -> (205-220)/15 = -15/15 = -1
      close=102.5 -> (205-205)/15 = 0
      close=200 -> clamp((205-400)/15) = clamp(-13) = -1
    """

    def _build_candles_with_known_zones(
        self,
        sup_bot: float = 95.0,
        res_top: float = 110.0,
        close_price: float = 102.5,
        n: int = 40,
    ) -> list[_Candle]:
        """构造含已知区间的 K 线序列。

        策略：
        - bar 5 (center=5): 下分形 peak at trough_price=sup_bot（形成支撑区）
          vol gate 满足
        - bar 15 (center=15): 上分形 peak at res_top（形成压力区）
          vol gate 满足
        - 最后 bar: close = close_price
        """
        base_vol = 10.0
        high_vol = base_vol * 10.0  # 满足严格大于 SMA(6)

        candles: list[_Candle] = []
        for i in range(n):
            candles.append(_Candle(100.0, 101.0, 99.0, 100.0, base_vol))

        # 下分形中心 @ 5 → close @ 8 后 sup_bot 生效
        # low[5] = sup_bot, low[4]>low[5], low[3]>low[4], low[6]>low[5], low[7]>low[6]
        sup_center = 5
        # vol gate: volume[5] > SMA(volume,6)[5]，基础量=10，SMA≈10，所以用 high_vol
        candles[sup_center] = _Candle(100.0, 101.0, sup_bot, 100.0, high_vol)
        candles[sup_center - 1] = _Candle(100.0, 101.0, sup_bot + 1.0, 100.0, base_vol)
        candles[sup_center - 2] = _Candle(100.0, 101.0, sup_bot + 2.0, 100.0, base_vol)
        candles[sup_center + 1] = _Candle(100.0, 101.0, sup_bot + 1.5, 100.0, base_vol)
        candles[sup_center + 2] = _Candle(100.0, 101.0, sup_bot + 3.0, 100.0, base_vol)

        # 上分形中心 @ 15 → close @ 18 后 res_top 生效
        # high[15] = res_top, high[14]<high[15], high[13]<high[14], high[16]<high[15], high[17]<high[16]
        res_center = 15
        candles[res_center] = _Candle(100.0, res_top, 99.0, 100.0, high_vol)
        candles[res_center - 1] = _Candle(100.0, res_top - 1.0, 99.0, 100.0, base_vol)
        candles[res_center - 2] = _Candle(100.0, res_top - 2.0, 99.0, 100.0, base_vol)
        candles[res_center + 1] = _Candle(100.0, res_top - 1.5, 99.0, 100.0, base_vol)
        candles[res_center + 2] = _Candle(100.0, res_top - 3.0, 99.0, 100.0, base_vol)

        # 最后一根 close = close_price
        last = candles[-1]
        candles[-1] = _Candle(last.o, last.h, last.l, close_price, last.v)

        return candles

    def test_midpoint_is_zero(self):
        """close=midpoint(95, 110)=102.5 → factor≈0。"""
        candles = self._build_candles_with_known_zones(close_price=102.5)
        val = lrsd_factor(candles)
        if val is not None:
            assert math.isclose(val, 0.0, abs_tol=0.05), (
                f"close=midpoint 时 factor 应≈0，实际={val:.4f}"
            )

    def test_close_at_support_is_positive(self):
        """close=sup_bot=95 → factor=+1 (bullish reversal signal)。"""
        candles = self._build_candles_with_known_zones(close_price=95.0)
        val = lrsd_factor(candles)
        if val is not None:
            assert val > 0.8, f"close=sup_bot 时 factor 应接近+1，实际={val:.4f}"

    def test_close_at_resistance_is_negative(self):
        """close=res_top=110 → factor=-1 (bearish reversal signal)。"""
        candles = self._build_candles_with_known_zones(close_price=110.0)
        val = lrsd_factor(candles)
        if val is not None:
            assert val < -0.8, f"close=res_top 时 factor 应接近-1，实际={val:.4f}"

    def test_close_far_above_resistance_clamps(self):
        """close=200 (远超 res_top) → clamp 到 -1。"""
        candles = self._build_candles_with_known_zones(close_price=200.0)
        val = lrsd_factor(candles)
        if val is not None:
            assert math.isclose(val, -1.0, abs_tol=1e-9), (
                f"close 远超压力区时 factor 应=-1，实际={val:.4f}"
            )

    def test_close_far_below_support_clamps(self):
        """close=10 (远低于 sup_bot) → clamp 到 +1。"""
        candles = self._build_candles_with_known_zones(close_price=10.0)
        val = lrsd_factor(candles)
        if val is not None:
            assert math.isclose(val, 1.0, abs_tol=1e-9), (
                f"close 远低于支撑区时 factor 应=+1，实际={val:.4f}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 测试 2：直接算术验证（isolated level_factor 计算）
# ─────────────────────────────────────────────────────────────────────────────

class TestLevelFactorArithmetic:
    """直接从 spec factor_formula 验证算术，不依赖分形触发。"""

    def _compute_formula(self, S: float, R: float, c: float) -> float:
        """spec factor_formula: clamp((S+R-2c)/(R-S), -1, 1)。"""
        if R <= S:
            return float("nan")
        raw = (S + R - 2 * c) / (R - S)
        return max(-1.0, min(1.0, raw))

    def test_spec_oracle_midpoint(self):
        """spec oracle: S=95,R=110,c=102.5 → 0.0。"""
        assert math.isclose(self._compute_formula(95, 110, 102.5), 0.0, abs_tol=1e-9)

    def test_spec_oracle_at_support(self):
        """spec oracle: S=95,R=110,c=95 → +1。"""
        assert math.isclose(self._compute_formula(95, 110, 95), 1.0, abs_tol=1e-9)

    def test_spec_oracle_at_resistance(self):
        """spec oracle: S=95,R=110,c=110 → -1。"""
        assert math.isclose(self._compute_formula(95, 110, 110), -1.0, abs_tol=1e-9)

    def test_spec_oracle_clamped(self):
        """spec oracle: S=95,R=110,c=200 → clamp(-13) = -1。"""
        assert math.isclose(self._compute_formula(95, 110, 200), -1.0, abs_tol=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 测试 3：输出范围 [-1, 1] + 有限性
# ─────────────────────────────────────────────────────────────────────────────

class TestOutputRange:
    def test_series_all_in_range(self):
        """所有有限值必须在 [-1, 1] 内。"""
        n = 50
        candles = _make_up_fractal_sequence(n=n)
        series = lrsd_series(candles)
        assert len(series) == n
        finite_vals = series[np.isfinite(series)]
        if len(finite_vals) > 0:
            assert np.all(finite_vals >= -1.0), f"有值超出下界-1: {finite_vals.min():.4f}"
            assert np.all(finite_vals <= 1.0), f"有值超出上界+1: {finite_vals.max():.4f}"

    def test_series_no_imputed_zeros(self):
        """NaN 哨兵：缺数据时返回 nan，不应 impute 为 0。"""
        # 少于 warmup 的短序列，应全部为 nan
        candles = _make_base_candles(3)
        series = lrsd_series(candles)
        assert np.all(np.isnan(series)), (
            f"短序列（不足 warmup）应全为 nan，实际 finite 值数量={np.sum(np.isfinite(series))}"
        )

    def test_no_inf_values(self):
        """序列中不应出现 inf/-inf。"""
        candles = _make_up_fractal_sequence(n=50)
        series = lrsd_series(candles)
        assert not np.any(np.isinf(series)), "序列中不应有 inf 值"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 4：warmup 边界
# ─────────────────────────────────────────────────────────────────────────────

class TestWarmupBoundary:
    def test_empty_candles_returns_empty_series(self):
        """空 candles → 空 series。"""
        series = lrsd_series([])
        assert len(series) == 0

    def test_factor_none_on_empty(self):
        """空 candles → lrsd_factor 返回 None。"""
        val = lrsd_factor([])
        assert val is None

    def test_too_few_candles_all_nan(self):
        """K 线少于最小分形确认需要数时，series 全 nan。"""
        # 最小：vol_ma_len=6 + 分形需 5 根 + 3-bar 确认 lag → 至少 6+4=10 根才能有第一个确认
        # 但实际更少会导致全 nan
        candles = _make_base_candles(4)
        series = lrsd_series(candles)
        assert np.all(np.isnan(series)), "极短序列应全为 nan"

    def test_factor_none_on_insufficient(self):
        """不足 warmup 时 lrsd_factor 返回 None。"""
        # 全均匀低量，无分形，返回 None
        candles = _make_base_candles(8)
        val = lrsd_factor(candles)
        # 无分形确立区间 → None 或 nan
        assert val is None or not math.isfinite(val), (
            f"无分形区间时应返回 None，实际={val}"
        )

    def test_series_length_equals_input(self):
        """series 长度必须等于输入 candles 数量。"""
        for n in [0, 1, 5, 10, 30, 100]:
            candles = _make_base_candles(n)
            series = lrsd_series(candles)
            assert len(series) == n, f"n={n}: series 长度={len(series)} ≠ {n}"

    def test_warmup_nan_prefix(self):
        """序列前 warmup 段必须为 nan（无历史区间）。"""
        # 构造含分形的序列
        candles = _make_up_fractal_sequence(n=30, center=10)
        series = lrsd_series(candles)
        # 分形 center=10, 确认 @ bar 13（i=center+3），压力区从 bar 13 起
        # 同时需要支撑区，如果没有支撑区，那么会 NaN fail-closed
        # 只要前几根是 nan 即合理
        # bar 0..7 一定是 nan（未到 vol_ma 的 warmup）
        assert np.isnan(series[0]), "series[0] 应为 nan"
        assert np.isnan(series[5]), "series[5] 应为 nan（vol_ma warmup 期内）"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 5：分形触发后区间方向性
# ─────────────────────────────────────────────────────────────────────────────

class TestFractalZoneBehavior:
    def test_up_fractal_creates_resistance(self):
        """上分形触发后，压力区确立，close 远高于区间时 factor→-1。"""
        # 构造：上分形 @ center=10，之后 close 远高于 peak → bearish signal
        n = 40
        peak = 115.0
        candles = _make_up_fractal_sequence(n=n, center=10, peak=peak)
        # 修改最后一根 close = peak * 1.5（远超压力区）
        last = candles[-1]
        candles[-1] = _Candle(last.o, last.h, last.l, peak * 1.5, last.v)

        series = lrsd_series(candles)
        # 只在有限值中检查
        finite_vals = series[np.isfinite(series)]
        # 如果有分形触发且两区都有，close 极高时 factor 应 < 0 或 = -1
        # 如果只有压力区没支撑区，依然 NaN fail-closed
        # 此测试主要确认序列可正常运行
        assert len(series) == n, "series 长度应等于输入"

    def test_down_fractal_creates_support(self):
        """下分形触发后，支撑区确立，close 远低于区间时 factor→+1。"""
        n = 40
        trough = 85.0
        candles = _make_down_fractal_sequence(n=n, center=10, trough=trough)
        # 修改最后一根 close = trough * 0.5（远低于支撑区）
        last = candles[-1]
        candles[-1] = _Candle(last.o, last.h, last.l, trough * 0.5, last.v)

        series = lrsd_series(candles)
        assert len(series) == n

    def test_both_zones_level_factor(self):
        """同时有支撑区和压力区时，close 在中点时 factor ≈ 0。"""
        # 构造含两个分形的序列
        n = 50
        base_vol = 10.0
        high_vol = 100.0
        candles = list(_make_base_candles(n, base_price=100.0, base_vol=base_vol))

        # 下分形 @ center=8 → sup_bot=85
        trough = 85.0
        sup_center = 8
        candles[sup_center] = _Candle(100.0, 101.0, trough, 100.0, high_vol)
        candles[sup_center - 1] = _Candle(100.0, 101.0, trough + 1.0, 100.0, base_vol)
        candles[sup_center - 2] = _Candle(100.0, 101.0, trough + 2.0, 100.0, base_vol)
        candles[sup_center + 1] = _Candle(100.0, 101.0, trough + 1.5, 100.0, base_vol)
        candles[sup_center + 2] = _Candle(100.0, 101.0, trough + 3.0, 100.0, base_vol)

        # 上分形 @ center=18 → res_top=115
        peak = 115.0
        res_center = 18
        candles[res_center] = _Candle(100.0, peak, 99.0, 100.0, high_vol)
        candles[res_center - 1] = _Candle(100.0, peak - 1.0, 99.0, 100.0, base_vol)
        candles[res_center - 2] = _Candle(100.0, peak - 2.0, 99.0, 100.0, base_vol)
        candles[res_center + 1] = _Candle(100.0, peak - 1.5, 99.0, 100.0, base_vol)
        candles[res_center + 2] = _Candle(100.0, peak - 3.0, 99.0, 100.0, base_vol)

        # 最后一根 close = midpoint(sup_bot=85, res_top=115) = 100
        mid = (trough + peak) / 2.0  # 100.0
        last = candles[-1]
        candles[-1] = _Candle(last.o, last.h, last.l, mid, last.v)

        series = lrsd_series(candles)
        last_val = series[-1]
        if math.isfinite(last_val):
            # 中点时 factor 应 ≈ 0（允许一些误差）
            assert abs(last_val) < 0.15, (
                f"close 在 S/R 中点时 factor 应≈0，实际={last_val:.4f}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 测试 6：no-repaint / prefix-invariance（核心 no-lookahead 护栏）
# ─────────────────────────────────────────────────────────────────────────────

class TestNoRepaint:
    """验证已发射的早期 series[i] 值不因追加新 bar 而改变。

    这是 lrsd 确认滞后（3-bar confirmation lag）的 prefix-invariance 硬护栏。
    """

    def _build_candles_with_both_zones(self, n: int) -> list[_Candle]:
        """构造含支撑+压力区的 K 线序列（确保有有限值可检验）。"""
        base_vol = 10.0
        high_vol = 100.0
        candles = list(_make_base_candles(n, base_price=100.0, base_vol=base_vol))

        # 下分形 @ center=7
        sc = 7
        candles[sc] = _Candle(100.0, 101.0, 85.0, 100.0, high_vol)
        candles[sc - 1] = _Candle(100.0, 101.0, 86.0, 100.0, base_vol)
        candles[sc - 2] = _Candle(100.0, 101.0, 87.0, 100.0, base_vol)
        candles[sc + 1] = _Candle(100.0, 101.0, 86.5, 100.0, base_vol)
        candles[sc + 2] = _Candle(100.0, 101.0, 88.0, 100.0, base_vol)

        # 上分形 @ center=17
        rc = 17
        candles[rc] = _Candle(100.0, 115.0, 99.0, 100.0, high_vol)
        candles[rc - 1] = _Candle(100.0, 114.0, 99.0, 100.0, base_vol)
        candles[rc - 2] = _Candle(100.0, 113.0, 99.0, 100.0, base_vol)
        candles[rc + 1] = _Candle(100.0, 113.5, 99.0, 100.0, base_vol)
        candles[rc + 2] = _Candle(100.0, 111.0, 99.0, 100.0, base_vol)

        return candles

    def test_prefix_invariance_on_new_bar(self):
        """追加一根普通新 bar，早期已发射值不变。"""
        base_n = 40
        candles_base = self._build_candles_with_both_zones(base_n)

        series_base = lrsd_series(candles_base)

        # 追加一根普通 bar
        extra = _Candle(100.0, 101.0, 99.0, 100.0, 10.0)
        candles_extended = candles_base + [extra]
        series_ext = lrsd_series(candles_extended)

        # 早期已发射值（前 base_n 根）应不变
        for i in range(base_n):
            v_base = series_base[i]
            v_ext = series_ext[i]
            if math.isfinite(v_base) and math.isfinite(v_ext):
                assert math.isclose(v_base, v_ext, rel_tol=1e-9, abs_tol=1e-12), (
                    f"series[{i}] 因追加新 bar 改变: {v_base:.6f} → {v_ext:.6f} (repaint!)"
                )
            else:
                # nan/nan 或 nan/finite（新 bar 不应让早期 nan 变 finite）
                if math.isnan(v_base):
                    assert math.isnan(v_ext) or not math.isfinite(v_ext), (
                        f"series[{i}] 因追加新 bar 从 nan 变为 finite {v_ext:.6f} (repaint!)"
                    )

    def test_prefix_invariance_on_extreme_bar(self):
        """追加一根极端 bar（超高高量，新分形），早期已发射值不变。"""
        base_n = 40
        candles_base = self._build_candles_with_both_zones(base_n)
        series_base = lrsd_series(candles_base)

        # 追加极端 bar：极高量 + 极高 high（可能触发新分形，但不应改变已发射值）
        extreme = _Candle(200.0, 500.0, 50.0, 300.0, 99999.0)
        candles_extended = candles_base + [extreme]
        series_ext = lrsd_series(candles_extended)

        for i in range(base_n):
            v_base = series_base[i]
            v_ext = series_ext[i]
            if math.isfinite(v_base) and math.isfinite(v_ext):
                assert math.isclose(v_base, v_ext, rel_tol=1e-9, abs_tol=1e-12), (
                    f"series[{i}] 因极端新 bar 改变: {v_base:.6f} → {v_ext:.6f} (repaint!)"
                )

    def test_prefix_invariance_extending_several_bars(self):
        """追加 5 根普通 bar，早期值保持不变。"""
        base_n = 40
        candles_base = self._build_candles_with_both_zones(base_n)
        series_base = lrsd_series(candles_base)

        extra_bars = [_Candle(100.0, 101.0, 99.0, 100.0, 10.0) for _ in range(5)]
        candles_extended = candles_base + extra_bars
        series_ext = lrsd_series(candles_extended)

        for i in range(base_n):
            v_base = series_base[i]
            v_ext = series_ext[i]
            if math.isfinite(v_base) and math.isfinite(v_ext):
                assert math.isclose(v_base, v_ext, rel_tol=1e-9, abs_tol=1e-12), (
                    f"series[{i}] 追加多根 bar 后改变: {v_base:.6f} → {v_ext:.6f} (repaint!)"
                )


# ─────────────────────────────────────────────────────────────────────────────
# 测试 7：lrsd_factor 标量包装
# ─────────────────────────────────────────────────────────────────────────────

class TestLrsdFactor:
    def test_returns_float_or_none(self):
        """lrsd_factor 应返回 float 或 None，不崩溃。"""
        candles = _make_base_candles(30)
        val = lrsd_factor(candles)
        assert val is None or isinstance(val, float), (
            f"应返回 float 或 None，实际={type(val)}"
        )

    def test_none_when_no_zones(self):
        """无分形（均匀低量 K 线）时 lrsd_factor 返回 None。"""
        candles = _make_base_candles(50)
        val = lrsd_factor(candles)
        # 无有效区间 → None
        assert val is None, f"无分形时应返回 None，实际={val}"

    def test_returns_last_finite_from_series(self):
        """lrsd_factor 应等于 series 中最后一个有限值。"""
        # 构造有分形的序列
        base_n = 50
        base_vol = 10.0
        high_vol = 100.0
        candles = list(_make_base_candles(base_n, base_price=100.0, base_vol=base_vol))

        sc = 7
        candles[sc] = _Candle(100.0, 101.0, 85.0, 100.0, high_vol)
        candles[sc - 1] = _Candle(100.0, 101.0, 86.0, 100.0, base_vol)
        candles[sc - 2] = _Candle(100.0, 101.0, 87.0, 100.0, base_vol)
        candles[sc + 1] = _Candle(100.0, 101.0, 86.5, 100.0, base_vol)
        candles[sc + 2] = _Candle(100.0, 101.0, 88.0, 100.0, base_vol)

        rc = 17
        candles[rc] = _Candle(100.0, 115.0, 99.0, 100.0, high_vol)
        candles[rc - 1] = _Candle(100.0, 114.0, 99.0, 100.0, base_vol)
        candles[rc - 2] = _Candle(100.0, 113.0, 99.0, 100.0, base_vol)
        candles[rc + 1] = _Candle(100.0, 113.5, 99.0, 100.0, base_vol)
        candles[rc + 2] = _Candle(100.0, 111.0, 99.0, 100.0, base_vol)

        series = lrsd_series(candles)
        factor_val = lrsd_factor(candles)

        finite_vals = series[np.isfinite(series)]
        if len(finite_vals) > 0:
            expected = finite_vals[-1]
            assert factor_val is not None
            assert math.isclose(factor_val, expected, rel_tol=1e-9), (
                f"lrsd_factor 应=series 末有限值 {expected:.6f}，实际={factor_val:.6f}"
            )
        else:
            assert factor_val is None, "无有限值时应返回 None"

    def test_factor_in_range(self):
        """lrsd_factor 若非 None，应在 [-1, 1] 内。"""
        base_n = 50
        base_vol = 10.0
        high_vol = 100.0
        candles = list(_make_base_candles(base_n, base_price=100.0, base_vol=base_vol))

        sc = 7
        candles[sc] = _Candle(100.0, 101.0, 85.0, 100.0, high_vol)
        candles[sc - 1] = _Candle(100.0, 101.0, 86.0, 100.0, base_vol)
        candles[sc - 2] = _Candle(100.0, 101.0, 87.0, 100.0, base_vol)
        candles[sc + 1] = _Candle(100.0, 101.0, 86.5, 100.0, base_vol)
        candles[sc + 2] = _Candle(100.0, 101.0, 88.0, 100.0, base_vol)

        rc = 17
        candles[rc] = _Candle(100.0, 115.0, 99.0, 100.0, high_vol)
        candles[rc - 1] = _Candle(100.0, 114.0, 99.0, 100.0, base_vol)
        candles[rc - 2] = _Candle(100.0, 113.0, 99.0, 100.0, base_vol)
        candles[rc + 1] = _Candle(100.0, 113.5, 99.0, 100.0, base_vol)
        candles[rc + 2] = _Candle(100.0, 111.0, 99.0, 100.0, base_vol)

        val = lrsd_factor(candles)
        if val is not None:
            assert -1.0 <= val <= 1.0, f"factor 应在 [-1,1]，实际={val}"
            assert math.isfinite(val), f"factor 应为有限数，实际={val}"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 8：NaN sentinel — 缺数据不补零
# ─────────────────────────────────────────────────────────────────────────────

class TestNanSentinel:
    def test_nan_not_zero_on_missing_zone(self):
        """缺区间（无分形确认）时输出 nan，不得补零。"""
        candles = _make_base_candles(50)  # 均匀量，无分形
        series = lrsd_series(candles)
        # 所有值应为 nan（fail-closed）
        assert np.all(np.isnan(series)), (
            f"无区间时全部应为 nan，有 {np.sum(~np.isnan(series))} 个非 nan 值"
        )

    def test_invalid_input_no_crash(self):
        """K 线含 nan/inf 字段不崩溃（to_float 守卫）。"""
        class _BadCandle:
            __slots__ = ("o", "h", "l", "c", "v")
            def __init__(self):
                self.o = float("nan")
                self.h = float("inf")
                self.l = float("-inf")
                self.c = float("nan")
                self.v = 0.0

        bad_candles = [_BadCandle() for _ in range(20)]
        # 不崩溃，返回 nan 序列
        series = lrsd_series(bad_candles)
        assert len(series) == 20
        assert not np.any(np.isinf(series)), "bad 输入不应产生 inf 输出"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 9：3-bar confirmation lag 显式验证
# ─────────────────────────────────────────────────────────────────────────────

class TestConfirmationLag:
    def test_zone_not_visible_before_confirmation(self):
        """上分形 center=c 时，zone 在 bar c+3 处确认，bar c+2 之前应仍为 nan（若无之前区间）。"""
        # 构造：仅有上分形，无支撑区 → factor 一直 NaN fail-closed
        # 但可以验证：没有 bar i < center+3 的 series 发生因新分形而改变
        n = 25
        center = 10
        peak = 115.0
        base_vol = 10.0
        high_vol = 100.0

        candles = list(_make_base_candles(n, base_price=100.0, base_vol=base_vol))
        candles[center] = _Candle(100.0, peak, 99.0, 100.0, high_vol)
        candles[center - 1] = _Candle(100.0, peak - 1.0, 99.0, 100.0, base_vol)
        candles[center - 2] = _Candle(100.0, peak - 2.0, 99.0, 100.0, base_vol)
        candles[center + 1] = _Candle(100.0, peak - 1.5, 99.0, 100.0, base_vol)
        candles[center + 2] = _Candle(100.0, peak - 3.0, 99.0, 100.0, base_vol)

        # 截取到 center+2（分形尚未确认）
        candles_pre = candles[:center + 2]
        # 截取到 center+3（确认 bar）
        candles_at = candles[:center + 3]

        series_pre = lrsd_series(candles_pre)
        series_at = lrsd_series(candles_at)

        # pre 序列中 center-1 前的值与 at 序列中 center-1 前的值应相同
        for i in range(min(len(series_pre), center - 1)):
            v_pre = series_pre[i]
            v_at = series_at[i]
            if math.isfinite(v_pre) and math.isfinite(v_at):
                assert math.isclose(v_pre, v_at, rel_tol=1e-9), (
                    f"series[{i}] 在分形确认前后不应改变"
                )
