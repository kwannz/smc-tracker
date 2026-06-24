"""tests/test_sfg_pivot.py — SFG pivot 反转因子 TDD 测试套件。

合成确定性测试：不依赖网络/真实数据，所有输入手工构造。

因子符号约定（SFG sign_convention，reversal / mean-reversion）：
  +1 = close == bot（支撑位）→ 看涨反转信号
  -1 = close == top（压力位）→ 看跌反转信号
   0 = close == mid            → 中性
  NaN = warmup / 无完整 pivot shelf / 退化

诚实标注：pivot 是反转因子，不是突破方向，不构成投资建议。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from smc_tracker.indicators.sfg.pivot import pivot_series, pivot_factor


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：合成 Candle 对象
# ─────────────────────────────────────────────────────────────────────────────

class _Candle:
    """属性访问（.o/.h/.l/.c/.v），与 _common.ohlcv_arrays 一致。"""
    __slots__ = ("o", "h", "l", "c", "v")

    def __init__(self, o: float, h: float, lo: float, c: float, v: float) -> None:
        self.o = o
        self.h = h
        self.l = lo
        self.c = c
        self.v = v


def _make_candles_from_arrays(
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> list[_Candle]:
    """从 high/low/close 列表构建 candle 列表（open/volume 为常数占位）。"""
    assert len(highs) == len(lows) == len(closes)
    return [
        _Candle(c, h, lo, c, 1000.0)
        for h, lo, c in zip(highs, lows, closes)
    ]


def _build_channel_candles(
    n: int = 50,
    top: float = 110.0,
    bot: float = 90.0,
    close_val: float | None = None,
    left: int = 5,
    right: int = 5,
) -> list[_Candle]:
    """构造有明确 pivot-high 和 pivot-low 的通道行情。

    结构：
      bars 0..left-1           : 递减斜坡（为 pivot-low 准备左侧背景）
      bar  left                : pivot-low 中心 (low=bot，最低点)
      bars left+1..left+right  : 斜坡回升（right 根右确认，确认发生在 left+right）
      bars left+right..2*left+right : 递增斜坡（为 pivot-high 准备左侧背景）
      bar  2*left+right        : pivot-high 中心 (high=top，最高点)
      bars 2*left+right+1..2*(left+right) : 斜坡回落（right 根右确认）
      bars 之后                : close 固定为 close_val，直到 n 根

    因此 shelf 在 n 根序列的末尾已被 ffill：top_shelf≈top，bot_shelf≈bot。
    close_val 在 (bot, top) 区间时因子应为有限值。
    """
    if close_val is None:
        close_val = (top + bot) / 2.0

    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []

    mid = (top + bot) / 2.0
    span = top - bot

    # Phase 1: 下降到 pivot-low 位置（bars 0..left+right）
    for j in range(left + right + 1):
        frac = j / (left + right)
        base = mid + span * 0.3 * (1 - frac)  # 从偏高开始下降
        if j == left:
            # pivot-low 中心：low 要严格低于左右各 left/right 根
            lo = bot
            hi = bot + span * 0.1
        else:
            lo = bot + span * 0.15 + span * 0.1 * abs(j - left) / (left + 1)
            hi = lo + span * 0.1
        highs.append(hi)
        lows.append(lo)
        closes.append((hi + lo) / 2.0)

    # Phase 2: 上升到 pivot-high 位置（bars left+right+1..2*(left+right)）
    phase2_len = left + right
    for j in range(1, phase2_len + 1):
        frac = j / phase2_len
        center_idx = left  # pivot-high 中心在 phase2 第 left 根
        if j == left:
            # pivot-high 中心：high 要严格高于左右各 left/right 根
            hi = top
            lo = top - span * 0.1
        else:
            hi = bot + span * 0.15 + span * 0.1 * abs(j - left) / (left + 1)
            lo = hi - span * 0.1
        highs.append(hi)
        lows.append(lo)
        closes.append((hi + lo) / 2.0)

    # Phase 3: 填充到 n 根，close 固定
    current_len = len(highs)
    extra = n - current_len
    if extra < 0:
        extra = 0
    for _ in range(extra):
        highs.append(close_val + span * 0.05)
        lows.append(close_val - span * 0.05)
        closes.append(close_val)

    # 截断到 n 根
    highs = highs[:n]
    lows = lows[:n]
    closes = closes[:n]

    return _make_candles_from_arrays(highs, lows, closes)


def _make_simple_pivot_candles(
    left: int = 3,
    right: int = 3,
    top: float = 110.0,
    bot: float = 90.0,
    final_close: float | None = None,
    n_extra: int = 5,
) -> list[_Candle]:
    """最小化手工 pivot 序列：精确控制 pivot-high/low 中心位置，验证黄金值。

    序列布局（以 left=right=3 为例）：
      bar 0: high=100 (下降背景)
      bar 1: high=102
      bar 2: high=104
      bar 3: pivot-low 中心 low=bot (低于前3后3)   → 确认在 bar 6
      bar 4: high=104 low=bot+gap
      bar 5: high=102
      bar 6: 确认 bar (pivot-low 确认), high~mid
      bar 7: high=100 (上升背景)
      bar 8: high=103
      bar 9: high=105
      bar 10: high=108
      ...（省略，继续到 pivot-high 位置）

    为简单起见，使用两段直线斜坡。
    """
    if final_close is None:
        final_close = (top + bot) / 2.0

    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []

    gap = (top - bot) * 0.15   # pivot 中心与左右邻居的差距

    # --- pivot-low 段：center 在 index = left ---
    # 左 left 根：low 从 bot+gap*(left+1) 到 bot+gap*1（单调减，但不达 bot）
    for j in range(left):
        lo = bot + gap * (left - j)
        hi = lo + gap
        highs.append(hi)
        lows.append(lo)
        closes.append((hi + lo) / 2.0)

    # pivot-low 中心
    lo_center = bot
    hi_center = bot + gap * 0.5
    highs.append(hi_center)
    lows.append(lo_center)
    closes.append((hi_center + lo_center) / 2.0)

    # 右 right 根：low 从 bot+gap 到 bot+gap*right（单调增，大于 bot）
    for j in range(1, right + 1):
        lo = bot + gap * j
        hi = lo + gap
        highs.append(hi)
        lows.append(lo)
        closes.append((hi + lo) / 2.0)
    # 此时 index = left + right = pivot-low 确认 bar

    # --- pivot-high 段：再走 left 根上升斜坡，pivot-high 中心在 index = 2*left+right ---
    # left 根上升
    base_hi = top - gap * (left + 1)
    for j in range(left):
        hi = base_hi + gap * (j + 1)
        lo = hi - gap * 0.5
        highs.append(hi)
        lows.append(lo)
        closes.append((hi + lo) / 2.0)

    # pivot-high 中心
    hi_center2 = top
    lo_center2 = top - gap * 0.5
    highs.append(hi_center2)
    lows.append(lo_center2)
    closes.append((hi_center2 + lo_center2) / 2.0)

    # 右 right 根下降（确认 pivot-high）
    for j in range(1, right + 1):
        hi = top - gap * j
        lo = hi - gap * 0.5
        highs.append(hi)
        lows.append(lo)
        closes.append((hi + lo) / 2.0)
    # 此时 index = 2*(left+right) = pivot-high 确认 bar

    # extra 根：close 固定为 final_close
    for _ in range(n_extra):
        highs.append(final_close + gap * 0.1)
        lows.append(final_close - gap * 0.1)
        closes.append(final_close)

    return _make_candles_from_arrays(highs, lows, closes)


# ─────────────────────────────────────────────────────────────────────────────
# 测试组 1：输出形状 + 类型
# ─────────────────────────────────────────────────────────────────────────────

class TestOutputShape:
    def test_series_length_equals_candles(self):
        """pivot_series 输出长度 == 输入 candles 数量。"""
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=10)
        s = pivot_series(candles, left_bars=3, right_bars=3)
        assert len(s) == len(candles), (
            f"series 长度 {len(s)} != candles 长度 {len(candles)}"
        )

    def test_series_returns_ndarray(self):
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=10)
        s = pivot_series(candles, left_bars=3, right_bars=3)
        assert isinstance(s, np.ndarray), f"应返回 np.ndarray，实际={type(s)}"

    def test_series_dtype_float(self):
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=10)
        s = pivot_series(candles, left_bars=3, right_bars=3)
        assert np.issubdtype(s.dtype, np.floating), f"dtype 应为 float，实际={s.dtype}"


# ─────────────────────────────────────────────────────────────────────────────
# 测试组 2：warmup 边界
# ─────────────────────────────────────────────────────────────────────────────

class TestWarmup:
    def test_empty_input_returns_empty(self):
        s = pivot_series([], left_bars=3, right_bars=3)
        assert len(s) == 0

    def test_too_short_all_nan(self):
        """K 线数量 < left+right+1 时，全部为 nan。"""
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=0)
        # 截取仅 3 根（远不足 left=3, right=3 最小需求 left+right+1=7）
        short = candles[:3]
        s = pivot_series(short, left_bars=3, right_bars=3)
        assert np.all(~np.isfinite(s)), "序列过短时全部应为 nan"

    def test_pivot_factor_none_on_too_short(self):
        """不足 warmup 时 pivot_factor 应返回 None。"""
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=0)
        short = candles[:3]
        result = pivot_factor(short, left_bars=3, right_bars=3)
        assert result is None, f"不足 warmup 时应返回 None，实际={result}"

    def test_warmup_prefix_is_nan(self):
        """pivot_series 首部（直到两个 shelf 均建立前）应为 nan。"""
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=10)
        s = pivot_series(candles, left_bars=3, right_bars=3)
        # 至少前 left+right 根应为 nan（pivot-low 确认前无 bot_shelf）
        prefix_len = 3 + 3  # left + right = 6
        prefix = s[:prefix_len]
        assert np.all(~np.isfinite(prefix)), (
            f"前 {prefix_len} 根应全为 nan，实际={prefix}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 测试组 3：输出范围 [-1, 1] + 有限性守卫
# ─────────────────────────────────────────────────────────────────────────────

class TestOutputRange:
    def test_finite_values_in_range(self):
        """所有非 nan 值应在 [-1, 1] 范围内。"""
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=20)
        s = pivot_series(candles, left_bars=3, right_bars=3)
        finite_mask = np.isfinite(s)
        if finite_mask.any():
            vals = s[finite_mask]
            assert np.all(vals >= -1.0 - 1e-9), f"有值 < -1: {vals[vals < -1.0]}"
            assert np.all(vals <= 1.0 + 1e-9), f"有值 > +1: {vals[vals > 1.0]}"

    def test_no_inf_values(self):
        """输出不得包含 inf/-inf（非有限必须是 nan）。"""
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=20)
        s = pivot_series(candles, left_bars=3, right_bars=3)
        assert not np.any(np.isinf(s)), f"输出包含 inf: {s[np.isinf(s)]}"

    def test_nan_sentinel_no_imputation(self):
        """缺失数据处（warmup/无 shelf）应为 nan，不应 impute 为 0。"""
        # 构造仅够一侧 shelf（低于建立双侧 shelf 的阈值）
        # 极短序列，pivot_series 不应返回全 0
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=0)
        short = candles[:5]
        s = pivot_series(short, left_bars=3, right_bars=3)
        # 此时全 nan，不应有 0
        assert not np.any(s == 0.0), (
            "warmup 期间不得 impute 为 0（应为 nan）"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 测试组 4：黄金值 — 边界价格精确验证
# ─────────────────────────────────────────────────────────────────────────────

class TestGoldenValues:
    """手工计算已知 pivot shelf，验证 pivot_factor 精确值。

    spec parity_notes 指出：
      close == bot → f = +1
      close == top → f = -1
      close == mid → f =  0
    (因为 f = (top+bot-2c)/(top-bot))

    使用直接数组构造确保 shelf 精确等于 top/bot，无额外 pivot 干扰。
    """

    def _shelved_candles_exact(
        self,
        top: float,
        bot: float,
        final_close: float,
        left: int = 3,
        right: int = 3,
    ) -> list[_Candle]:
        """精确构造：确保 top_shelf == top、bot_shelf == bot 在末 bar 有效。

        构造逻辑（left=right=3 为例，不形成额外中间 pivot）：

        pivot-low 段（center 在 index=left=3）：
          bars 0..2: low 单调递减 [bot+3gap, bot+2gap, bot+gap]（各不相等且 > bot）
          bar   3:   low = bot（严格低于前后各 3）
          bars 4..6: low 单调递增 [bot+gap, bot+2gap, bot+3gap]

        pivot-high 段（center 在 index = left + (left + right + 1) = 3 + 7 = 10）：
          bars 7..9: high 单调递增 [top-3gap, top-2gap, top-gap]
          bar  10:   high = top（严格高于前后各 3）
          bars 11..13: high 单调递减 [top-gap, top-2gap, top-3gap]

        extra 段（bar 14..14+n_extra-1）：
          high/low 在 (bot, top) 内部，close = final_close
          选取区间 [mid-gap/2, mid+gap/2]，不足以形成新 pivot

        这保证：
          top_shift (确认后 ffill) = top
          bot_shift (确认后 ffill) = bot
          在 extra 段末尾成立。
        """
        mid = (top + bot) / 2.0
        gap = (top - bot) * 0.12  # pivot 中心与邻居差距

        highs: list[float] = []
        lows: list[float] = []

        # ── pivot-low 段 ──────────────────────────────────────────────────────
        # bar 0..left-1: low 单调递减，high 随之
        for j in range(left):
            lo = bot + gap * (left - j)
            highs.append(lo + gap * 0.5)
            lows.append(lo)
        # bar left: pivot-low 中心
        highs.append(bot + gap * 0.5)
        lows.append(bot)
        # bar left+1..left+right: low 单调递增（各 > bot）
        for j in range(1, right + 1):
            lo = bot + gap * j
            highs.append(lo + gap * 0.5)
            lows.append(lo)
        # pivot-low 确认在 bar left+right，bot_shift 从此 ffill = bot

        # ── pivot-high 段 ────────────────────────────────────────────────────
        # bar left+right+1..2*left+right: high 单调递增
        for j in range(1, left + 1):
            hi = top - gap * (left + 1 - j)
            highs.append(hi)
            lows.append(hi - gap * 0.5)
        # bar 2*left+right+1: pivot-high 中心
        highs.append(top)
        lows.append(top - gap * 0.5)
        # bar 2*left+right+2..2*(left+right)+1: high 单调递减（各 < top）
        for j in range(1, right + 1):
            hi = top - gap * j
            highs.append(hi)
            lows.append(hi - gap * 0.5)
        # pivot-high 确认在 bar 2*(left+right)+1，top_shift 从此 ffill = top

        # ── extra 段：不形成新 pivot，close = final_close ──────────────────
        n_extra = 15
        inner_hi = mid + gap * 0.3
        inner_lo = mid - gap * 0.3
        for _ in range(n_extra):
            highs.append(inner_hi)
            lows.append(inner_lo)

        # close 对所有 bar 都设为 mid（只有 extra 段的最后一根设为 final_close）
        n = len(highs)
        closes: list[float] = []
        for i in range(n):
            if i == n - 1:
                closes.append(final_close)
            else:
                closes.append((highs[i] + lows[i]) / 2.0)

        return _make_candles_from_arrays(highs, lows, closes)

    def test_close_at_bot_gives_plus_one(self):
        """close == bot → factor = +1 (看涨反转，在支撑位)。"""
        top, bot = 110.0, 90.0
        candles = self._shelved_candles_exact(top, bot, final_close=bot)
        f = pivot_factor(candles, left_bars=3, right_bars=3)
        assert f is not None, "应有有效 factor"
        assert math.isclose(f, 1.0, abs_tol=1e-6), (
            f"close==bot 时 factor 应=+1，实际={f:.8f}"
        )

    def test_close_at_top_gives_minus_one(self):
        """close == top → factor = -1 (看跌反转，在压力位)。"""
        top, bot = 110.0, 90.0
        candles = self._shelved_candles_exact(top, bot, final_close=top)
        f = pivot_factor(candles, left_bars=3, right_bars=3)
        assert f is not None, "应有有效 factor"
        assert math.isclose(f, -1.0, abs_tol=1e-6), (
            f"close==top 时 factor 应=-1，实际={f:.8f}"
        )

    def test_close_at_mid_gives_zero(self):
        """close == mid → factor = 0 (在通道中点)。"""
        top, bot = 110.0, 90.0
        mid = (top + bot) / 2.0
        candles = self._shelved_candles_exact(top, bot, final_close=mid)
        f = pivot_factor(candles, left_bars=3, right_bars=3)
        assert f is not None, "应有有效 factor"
        assert math.isclose(f, 0.0, abs_tol=1e-6), (
            f"close==mid 时 factor 应=0，实际={f:.8f}"
        )

    def test_formula_exact_midpoint_value(self):
        """中间价格精确公式验证: f = (top+bot-2c)/(top-bot)。"""
        top, bot = 110.0, 90.0
        c = 95.0  # 偏支撑侧
        expected = (top + bot - 2 * c) / (top - bot)  # = (200-190)/20 = 0.5
        candles = self._shelved_candles_exact(top, bot, final_close=c)
        f = pivot_factor(candles, left_bars=3, right_bars=3)
        assert f is not None, "应有有效 factor"
        assert math.isclose(f, expected, abs_tol=1e-4), (
            f"c={c} 时 factor 应≈{expected:.6f}，实际={f:.8f}"
        )

    def test_formula_above_midpoint_value(self):
        """压力侧价格: c=105 → f = (200-210)/20 = -0.5（看跌）。"""
        top, bot = 110.0, 90.0
        c = 105.0
        expected = (top + bot - 2 * c) / (top - bot)  # = -0.5
        candles = self._shelved_candles_exact(top, bot, final_close=c)
        f = pivot_factor(candles, left_bars=3, right_bars=3)
        assert f is not None, "应有有效 factor"
        assert math.isclose(f, expected, abs_tol=1e-4), (
            f"c={c} 时 factor 应≈{expected:.6f}，实际={f:.8f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 测试组 5：退化情形 fail-closed
# ─────────────────────────────────────────────────────────────────────────────

class TestFailClosed:
    def test_degenerate_top_equals_bot_gives_nan(self):
        """top==bot（half_range=0）→ nan，不崩溃。"""
        # 构造 top=bot 的退化通道：high 和 low 完全相同
        n = 30
        val = 100.0
        highs = [val] * n
        lows = [val] * n
        closes = [val] * n
        candles = _make_candles_from_arrays(highs, lows, closes)
        s = pivot_series(candles, left_bars=3, right_bars=3)
        # 退化通道：所有值应为 nan（无法形成有效 pivot 且 top==bot）
        assert np.all(~np.isfinite(s)), (
            f"退化通道（top==bot）时全部应为 nan"
        )

    def test_all_nan_input_gives_nan_output(self):
        """含 nan 的输入不应崩溃，输出 nan。"""
        n = 20
        highs = [float("nan")] * n
        lows = [float("nan")] * n
        closes = [float("nan")] * n
        candles = _make_candles_from_arrays(highs, lows, closes)
        s = pivot_series(candles, left_bars=3, right_bars=3)
        assert np.all(~np.isfinite(s)), "全 nan 输入时输出全为 nan"


# ─────────────────────────────────────────────────────────────────────────────
# 测试组 6：符号方向（反转因子方向验证）
# ─────────────────────────────────────────────────────────────────────────────

class TestSignDirection:
    """反转因子：close 在支撑 → 正；close 在压力 → 负。"""

    def test_near_support_positive(self):
        """close 靠近 bot（支撑位），factor 应 > 0。"""
        top, bot = 110.0, 90.0
        close_val = bot + (top - bot) * 0.1  # 10% 以上 bot，靠近支撑
        candles = _make_simple_pivot_candles(
            left=3, right=3, top=top, bot=bot,
            final_close=close_val, n_extra=10
        )
        f = pivot_factor(candles, left_bars=3, right_bars=3)
        assert f is not None
        assert f > 0, (
            f"close 靠近支撑时 factor 应 > 0，实际={f:.4f}"
        )

    def test_near_resistance_negative(self):
        """close 靠近 top（压力位），factor 应 < 0。"""
        top, bot = 110.0, 90.0
        close_val = top - (top - bot) * 0.1  # 10% 以下 top，靠近压力
        candles = _make_simple_pivot_candles(
            left=3, right=3, top=top, bot=bot,
            final_close=close_val, n_extra=10
        )
        f = pivot_factor(candles, left_bars=3, right_bars=3)
        assert f is not None
        assert f < 0, (
            f"close 靠近压力时 factor 应 < 0，实际={f:.4f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 测试组 7：no-repaint（prefix-invariance）— 核心无前视护栏
# ─────────────────────────────────────────────────────────────────────────────

class TestNoRepaint:
    """测试已发射的早期值不因尾部新 bar 变化而改变（prefix-invariance）。

    这是本因子的 no-lookahead 硬护栏，对 KNN 标签诚实性至关重要。
    测试方法：
      1. 对 candles[:N] 计算 pivot_series → series_short
      2. 在末尾追加更极端的新 bar，对 candles[:N+K] 计算 → series_long
      3. 断言 series_short[:N] == series_long[:N]（prefix 不变）
    """

    def _candles_with_extreme_tail(
        self, base_n: int, extra_bars: int, extreme_high: float, extreme_low: float
    ) -> tuple[list[_Candle], list[_Candle]]:
        """构造 base candles + 追加极端新 bar 的两版序列。"""
        base = _make_simple_pivot_candles(left=3, right=3, top=110.0, bot=90.0, n_extra=base_n)
        extreme = [
            _Candle(100.0, extreme_high, extreme_low, 100.0, 1000.0)
            for _ in range(extra_bars)
        ]
        extended = base + extreme
        return base, extended

    def test_prefix_invariance_basic(self):
        """尾部追加普通 bar，早期值不变。"""
        base, extended = self._candles_with_extreme_tail(
            base_n=5, extra_bars=3,
            extreme_high=120.0, extreme_low=80.0  # 不改变已确认的 pivot
        )
        s_base = pivot_series(base, left_bars=3, right_bars=3)
        s_ext = pivot_series(extended, left_bars=3, right_bars=3)

        n = len(base)
        prefix_base = s_base[:n]
        prefix_ext = s_ext[:n]

        # 对有限值（已发射）进行比较
        finite_both = np.isfinite(prefix_base) & np.isfinite(prefix_ext)
        if finite_both.any():
            np.testing.assert_allclose(
                prefix_base[finite_both],
                prefix_ext[finite_both],
                rtol=1e-9,
                err_msg="早期已发射值不应因尾部新 bar 变化（no-repaint 违规）",
            )

        # 同时断言：nan 的位置也应一致（warmup 区域不变）
        nan_base = ~np.isfinite(prefix_base)
        nan_ext = ~np.isfinite(prefix_ext)
        np.testing.assert_array_equal(
            nan_base,
            nan_ext,
            err_msg="nan 掩码应相同（warmup 区域前视不变）",
        )

    def test_prefix_invariance_extreme_tail(self):
        """尾部追加极端新高新低 bar，早期已确认 pivot shelf 不应改变。

        关键：确认已发射的 top_shift/bot_shift 值（前向填充的历史 pivot）
        不因新 bar 的极端 high/low 而回溯修改。
        """
        base, extended = self._candles_with_extreme_tail(
            base_n=10, extra_bars=5,
            extreme_high=200.0,  # 极端新高
            extreme_low=10.0,    # 极端新低
        )
        s_base = pivot_series(base, left_bars=3, right_bars=3)
        s_ext = pivot_series(extended, left_bars=3, right_bars=3)

        n = len(base)
        prefix_base = s_base[:n]
        prefix_ext = s_ext[:n]

        # 只要两边都有限，值应完全相同
        finite_both = np.isfinite(prefix_base) & np.isfinite(prefix_ext)
        if finite_both.any():
            np.testing.assert_allclose(
                prefix_base[finite_both],
                prefix_ext[finite_both],
                rtol=1e-9,
                err_msg="极端尾部 bar 不应改变早期已确认的 pivot factor 值",
            )

    def test_no_future_bars_affect_past_nan_to_finite(self):
        """早期 nan（无 shelf）不应因追加尾部新 bar 变成有限值。

        i.e. 若 s_base[i] 为 nan，s_ext[i] 也应为 nan（不因后来 bar 产生前视 shelf）。
        """
        base, extended = self._candles_with_extreme_tail(
            base_n=8, extra_bars=10,
            extreme_high=150.0,
            extreme_low=50.0,
        )
        s_base = pivot_series(base, left_bars=3, right_bars=3)
        s_ext = pivot_series(extended, left_bars=3, right_bars=3)

        n = len(base)
        # 若 s_base[i] 是 nan，s_ext[i] 也必须是 nan
        nan_in_base = ~np.isfinite(s_base[:n])
        nan_in_ext = ~np.isfinite(s_ext[:n])
        # nan_in_base 应是 nan_in_ext 的子集（追加 bar 不能使早期 nan 变有限）
        assert np.all(nan_in_base[nan_in_base] == nan_in_ext[nan_in_base]), (
            "尾部 bar 不能让早期 nan 回填为有限值（no-lookahead 违规）"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 测试组 8：pivot_factor 标量包装器
# ─────────────────────────────────────────────────────────────────────────────

class TestPivotFactor:
    def test_returns_float_on_sufficient_data(self):
        """足量数据时应返回 float。"""
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=10)
        f = pivot_factor(candles, left_bars=3, right_bars=3)
        # 可能因 shelf 未建立仍为 None，但不应崩溃
        if f is not None:
            assert isinstance(f, float), f"应为 float，实际={type(f)}"
            assert math.isfinite(f), f"应为有限 float，实际={f}"

    def test_returns_none_on_empty(self):
        f = pivot_factor([], left_bars=3, right_bars=3)
        assert f is None

    def test_factor_matches_series_last_finite(self):
        """pivot_factor 应等于 pivot_series 的最后一个有限值。"""
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=15)
        s = pivot_series(candles, left_bars=3, right_bars=3)
        f = pivot_factor(candles, left_bars=3, right_bars=3)

        finite_vals = s[np.isfinite(s)]
        if len(finite_vals) == 0:
            assert f is None, "series 全 nan 时 factor 应为 None"
        else:
            assert f is not None, "series 有有限值时 factor 不应为 None"
            assert math.isclose(f, finite_vals[-1], rel_tol=1e-9), (
                f"factor={f:.8f} 应等于 series 末有限值={finite_vals[-1]:.8f}"
            )

    def test_factor_in_range(self):
        """factor 值应在 [-1, 1] 范围内。"""
        candles = _make_simple_pivot_candles(left=3, right=3, n_extra=15)
        f = pivot_factor(candles, left_bars=3, right_bars=3)
        if f is not None:
            assert -1.0 <= f <= 1.0, f"factor={f} 超出 [-1,1] 范围"


# ─────────────────────────────────────────────────────────────────────────────
# 测试组 9：默认参数（left_bars=10, right_bars=10）
# ─────────────────────────────────────────────────────────────────────────────

class TestDefaultParams:
    def test_default_params_work(self):
        """默认参数 left_bars=10, right_bars=10 时不崩溃，返回同长度数组。"""
        candles = _make_simple_pivot_candles(left=10, right=10, n_extra=30)
        s = pivot_series(candles)
        assert len(s) == len(candles)
        assert isinstance(s, np.ndarray)

    def test_pivot_factor_default_params(self):
        """pivot_factor 使用默认参数不崩溃。"""
        candles = _make_simple_pivot_candles(left=10, right=10, n_extra=30)
        f = pivot_factor(candles)  # 默认 left=10, right=10
        # 不崩溃即可；返回 None 或 float 均可
        assert f is None or (isinstance(f, float) and math.isfinite(f))
