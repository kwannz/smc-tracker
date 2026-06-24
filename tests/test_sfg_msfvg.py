"""tests/test_sfg_msfvg.py — SFG MSFVG 反转簇因子确定性测试套件。

测试策略（TDD，CLAUDE.md §四）：
  1. 黄金平价：spec parity_notes 中的闭合数值 oracle（3 个因子标量）
  2. 符号约定：bull-zone-only → 支撑 → 正（看涨反转）；bear-zone-only → 阻力 → 负
  3. warmup 边界：序列长度 < 41 → series 全 NaN；msfvg_factor → None
  4. 输出范围 [-1, 1] + 有限性 + NaN 哨兵
  5. no-repaint 检验：前缀不变性（追加新极端 bar 不改已发射的早期值）
  6. 无 lookahead：FVG 事件只用 h/l[-3] vs h/l[-1]（bar i-3 vs i-1），当前bar更新zone
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from smc_tracker.indicators.sfg.msfvg import msfvg_series, msfvg_factor


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：合成 Candle 对象
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


def _flat_candle(price: float, spread: float = 0.1) -> _Candle:
    """构造一根中性平稳 K 线（不产生 FVG）。"""
    return _Candle(price, price + spread, price - spread, price)


def _make_flat_candles(n: int, base: float = 100.0, spread: float = 0.1) -> list[_Candle]:
    """生成 n 根平稳无 FVG 的 K 线（close=base, 范围 [base-spread, base+spread]）。"""
    return [_flat_candle(base, spread) for _ in range(n)]


def _make_bull_fvg_candles(
    base: float = 100.0,
    prefix_n: int = 45,
    gap_size: float = 3.0,
    close_after: float | None = None,
) -> list[_Candle]:
    """构造含一个看涨 FVG 的序列（用于测试 bull-zone-only 分支）。

    FVG 事件触发条件（bar-index 基于原始历史顺序，i-3 < i-1 算下标偏移）：
      bull_fvg = h[i-3] < l[i-1]  （最新bar为 i）
    构造：在序列末尾插入 3 根特殊 bar 触发 bull FVG：
      bar i-3: high=base,   low=base-1
      bar i-2: high=base+5, low=base+4   (中间bar，无约束)
      bar i-1: low=base+gap_size          (l[i-1] > h[i-3]=base => gap)
      bar i  : close=close_after (当前处理bar, 决定 zone fill/shrink)
    prefix_n 根平稳 bar 在前（提供 warmup 期超过 41 根）。
    """
    if close_after is None:
        close_after = base + gap_size + 1.0  # 收盘高于 FVG zone => 不填充

    candles: list[_Candle] = []
    # 前缀平稳 bar
    for _ in range(prefix_n):
        candles.append(_flat_candle(base))
    # 触发 bull FVG 的 3 根 bar（位于 i-3, i-2, i-1）
    # bar i-3: high=base, low=base-1  => h[i-3] = base
    candles.append(_Candle(base - 0.5, base, base - 1.0, base - 0.5))
    # bar i-2: 中间 bar（随意，不参与 FVG 条件）
    candles.append(_Candle(base + 4.0, base + 5.0, base + 4.0, base + 4.5))
    # bar i-1: low = base + gap_size => l[i-1] = base + gap_size > h[i-3] = base => bull FVG
    low_i1 = base + gap_size
    candles.append(_Candle(low_i1 + 0.5, low_i1 + 2.0, low_i1, low_i1 + 1.0))
    # bar i (current): close=close_after, zone 处理在 bar i
    # zone: top=l[i-1]=base+gap_size, bottom=h[i-3]=base
    # bar i 的 low 不能 <= bottom(=base) 否则 zone 被全填
    candles.append(_Candle(close_after, close_after + 0.5, close_after - 0.3, close_after))
    return candles


def _make_bear_fvg_candles(
    base: float = 100.0,
    prefix_n: int = 45,
    gap_size: float = 3.0,
    close_after: float | None = None,
) -> list[_Candle]:
    """构造含一个看跌 FVG 的序列（用于测试 bear-zone-only 分支）。

    bear_fvg = l[i-3] > h[i-1]  （最新bar为 i）
    构造：
      bar i-3: low=base,    high=base+1
      bar i-2: 中间 bar（无约束）
      bar i-1: high=base-gap_size  => h[i-1] < l[i-3]=base => bear FVG
      bar i  : close=close_after
    bear zone: top=l[i-3]=base, bottom=h[i-1]=base-gap_size
    """
    if close_after is None:
        close_after = base - gap_size - 1.0  # 收盘低于 zone => 不填充

    candles: list[_Candle] = []
    for _ in range(prefix_n):
        candles.append(_flat_candle(base))
    # bar i-3: low=base, high=base+1
    candles.append(_Candle(base + 0.5, base + 1.0, base, base + 0.5))
    # bar i-2: 中间 bar
    candles.append(_Candle(base - 4.0, base - 4.0, base - 5.0, base - 4.5))
    # bar i-1: high=base-gap_size  => h[i-1] = base-gap_size < l[i-3]=base => bear FVG
    high_i1 = base - gap_size
    candles.append(_Candle(high_i1 - 0.5, high_i1, high_i1 - 2.0, high_i1 - 1.0))
    # bar i: close=close_after, bar i high 不能 >= zone.top(=base) 否则 zone 被全填
    candles.append(_Candle(close_after, close_after + 0.3, close_after - 0.5, close_after))
    return candles


# ─────────────────────────────────────────────────────────────────────────────
# 测试 0: 编译+导入检查
# ─────────────────────────────────────────────────────────────────────────────

class TestImport:
    def test_can_import_msfvg_series(self):
        from smc_tracker.indicators.sfg.msfvg import msfvg_series
        assert callable(msfvg_series)

    def test_can_import_msfvg_factor(self):
        from smc_tracker.indicators.sfg.msfvg import msfvg_factor
        assert callable(msfvg_factor)


# ─────────────────────────────────────────────────────────────────────────────
# 测试 1: warmup 边界 (n < 41 = 2*swing_size+1)
# ─────────────────────────────────────────────────────────────────────────────

class TestWarmup:
    def test_series_all_nan_when_insufficient(self):
        """n < 41 时 msfvg_series 全部返回 nan。"""
        candles = _make_flat_candles(n=40)
        s = msfvg_series(candles)
        assert len(s) == 40
        assert np.all(np.isnan(s)), f"n=40 时 series 应全 NaN，实际有限值索引={np.where(np.isfinite(s))[0]}"

    def test_series_all_nan_empty(self):
        s = msfvg_series([])
        assert len(s) == 0

    def test_factor_none_when_insufficient(self):
        """n < 41 时 msfvg_factor 应返回 None。"""
        candles = _make_flat_candles(n=40)
        result = msfvg_factor(candles)
        assert result is None, f"n=40 时应返回 None，实际={result}"

    def test_factor_none_on_empty(self):
        assert msfvg_factor([]) is None

    def test_series_length_matches_input(self):
        """series 长度必须等于输入 candle 数。"""
        for n in [0, 10, 40, 41, 100]:
            candles = _make_flat_candles(n=n)
            s = msfvg_series(candles)
            assert len(s) == n, f"n={n} 时 series 长度={len(s)} 不等于输入"

    def test_warmup_prefix_nan_then_may_have_value(self):
        """n >= 41 后，前 40 根仍为 NaN（warmup guard）；之后可以有值（视 FVG 是否出现）。"""
        # 纯平稳序列：虽然 n >= 41 但无 FVG => 全 NaN（fail-closed）
        candles = _make_flat_candles(n=60)
        s = msfvg_series(candles)
        assert np.all(np.isnan(s[:40])), "前 40 根必须全 NaN"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 2: 黄金平价 — spec parity_notes 闭合数值 oracle
# ─────────────────────────────────────────────────────────────────────────────
#
# 来自 continuous_factors.rs 测试 lines 1185-1206:
#   bull-only:  top=98, bot=95, close=100 -> (98-100+3)/(2*3) = +0.16667
#   bear-only:  top=105, bot=102, close=100 -> -(100-102+3)/(2*3) = -0.16667
#   both-zones: level_factor(close=100, SL=bull_top=98, RL=bear_bot=102)
#               mid=(98+102)/2=100, half=(102-98)/2=2
#               f = (100-100)/2 = 0.0  (close at midpoint -> neutral)
#   neither:    NaN (fail-closed)

class TestGoldenParity:
    """直接调用 _factor_scalar() 内部纯函数，验证闭合数值。

    spec 给出了因子标量公式，这里直接用公式计算，无需构造完整 candle 序列。
    """

    def test_bull_only_parity(self):
        """bull-only: top=98, bot=95, close=100 -> +1/6 ≈ 0.16667。

        factor = clamp((bull_top - close + half)/(2*half))
              = (98 - 100 + 3) / (2 * 3) = 1/6
        """
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=100.0, bull_top=98.0, bull_bot=95.0,
                           bear_top=math.nan, bear_bot=math.nan)
        expected = 1.0 / 6.0
        assert math.isfinite(f), f"bull-only 结果不应为 NaN，实际={f}"
        assert math.isclose(f, expected, rel_tol=1e-6), (
            f"bull-only 期望 {expected:.6f}，实际 {f:.6f}"
        )

    def test_bear_only_parity(self):
        """bear-only: top=105, bot=102, close=100 -> -1/6 ≈ -0.16667。

        factor = clamp(-(close - bear_bot + half)/(2*half))
               = -(100 - 102 + 3) / (2*3) = -1/6
        """
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=100.0, bull_top=math.nan, bull_bot=math.nan,
                           bear_top=105.0, bear_bot=102.0)
        expected = -1.0 / 6.0
        assert math.isfinite(f), f"bear-only 结果不应为 NaN，实际={f}"
        assert math.isclose(f, expected, rel_tol=1e-6), (
            f"bear-only 期望 {expected:.6f}，实际 {f:.6f}"
        )

    def test_both_zones_midpoint(self):
        """both-zones: close=midpoint -> factor=0.0（中性）。

        level_factor(close=100, SL=bull_top=98, RL=bear_bot=102):
          mid=100, half=2 => (100-100)/2 = 0
        """
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=100.0, bull_top=98.0, bull_bot=95.0,
                           bear_top=105.0, bear_bot=102.0)
        assert math.isfinite(f), f"both-zones midpoint 不应为 NaN，实际={f}"
        assert math.isclose(f, 0.0, abs_tol=1e-9), (
            f"both-zones close=midpoint 期望 0.0，实际 {f:.9f}"
        )

    def test_neither_zone_nan(self):
        """neither: NaN (fail-closed)。"""
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=100.0, bull_top=math.nan, bull_bot=math.nan,
                           bear_top=math.nan, bear_bot=math.nan)
        assert math.isnan(f), f"neither-zone 应返回 NaN，实际={f}"

    def test_bull_only_at_top_is_zero(self):
        """bull-only: close==bull_top => factor=(top-top+half)/(2*half)=0.5, clamp->0.5。"""
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        # top=98, bot=95, half=3; close=98(=top) => (0+3)/6 = 0.5
        f = _factor_scalar(close=98.0, bull_top=98.0, bull_bot=95.0,
                           bear_top=math.nan, bear_bot=math.nan)
        assert math.isfinite(f)
        assert math.isclose(f, 0.5, rel_tol=1e-6), f"实际={f}"

    def test_both_zones_at_bull_top_positive(self):
        """both-zones: close=bull_top=98 => level_factor -> +1 方向（接近+1）。

        mid=(98+102)/2=100, half=2; close=98 => (100-98)/2 = 1.0 -> clamp=+1
        """
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=98.0, bull_top=98.0, bull_bot=95.0,
                           bear_top=105.0, bear_bot=102.0)
        assert math.isfinite(f)
        assert math.isclose(f, 1.0, rel_tol=1e-6), f"实际={f}"

    def test_both_zones_at_bear_bot_negative(self):
        """both-zones: close=bear_bot=102 => level_factor -> -1（close 在阻力区）。

        mid=100, half=2; close=102 => (100-102)/2 = -1 -> clamp=-1
        """
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=102.0, bull_top=98.0, bull_bot=95.0,
                           bear_top=105.0, bear_bot=102.0)
        assert math.isfinite(f)
        assert math.isclose(f, -1.0, rel_tol=1e-6), f"实际={f}"

    def test_degenerate_bull_zero_width_nan(self):
        """bull zone 宽度=0 (top==bot) => NaN (fail-closed)。"""
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=100.0, bull_top=98.0, bull_bot=98.0,
                           bear_top=math.nan, bear_bot=math.nan)
        assert math.isnan(f), f"宽度=0 应返回 NaN，实际={f}"

    def test_degenerate_bear_zero_width_nan(self):
        """bear zone 宽度=0 => NaN (fail-closed)。"""
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=100.0, bull_top=math.nan, bull_bot=math.nan,
                           bear_top=102.0, bear_bot=102.0)
        assert math.isnan(f), f"宽度=0 应返回 NaN，实际={f}"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 3: 符号约定（sign convention）
# ─────────────────────────────────────────────────────────────────────────────

class TestSignConvention:
    """反转簇因子符号约定（spec output_range）：
      +1 = price 在支撑区 (bull FVG 在下方) => 预期看涨反转
      -1 = price 在阻力区 (bear FVG 在上方) => 预期看跌反转
    """

    def test_bull_zone_close_above_and_near_zone_positive(self):
        """bull FVG 在价格略下方（支撑），close < bull_top + half => 因子 > 0。

        bull-only 公式: (bull_top - close + half) / (2*half)
        factor > 0 当 close < bull_top + half。
        用 spec golden parity 数值: top=98, bot=95, close=100 => factor=1/6 > 0。
        100 < 98+3=101 => 满足 > 0 条件。
        """
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=100.0, bull_top=98.0, bull_bot=95.0,
                           bear_top=math.nan, bear_bot=math.nan)
        assert f > 0, f"close=100, bull top=98(下方), factor 应>0，实际={f}"

    def test_bear_zone_close_below_and_near_zone_negative(self):
        """bear FVG 在价格略上方（阻力），close > bear_bot - half => 因子 < 0。

        bear-only 公式: -(close - bear_bot + half) / (2*half)
        factor < 0 当 close > bear_bot - half。
        用 spec golden parity 数值: top=105, bot=102, close=100 => factor=-1/6 < 0。
        100 > 102-3=99 => 满足 < 0 条件。
        """
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        f = _factor_scalar(close=100.0, bull_top=math.nan, bull_bot=math.nan,
                           bear_top=105.0, bear_bot=102.0)
        assert f < 0, f"close=100, bear bot=102(上方), factor 应<0，实际={f}"

    def test_bull_zone_when_close_in_zone_smaller(self):
        """close 进入 bull zone 内 => factor 更小（趋向0或负，zone被部分消化）。"""
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        # close=100, zone top=102, bot=95 => close 在 zone 内
        f_in = _factor_scalar(close=100.0, bull_top=102.0, bull_bot=95.0,
                              bear_top=math.nan, bear_bot=math.nan)
        # close=105, zone top=102, bot=95 => close 高于 zone（强支撑下方）
        f_above = _factor_scalar(close=105.0, bull_top=102.0, bull_bot=95.0,
                                 bear_top=math.nan, bear_bot=math.nan)
        # f_in 应 > f_above（close越远离top，factor越大因为(top-close+half)更大）
        # 实际: f_in=(102-100+3.5)/(2*3.5)=5.5/7≈0.786; f_above=(102-105+3.5)/7=0.5/7≈0.071
        assert f_in > f_above, f"close更接近top时factor更大: f_in={f_in:.3f}, f_above={f_above:.3f}"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 4: 输出范围 [-1, 1] + NaN哨兵
# ─────────────────────────────────────────────────────────────────────────────

class TestOutputRange:
    def test_factor_scalar_within_range(self):
        """_factor_scalar 对任意有效输入应在 [-1, 1]。"""
        from smc_tracker.indicators.sfg.msfvg import _factor_scalar
        test_cases = [
            (100.0, 98.0, 95.0, float("nan"), float("nan")),
            (100.0, float("nan"), float("nan"), 105.0, 102.0),
            (100.0, 98.0, 95.0, 105.0, 102.0),
            (98.0, 98.0, 95.0, float("nan"), float("nan")),
            (95.0, 98.0, 95.0, float("nan"), float("nan")),  # close at bottom
        ]
        for close, bt, bb, at, ab in test_cases:
            f = _factor_scalar(close, bt, bb, at, ab)
            if math.isfinite(f):
                assert -1.0 <= f <= 1.0, f"factor={f} 超出 [-1,1]，参数={close,bt,bb,at,ab}"

    def test_series_all_finite_in_range(self):
        """series 中所有有限值应在 [-1, 1]。"""
        candles = _make_bull_fvg_candles(prefix_n=50)
        s = msfvg_series(candles)
        finite_vals = s[np.isfinite(s)]
        if len(finite_vals) > 0:
            assert np.all(finite_vals >= -1.0) and np.all(finite_vals <= 1.0), (
                f"series 有限值超出 [-1,1]：min={finite_vals.min():.4f}, max={finite_vals.max():.4f}"
            )

    def test_nan_sentinel_no_impute(self):
        """NaN 哨兵不得被 impute 为 0（无 FVG 区域时必须是 NaN，不是 0）。"""
        # 纯平稳序列 => 无 FVG => series 全 NaN
        candles = _make_flat_candles(n=60)
        s = msfvg_series(candles)
        # 期望全是 NaN，不允许出现 0.0
        assert not np.any(s == 0.0), "无 FVG 时 series 不应有 0.0（应为 NaN，fail-closed）"

    def test_series_is_float_array(self):
        """series 应返回 np.ndarray，dtype=float。"""
        candles = _make_flat_candles(n=50)
        s = msfvg_series(candles)
        assert isinstance(s, np.ndarray)
        assert np.issubdtype(s.dtype, np.floating)


# ─────────────────────────────────────────────────────────────────────────────
# 测试 5: no-repaint / prefix-invariance（关键防护）
# ─────────────────────────────────────────────────────────────────────────────

class TestNoRepaint:
    """核心 no-lookahead 保证：
    对任意已发射位置 i，追加更极端的新 bar 后 series[i] 不变。
    这钉死了 FVG zone 状态机的前向增量语义。
    """

    def test_prefix_invariance_plain_series(self):
        """基础 prefix 不变性：前 N 根 series 值追加 bar 后不变。"""
        base_candles = _make_flat_candles(n=60)
        s_base = msfvg_series(base_candles)

        # 追加 10 根极端 bar（高波动，确保不影响已发射值）
        extreme_candles = base_candles + [
            _Candle(50.0, 200.0, 10.0, 150.0) for _ in range(10)
        ]
        s_extended = msfvg_series(extreme_candles)

        # 前 len(base_candles) 根 series 值必须完全不变
        prefix_base = s_base
        prefix_ext = s_extended[:len(base_candles)]
        # 允许两者都是 NaN（同位置），或数值相同
        for i in range(len(base_candles)):
            b = prefix_base[i]
            e = prefix_ext[i]
            if math.isnan(b):
                assert math.isnan(e), (
                    f"位置 {i}: 基础为 NaN，扩展后变为 {e:.4f}（repaint!）"
                )
            else:
                assert math.isclose(b, e, rel_tol=1e-9, abs_tol=1e-12), (
                    f"位置 {i}: 基础={b:.6f}，扩展后={e:.6f}（repaint!）"
                )

    def test_prefix_invariance_with_fvg(self):
        """含 FVG 序列的前缀不变性（最重要的 no-lookahead 测试）。"""
        # 构造有 FVG 的 base 序列
        base_candles = _make_bull_fvg_candles(prefix_n=48, base=100.0, gap_size=3.0)
        s_base = msfvg_series(base_candles)

        # 追加极端 bar（低于 zone bottom，会触发 zone 消除）
        extreme_low = 90.0  # 远低于 bull zone bottom=100.0
        new_bar = _Candle(extreme_low, extreme_low + 0.5, extreme_low - 2.0, extreme_low)
        extended_candles = base_candles + [new_bar] * 5
        s_extended = msfvg_series(extended_candles)

        n = len(base_candles)
        for i in range(n):
            b = s_base[i]
            e = s_extended[i]
            if math.isnan(b):
                assert math.isnan(e), (
                    f"位置 {i}: 基础为 NaN，追加极端 bar 后变为 {e:.4f}（repaint!）"
                )
            else:
                assert math.isclose(b, e, rel_tol=1e-9, abs_tol=1e-12), (
                    f"位置 {i}: 基础={b:.6f}，追加极端 bar 后={e:.6f}（repaint!）"
                )

    def test_new_bar_does_not_rewrite_history(self):
        """FVG event 检测只用 h/l 相对偏移，当前 bar 的处理不回写历史值。

        这验证 CAVEAT 3：fill/shrink 增量前向处理。
        """
        candles = _make_bear_fvg_candles(prefix_n=48, base=100.0, gap_size=3.0)
        s_before = msfvg_series(candles)

        # 追加一根高价 bar，会触发 bear zone fill（high >= zone top）
        fill_bar = _Candle(110.0, 115.0, 109.0, 112.0)  # high > bear zone top=100
        s_after = msfvg_series(candles + [fill_bar])

        n = len(candles)
        for i in range(n):
            b = s_before[i]
            e = s_after[i]
            if math.isnan(b):
                assert math.isnan(e), f"位置 {i} repaint: NaN->有限"
            else:
                assert math.isclose(b, e, rel_tol=1e-9, abs_tol=1e-12), (
                    f"位置 {i} repaint: {b:.6f}->{e:.6f}"
                )


# ─────────────────────────────────────────────────────────────────────────────
# 测试 6: FVG 事件逻辑正确性
# ─────────────────────────────────────────────────────────────────────────────

class TestFvgEventLogic:
    """验证 FVG 事件检测的 bar-index 语义（h[i-3]<l[i-1] / l[i-3]>h[i-1]）。"""

    def test_bull_fvg_detected(self):
        """看涨 FVG：h[i-3] < l[i-1] => 产生 bull zone，因子 > 0。

        bull-only 公式: (bull_top - close + half) / (2*half)
        factor > 0 的条件: close < bull_top + half（即 close 不能超过 zone top + zone width）。
        构造 close = bull_top + 0.5*half = base+gap + 0.5*gap = 100+3+1.5 = 104.5。
        zone: top=l[i-1]=103, bottom=h[i-3]=100.1(flat bar high); half≈2.9
        close=104 < 105.9=top+half => factor > 0。
        """
        # close_after=104: zone top≈103, half≈2.9 => (103-104+2.9)/(5.8) = 1.9/5.8 > 0
        candles = _make_bull_fvg_candles(prefix_n=48, close_after=104.0)
        result = msfvg_factor(candles)
        assert result is not None, "含 bull FVG 的足够序列，factor 不应为 None"
        assert result > 0, f"bull zone 支撑（close 在 zone 附近上方）=> factor>0，实际={result:.4f}"

    def test_bear_fvg_detected(self):
        """看跌 FVG：l[i-3] > h[i-1] => 产生 bear zone，因子 < 0。

        bear-only 公式: -(close - bear_bot + half) / (2*half)
        factor < 0 的条件: close > bear_bot - half。
        构造 close = bear_bot - 0.5*half：价格在 zone 附近下方，factor < 0。
        zone: top=l[i-3]=100, bottom=h[i-1]=97(high_i1=100-3=97); half≈3
        close=96 < 97-3=94 不行；用 close=98 => -(98-97+3)/(6) = -4/6 < 0。
        """
        # close_after=96: zone top=100(l[i-3]=base=100), bot≈97-gap; close slightly below zone
        # bear zone top=l[i-3]=base=100, bottom=h[i-1]=base-gap_size=97
        # half≈3; close=96: -(96-97+3)/(6) = -(2)/6 = -0.333 < 0
        candles = _make_bear_fvg_candles(prefix_n=48, close_after=96.0)
        result = msfvg_factor(candles)
        assert result is not None, "含 bear FVG 的足够序列，factor 不应为 None"
        assert result < 0, f"bear zone 阻力（close 在 zone 附近下方）=> factor<0，实际={result:.4f}"

    def test_no_fvg_returns_nan(self):
        """无 FVG（纯平稳序列）=> 全 NaN，factor None。"""
        candles = _make_flat_candles(n=60)
        result = msfvg_factor(candles)
        assert result is None, f"无 FVG 时 factor 应为 None，实际={result}"

    def test_flat_series_all_nan(self):
        """无 FVG 纯平稳 => series 应全 NaN（fail-closed）。"""
        candles = _make_flat_candles(n=100)
        s = msfvg_series(candles)
        finite_count = np.sum(np.isfinite(s))
        assert finite_count == 0, f"纯平稳序列 series 应全 NaN，实际有 {finite_count} 个有限值"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 7: FVG zone 状态机（fill/shrink/FIFO cap）
# ─────────────────────────────────────────────────────────────────────────────

class TestZoneStateMachine:
    def test_bull_zone_fills_when_low_at_bottom(self):
        """bull zone 被完全填充（low <= bottom），之后无 bull zone => factor NaN 或 bear-only。"""
        # bull zone: top=l[i-1]=103, bottom=h[i-3]=100
        # 追加一根 low=99 < bottom=100 的 bar => zone 被删除
        candles = _make_bull_fvg_candles(
            prefix_n=48, base=100.0, gap_size=3.0,
            close_after=106.0  # 第一个处理 bar，close 在 zone 上方
        )
        # 追加 bar：low <= bottom=100 => zone fully filled
        fill_bar = _Candle(103.0, 104.0, 99.0, 100.0)  # low=99 < bottom=100
        candles_with_fill = candles + [fill_bar]
        s = msfvg_series(candles_with_fill)
        # 最后一个 bar 的 factor：bull zone 已被删，无 zone => NaN 或有 bear zone
        last = s[-1]
        # 没有 bear zone，所以应为 NaN
        assert math.isnan(last), (
            f"bull zone 被删后最后一个 bar 应为 NaN（无 zone），实际={last:.4f}"
        )

    def test_bull_zone_shrinks_on_partial_mitigation(self):
        """bull zone shrink_mitigated: low < top => top 收缩到 low，zone 保留。"""
        # bull zone: top=103, bottom=100
        # bar close_after: low=102 < top=103 => shrink top to 102, zone stays
        candles = _make_bull_fvg_candles(
            prefix_n=48, base=100.0, gap_size=3.0,
            close_after=105.0
        )
        shrink_bar = _Candle(104.0, 106.0, 102.0, 105.0)  # low=102 < top=103 => shrink
        candles_with_shrink = candles + [shrink_bar]
        s = msfvg_series(candles_with_shrink)
        # shrink 后 zone 仍存在，应有有限值
        last = s[-1]
        assert math.isfinite(last), (
            f"部分 mitigation 后 zone 仍在，应有有限 factor，实际={last}"
        )

    def test_fifo_cap_evicts_oldest(self):
        """FIFO cap(fvg_history+1=8) 满时最旧 zone 被驱逐。

        构造 >8 个 FVG 事件，确保实现不崩溃（list 长度受控）。
        """
        candles: list[_Candle] = _make_flat_candles(n=45)
        # 插入 10 次 bull FVG 事件（每次需要 4 根 bar）
        base = 100.0
        for k in range(10):
            gap = 3.0 + k * 0.1
            # bar i-3
            candles.append(_Candle(base - 0.5, base, base - 1.0, base - 0.5))
            # bar i-2
            candles.append(_Candle(base + 4.0, base + 5.0, base + 4.0, base + 4.5))
            # bar i-1: low = base + gap > h[i-3] = base => bull FVG
            low_i1 = base + gap
            candles.append(_Candle(low_i1 + 0.5, low_i1 + 2.0, low_i1, low_i1 + 1.0))
            # bar i: current
            close_bar = base + gap + 2.0
            candles.append(_Candle(close_bar, close_bar + 0.5, close_bar - 0.3, close_bar))

        # 不崩溃，返回正确长度的 series
        s = msfvg_series(candles)
        assert len(s) == len(candles), "FIFO cap 测试：series 长度必须等于输入"
        # 最后一个应为有限值（最近几个 zone 存活）
        last = s[-1]
        assert math.isfinite(last), f"多 FVG 序列最后一个 factor 应有限，实际={last}"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 8: msfvg_factor 末值消费
# ─────────────────────────────────────────────────────────────────────────────

class TestFactorScalar:
    def test_factor_matches_last_finite_series(self):
        """msfvg_factor 应返回 msfvg_series 最后一个有限值。"""
        candles = _make_bull_fvg_candles(prefix_n=48)
        s = msfvg_series(candles)
        f = msfvg_factor(candles)

        finite_vals = s[np.isfinite(s)]
        if len(finite_vals) == 0:
            assert f is None, f"series 全 NaN 时 factor 应 None，实际={f}"
        else:
            expected = float(finite_vals[-1])
            assert f is not None
            assert math.isclose(f, expected, rel_tol=1e-9), (
                f"factor={f:.6f}，series 末尾有限值={expected:.6f}"
            )

    def test_factor_is_float_when_valid(self):
        """有效时 msfvg_factor 应返回 float。"""
        candles = _make_bull_fvg_candles(prefix_n=48)
        f = msfvg_factor(candles)
        if f is not None:
            assert isinstance(f, float), f"factor 应为 float，实际={type(f)}"

    def test_factor_none_when_all_nan(self):
        """series 全 NaN 时 factor 应为 None（不返回 nan 也不抛异常）。"""
        candles = _make_flat_candles(n=60)  # 无 FVG => 全 NaN
        f = msfvg_factor(candles)
        assert f is None, f"无 FVG 全 NaN 时 factor 应 None，实际={f}"

    def test_factor_in_range(self):
        """factor 如果有值，必须在 [-1, 1]。"""
        candles = _make_bull_fvg_candles(prefix_n=48)
        f = msfvg_factor(candles)
        if f is not None:
            assert -1.0 <= f <= 1.0, f"factor={f:.4f} 超出 [-1,1]"


# ─────────────────────────────────────────────────────────────────────────────
# 测试 9: 参数化 — fvg_history cap 改变后行为一致
# ─────────────────────────────────────────────────────────────────────────────

class TestParams:
    def test_fvg_history_one_limits_zones(self):
        """fvg_history=1 => cap=2，只保留最近 2 个 zone，不崩溃。"""
        candles = _make_bull_fvg_candles(prefix_n=48)
        s = msfvg_series(candles, fvg_history=1)
        assert len(s) == len(candles)
        # 可能 NaN 或有限，不崩溃即可
        assert isinstance(s, np.ndarray)

    def test_swing_size_only_affects_warmup(self):
        """swing_size 仅影响 warmup 长度（2*swing_size+1），不影响 FVG 检测。"""
        candles_short = _make_flat_candles(n=30)
        s10 = msfvg_series(candles_short, swing_size=10)  # warmup = 21
        s20 = msfvg_series(candles_short, swing_size=20)  # warmup = 41 > 30
        # swing_size=20 => 全 NaN（n<41）
        assert np.all(np.isnan(s20)), f"swing_size=20, n=30 应全 NaN"
        # swing_size=10 => warmup=21，n=30>=21 可发射（但无 FVG => 仍全 NaN）
        assert len(s10) == 30

    def test_default_params_match_spec(self):
        """默认参数应与 spec 一致：swing_size=20, fvg_history=7, shrink_mitigated=True。"""
        import inspect
        sig = inspect.signature(msfvg_series)
        params = sig.parameters
        assert params["swing_size"].default == 20
        assert params["fvg_history"].default == 7
        assert params["shrink_mitigated"].default is True


# ─────────────────────────────────────────────────────────────────────────────
# 测试 10: 生产路径黄金断言 — 直接调用 msfvg_series/msfvg_factor（修 2）
# ─────────────────────────────────────────────────────────────────────────────
#
# 解决 WF4 审计发现的问题：原 TestGoldenParity 仅调用 _factor_scalar 内部纯函数，
# 不 exercise 生产路径（msfvg_series/msfvg_factor）。本 class 构造已知合成序列，
# 直接断言 msfvg_series/msfvg_factor 的真实输出值，确保 factor-of-2 等 bug 会失败。

class TestProductionPathGolden:
    """msfvg_series/msfvg_factor 生产路径数值正确性（已知输入→已知输出）。

    公式（bull-only, close=bull_top）:
      zone: top=103.0, bottom=100.0, half=3.0
      factor = (bull_top - close + half) / (2*half) = (103.0-103.0+3.0)/(6.0) = 0.5

    公式（bear-only, close=bear_bot）:
      zone: top=99.9, bottom=97.0, half=2.9
      factor = -(close - bear_bot + half)/(2*half) = -(97.0-97.0+2.9)/(5.8) = -0.5
    """

    def _make_bull_fvg_known(self) -> list:
        """构造 bull FVG 序列，zone 边界确定已知：top=103.0, bottom=100.0, half=3.0。

        序列结构（共 45 根 bar，swing_size=20 默认，min_bars=41）：
          bars 0..40: 平稳 base=100（h=100.1, l=99.9）
          bar 41: h=100.0（将成为 h[i-3] at i=44，h[41]=100.0）
          bar 42: 中间 bar（不参与 FVG 条件）
          bar 43: l=103.0（将成为 l[i-1] at i=44，l[43]=103.0 > h[41]=100.0 => bull FVG）
          bar 44: current close=103.0, l=103.5（l > top=103.0 不触发 shrink/fill）
            zone: top=103.0, bottom=100.0, half=3.0
            factor = (103.0-103.0+3.0)/(6.0) = 3.0/6.0 = 0.5
        """
        base = 100.0
        candles = []
        # bars 0..40: 平稳
        for _ in range(41):
            candles.append(_Candle(base, base + 0.1, base - 0.1, base))
        # bar 41: h=100.0（作为 h[i-3]）
        candles.append(_Candle(base, base, base - 0.1, base))
        # bar 42: 中间
        candles.append(_Candle(102.0, 104.0, 102.0, 103.0))
        # bar 43: l=103.0（作为 l[i-1]），l[43]=103.0 > h[41]=100.0 => bull FVG
        candles.append(_Candle(103.5, 105.0, 103.0, 104.0))
        # bar 44: current; close=103.0, l=103.5 > top=103.0 => 不触发 shrink/fill
        candles.append(_Candle(103.0, 103.5, 103.5, 103.0))
        return candles

    def _make_bear_fvg_known(self) -> list:
        """构造 bear FVG 序列，zone 边界确定已知：top=99.9, bottom=97.0, half=2.9。

        序列结构（共 45 根 bar）：
          bars 0..40: 平稳 base=100（l=99.9）
          bar 41: l=99.9（将成为 l[i-3] at i=44，l[41]=99.9）
          bar 42: 中间 bar
          bar 43: h=97.0（将成为 h[i-1] at i=44，l[41]=99.9 > h[43]=97.0 => bear FVG）
          bar 44: current close=97.0, h=97.0（h[44]=97.0 == bottom=97.0，不触发 partial mit）
            zone: top=99.9, bottom=97.0, half=2.9
            factor = -(97.0-97.0+2.9)/(5.8) = -2.9/5.8 = -0.5
        """
        base = 100.0
        candles = []
        for _ in range(41):
            candles.append(_Candle(base, base + 0.1, base - 0.1, base))
        # bar 41: l=99.9（作为 l[i-3]）
        candles.append(_Candle(base, base + 0.1, base - 0.1, base))
        # bar 42: 中间
        candles.append(_Candle(96.0, 97.5, 94.0, 96.0))
        # bar 43: h=97.0（作为 h[i-1]），l[41]=99.9 > h[43]=97.0 => bear FVG
        candles.append(_Candle(96.5, 97.0, 95.0, 96.5))
        # bar 44: current close=97.0, h=97.0（h == bottom，条件 h > bottom=97.0 不满足 => 不 shrink）
        candles.append(_Candle(97.0, 97.0, 96.5, 97.0))
        return candles

    def test_bull_fvg_known_series_exact(self):
        """msfvg_series 生产路径：bull FVG only, close=bull_top => factor=0.5（精确）。

        zone top=103.0, bot=100.0, half=3.0; close=103.0:
          (103.0 - 103.0 + 3.0) / (2*3.0) = 3.0/6.0 = 0.5
        """
        candles = self._make_bull_fvg_known()
        s = msfvg_series(candles)
        last = s[-1]
        assert math.isfinite(last), f"bull FVG known: 最后一根应有限，实际={last}"
        assert math.isclose(last, 0.5, rel_tol=1e-9), (
            f"bull FVG known: 期望 0.5，实际={last:.8f}"
        )

    def test_bull_fvg_known_factor_exact(self):
        """msfvg_factor 生产路径：bull FVG only, close=bull_top => 0.5。"""
        candles = self._make_bull_fvg_known()
        f = msfvg_factor(candles)
        assert f is not None, "bull FVG known: factor 应非 None"
        assert math.isclose(f, 0.5, rel_tol=1e-9), (
            f"bull FVG known: msfvg_factor 期望 0.5，实际={f:.8f}"
        )

    def test_bear_fvg_known_series_exact(self):
        """msfvg_series 生产路径：bear FVG only, close=bear_bot => factor=-0.5（精确）。

        zone top=99.9, bot=97.0, half=2.9; close=97.0:
          -(97.0 - 97.0 + 2.9) / (2*2.9) = -2.9/5.8 = -0.5
        """
        candles = self._make_bear_fvg_known()
        s = msfvg_series(candles)
        last = s[-1]
        assert math.isfinite(last), f"bear FVG known: 最后一根应有限，实际={last}"
        assert math.isclose(last, -0.5, rel_tol=1e-9), (
            f"bear FVG known: 期望 -0.5，实际={last:.8f}"
        )

    def test_bear_fvg_known_factor_exact(self):
        """msfvg_factor 生产路径：bear FVG only, close=bear_bot => -0.5。"""
        candles = self._make_bear_fvg_known()
        f = msfvg_factor(candles)
        assert f is not None, "bear FVG known: factor 应非 None"
        assert math.isclose(f, -0.5, rel_tol=1e-9), (
            f"bear FVG known: msfvg_factor 期望 -0.5，实际={f:.8f}"
        )

    def test_production_path_differs_from_internal_only(self):
        """确认生产路径与单独调用 _factor_scalar 的结合覆盖正确（缺陷检测确认）。

        若 msfvg_series 内部有 factor-of-2 错误（如除 half 而非 2*half），
        则 msfvg_series 输出 1.0 但 _factor_scalar 输出 0.5。
        本测试确保此类 bug 被捕获：断言 msfvg_series()[-1] == 0.5（非 1.0）。
        """
        candles = self._make_bull_fvg_known()
        # 如果有 factor-of-2 bug，这里会失败
        s = msfvg_series(candles)
        assert not math.isclose(s[-1], 1.0, rel_tol=1e-6), (
            "factor-of-2 检测：结果不应为 1.0（若为 1.0 说明除法分母有 bug）"
        )
        assert math.isclose(s[-1], 0.5, rel_tol=1e-9), (
            f"生产路径 factor-of-2 缺陷检测：期望 0.5，实际={s[-1]:.8f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 测试 11: warmup 修复验证 — 早期 FVG zone 在整批守卫通过后正确发射（修 1）
# ─────────────────────────────────────────────────────────────────────────────
#
# WF4 审计发现：原代码 per-row `if i < warmup: continue` 导致早期 bar（i < 2*swing_size）
# 即使有存活 FVG zone 也不发射。Rust 行为（market_structure_fvg.rs:207-209）只有整批守卫
# (n < 2*swing_size+1 → return [])，无 per-row mask。
# 修后：早期 bar 按 active_zones 是否非空决定 finite/NaN，与 Rust 对齐。

class TestWarmupFix:
    """验证 per-row warmup mask 已移除，早期 FVG zone 可在 i < 2*swing_size 时发射。

    关键：这些早期值保持 prefix-invariance（no-repaint），因为 zone 状态机是严格前向的。
    """

    def _make_early_fvg(self, swing_size: int = 5) -> list:
        """构造 early FVG 测试序列（swing_size=5, min_bars=11）。

        结构（共 11 根 bar）：
          bars 0,1,2: 平稳（h=100.1）
          bar 3: h=100.0（将成为 h[i-3] at i=6）
          bar 4: 中间 bar（l=102.0）
          bar 5: l=103.0（l[5]=103.0 > h[3]=100.0 => bull FVG at i=6）
                 另外：h[2]=100.1 < l[4]=102.0 => bull FVG at i=5 too
          bar 6: current bar（第一个 FVG 检测，zone 从这里开始存活）
          bars 7,8,9,10: 延续平稳 bar（保持 zone 活跃）

        n=11 >= min_bars=11 => 整批守卫通过 => 早期 bar（i=5,6 < warmup=10）应发射有限值。
        """
        candles = []
        for _ in range(3):
            candles.append(_Candle(100.0, 100.1, 99.9, 100.0))
        # bar 3: h=100.0
        candles.append(_Candle(99.5, 100.0, 99.0, 99.5))
        # bar 4: middle, l=102.0
        candles.append(_Candle(102.0, 104.0, 102.0, 103.0))
        # bar 5: l=103.0 (l[5] > h[3]=100.0 => FVG at i=6)
        # Also: h[2]=100.1 < l[4]=102.0 => FVG at i=5
        candles.append(_Candle(103.5, 105.0, 103.0, 104.0))
        # bar 6: close=103.5, l=103.5 > top=103.0 => no fill/shrink
        candles.append(_Candle(103.5, 104.0, 103.5, 103.5))
        # bars 7..10: keep zone alive
        for _ in range(4):
            candles.append(_Candle(103.5, 104.0, 103.5, 103.5))
        return candles

    def test_early_fvg_emits_before_warmup_index(self):
        """修后：swing_size=5 时，bar 5,6（< warmup=10）若有 active zone，应发射有限值。

        bar 5 FVG（h[2]=100.1 < l[4]=102.0）在 i=5 创建 zone，close=104.0 在 zone 上方。
        bar 6 FVG（h[3]=100.0 < l[5]=103.0）在 i=6 创建 zone，close=103.5。
        两者均 < warmup=10，但修后不被 per-row mask 屏蔽。
        """
        candles = self._make_early_fvg(swing_size=5)
        assert len(candles) == 11  # 恰好达到 min_bars=11
        s = msfvg_series(candles, swing_size=5)
        assert len(s) == 11
        # bar 5 和 bar 6（i < warmup=10）应有有限值（因为有 active zone）
        assert math.isfinite(s[5]), (
            f"修后 bar 5（i=5 < warmup=10）应发射有限值，实际={s[5]}"
        )
        assert math.isfinite(s[6]), (
            f"修后 bar 6（i=6 < warmup=10）应发射有限值，实际={s[6]}"
        )
        # 早期值应在 [-1, 1]
        assert -1.0 <= s[5] <= 1.0, f"bar 5 factor={s[5]:.4f} 超出范围"
        assert -1.0 <= s[6] <= 1.0, f"bar 6 factor={s[6]:.4f} 超出范围"

    def test_early_fvg_bar6_value_exact(self):
        """bar 6 的因子值：最近 zone 为 (top=103.0, bot=100.0)，close=103.5。

        nearest zone: top=103.0, bot=100.0, half=3.0
        factor = (103.0 - 103.5 + 3.0) / (2*3.0) = 2.5/6.0 ≈ 0.4167
        （两个 zone 都活跃时取最近那个；与 early-emission 无关，验证数值正确性）
        """
        candles = self._make_early_fvg(swing_size=5)
        s = msfvg_series(candles, swing_size=5)
        expected = 2.5 / 6.0  # (103.0 - 103.5 + 3.0) / 6.0
        assert math.isfinite(s[6]), f"bar 6 应有限，实际={s[6]}"
        assert math.isclose(s[6], expected, rel_tol=1e-9), (
            f"bar 6 期望={expected:.6f}，实际={s[6]:.6f}"
        )

    def test_batch_guard_still_blocks_short_series(self):
        """整批守卫（n < min_bars）在修后仍有效：n=10 < min_bars=11 => 全 NaN。"""
        candles = self._make_early_fvg(swing_size=5)[:-1]  # 去掉最后一根 => n=10 < 11
        assert len(candles) == 10
        s = msfvg_series(candles, swing_size=5)
        assert np.all(np.isnan(s)), (
            f"n=10 < min_bars=11 应全 NaN，有限值索引={np.where(np.isfinite(s))[0]}"
        )

    def test_early_emission_no_repaint(self):
        """早期发射值（i < warmup=10）追加更极端 bar 后不变（no-repaint 护栏）。

        关键：zone 状态机严格前向，append 新 bar 可以 fill/shrink zone（影响新 bar 的输出），
        但不回写已发射的历史值。
        """
        candles = self._make_early_fvg(swing_size=5)
        s_base = msfvg_series(candles, swing_size=5)

        # 追加 5 根极端 bar（low << zone bottom，确保 zone 被填满）
        extreme_bars = [_Candle(50.0, 200.0, 10.0, 150.0) for _ in range(5)]
        candles_ext = candles + extreme_bars
        s_ext = msfvg_series(candles_ext, swing_size=5)

        # 前 len(candles) 根的值必须完全一致（含早期有限值）
        for i in range(len(candles)):
            b, e = s_base[i], s_ext[i]
            if math.isnan(b):
                assert math.isnan(e), (
                    f"no-repaint 违反：位置 {i} 基础=NaN，追加极端 bar 后变为 {e:.4f}"
                )
            else:
                assert math.isclose(b, e, rel_tol=1e-9, abs_tol=1e-12), (
                    f"no-repaint 违反：位置 {i} 早期值 {b:.6f} -> {e:.6f}（应不变）"
                )
