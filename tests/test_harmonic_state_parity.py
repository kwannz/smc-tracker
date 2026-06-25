"""增量 HarmonicState 与全量 analyze_candles 的等价性护栏（parity 回归）。

spec §1 A1：
  (a) 逐根 HarmonicState.update() 后 snapshot() == analyze_candles(candles, order, tol) 逐字段完全相等；
  (b) 每个前缀 k 都相等（analyze_candles(candles[:k]) == 喂前 k 根后的 snapshot）；
  (c) no-repaint：前缀 pivots 是更长序列 pivots 的前缀（不回改）。

order=2, tol=0.07 —— 与新运行时默认值一致（见 spec §3 C）。
显式传参，不依赖 config 默认值，避免与 Cfg agent 时序耦合。
"""
from __future__ import annotations

import math
from typing import Any

import pytest

from smc_tracker.models import Candle
from smc_tracker.indicators import HarmonicState, analyze_candles
from smc_tracker.indicators.harmonic import pivots_from_structure

# ---- 与新运行时默认值一致（spec §3 C：order 3→2, tol 0.05→0.07）----
ORDER = 2
TOL = 0.07

# ================================================================
# 辅助：合成 Candle 对象
# ================================================================

def _candle(h: float, l: float, c: float | None = None, idx: int = 0) -> Candle:
    """合成 Candle（coin="PARITY", interval="1m"）。"""
    price = c if c is not None else (h + l) / 2
    return Candle(
        coin="PARITY",
        interval="1m",
        open_time_ms=idx * 60_000,
        close_time_ms=(idx + 1) * 60_000,
        o=price,
        h=h,
        l=l,
        c=price,
        v=1.0,
        n=1,
    )


def _zigzag(n: int, amplitude: float = 20.0, base: float = 100.0) -> list[Candle]:
    """生成 n 根锯齿 K 线（正弦波）。每根 high/low 均有足够幅度形成清晰摆动点。"""
    out: list[Candle] = []
    for i in range(n):
        mid = base + amplitude * math.sin(i * math.pi / 4)
        out.append(_candle(h=mid + 2.0, l=mid - 2.0, c=mid, idx=i))
    return out


def _large_zigzag(n: int) -> list[Candle]:
    """大振幅锯齿（确保 order=2 能找到更多枢轴）。"""
    return _zigzag(n, amplitude=50.0, base=1000.0)


def _gartley_bull(n_extra: int = 15) -> list[Candle]:
    """构造含 Gartley 牛市比率的合成 K 线序列（精确几何）。

    order=2 时枢轴间距 spacing=6，确保 MarketStructure 能确认摆动点。
    """
    spacing = ORDER * 3  # 枢轴间距：6 根
    X_px, A_px = 0.0, 100.0
    XA = abs(A_px - X_px)
    B_px = A_px - XA * 0.618   # ~38.2
    AB = abs(B_px - A_px)
    C_px = B_px + AB * 0.618   # ~76.4
    D_px = C_px - abs(C_px - B_px) * 1.618

    pivots_spec = [
        (X_px, "L"),
        (A_px, "H"),
        (B_px, "L"),
        (C_px, "H"),
        (D_px, "L"),
    ]

    candles: list[Candle] = []
    idx = 0

    # 预热：让 MarketStructure 有几根背景
    for _ in range(ORDER + 2):
        candles.append(_candle(h=50.5, l=49.5, c=50.0, idx=idx))
        idx += 1

    for seg_i, (px, kind) in enumerate(pivots_spec):
        if seg_i > 0:
            prev_px = pivots_spec[seg_i - 1][0]
            for j in range(1, spacing):
                t = j / spacing
                interp = prev_px + (px - prev_px) * t
                noise = 0.2 * (0.5 - (j % 2))
                h = interp + 0.3 + abs(noise)
                l = interp - 0.3 - abs(noise)
                candles.append(_candle(h=h, l=l, c=interp, idx=idx))
                idx += 1
        if kind == "H":
            candles.append(_candle(h=px + 6.0, l=px - 1.0, c=px, idx=idx))
        else:
            candles.append(_candle(h=px + 1.0, l=px - 6.0, c=px, idx=idx))
        idx += 1

    # 额外 K 线（使 D 接近现价，满足 _COMPLETED_MAX_DIST 过滤）
    last_px = pivots_spec[-1][0]
    for j in range(n_extra):
        h = last_px + 2.0 + (j % 3) * 0.1
        l = last_px - 2.0 - (j % 3) * 0.1
        candles.append(_candle(h=h, l=l, c=last_px + 0.1, idx=idx))
        idx += 1

    return candles


# ================================================================
# 工具函数：对比两个 snapshot 逐字段相等
# ================================================================

def _assert_snapshots_equal(snap_inc: dict, snap_full: dict, label: str = "") -> None:
    """断言增量 snapshot 与全量 analyze_candles 结果逐字段完全相等。

    对比规则：
    - price 必须相等（float 精确）。
    - completed/forming 列表长度必须相等。
    - 每条记录的 pattern/direction/confidence/confluence/prz/completed/points 逐字段相等。
    """
    ctx = f"[{label}] " if label else ""

    assert snap_inc["price"] == snap_full["price"], (
        f"{ctx}price 不等: inc={snap_inc['price']}, full={snap_full['price']}"
    )

    for key in ("completed", "forming"):
        inc_list = snap_inc[key]
        full_list = snap_full[key]
        assert len(inc_list) == len(full_list), (
            f"{ctx}{key} 长度不等: inc={len(inc_list)}, full={len(full_list)}\n"
            f"  inc={inc_list}\n  full={full_list}"
        )
        for j, (r_inc, r_full) in enumerate(zip(inc_list, full_list)):
            for field in ("pattern", "direction", "confidence", "confluence",
                          "prz", "completed"):
                assert r_inc.get(field) == r_full.get(field), (
                    f"{ctx}{key}[{j}].{field} 不等: "
                    f"inc={r_inc.get(field)!r}, full={r_full.get(field)!r}"
                )
            # completed 记录才有 points 字段
            if key == "completed":
                for pt in ("X", "A", "B", "C", "D"):
                    assert r_inc["points"][pt] == r_full["points"][pt], (
                        f"{ctx}completed[{j}].points[{pt}] 不等: "
                        f"inc={r_inc['points'][pt]!r}, full={r_full['points'][pt]!r}"
                    )


# ================================================================
# 1. 核心等价性：每个前缀 k，增量 == 全量（spec §1 A1 条件 a+b）
# ================================================================

class TestParityAllPrefixes:
    """(a+b) 每个前缀 k 的增量 snapshot == 全量 analyze_candles。"""

    def _run_prefix_parity(self, candles: list[Candle], name: str) -> None:
        """对给定序列的每个前缀 k（从 0 到 len）验证等价性。"""
        hs = HarmonicState(order=ORDER, tol=TOL)
        for k, c in enumerate(candles, start=1):
            snap_inc = hs.update(c)
            snap_full = analyze_candles(candles[:k], order=ORDER, tol=TOL)
            _assert_snapshots_equal(snap_inc, snap_full, label=f"{name} k={k}")

    def test_parity_zigzag_60(self) -> None:
        """60 根锯齿序列，每根喂入后增量==全量。"""
        cs = _zigzag(60)
        self._run_prefix_parity(cs, "zigzag60")

    def test_parity_zigzag_100(self) -> None:
        """100 根大振幅锯齿序列。"""
        cs = _large_zigzag(100)
        self._run_prefix_parity(cs, "zigzag100_large")

    def test_parity_gartley_bull(self) -> None:
        """Gartley 牛市几何序列：含完整形态，增量==全量。"""
        cs = _gartley_bull()
        self._run_prefix_parity(cs, "gartley_bull")

    def test_parity_short_sequence(self) -> None:
        """极短序列（不足以产生枢轴）：两边均返回空 completed/forming，price 一致。"""
        cs = _zigzag(8)
        self._run_prefix_parity(cs, "short8")

    def test_parity_snapshot_idempotent(self) -> None:
        """snapshot() 幂等：调用两次返回完全相同结果（不消耗新 K 线）。"""
        cs = _zigzag(60)
        hs = HarmonicState(order=ORDER, tol=TOL)
        for c in cs:
            hs.update(c)
        s1 = hs.snapshot()
        s2 = hs.snapshot()
        assert s1 == s2, "snapshot() 应幂等，两次调用结果不等"

    def test_parity_update_returns_same_as_snapshot(self) -> None:
        """update() 的返回值与随后调用 snapshot() 完全相等。"""
        cs = _zigzag(60)
        hs = HarmonicState(order=ORDER, tol=TOL)
        for c in cs[:-1]:
            hs.update(c)
        ret = hs.update(cs[-1])
        snap = hs.snapshot()
        assert ret == snap, "update() 返回值应与 snapshot() 完全相等"


# ================================================================
# 2. no-repaint 不变量：前缀 pivots 是更长序列 pivots 的前缀（spec §1 A1 条件 c）
# ================================================================

class TestNoPaintPrefixInvariant:
    """(c) 前缀 pivots 是更长序列 pivots 的前缀——不回改。

    注意：这是 _alternate_immutable（first-wins）的属性，这里从 analyze_candles
    调用链验证（pivots_from_structure → _alternate_immutable），与 HarmonicState 内部
    共用同一路径，故同时间接守护增量路径的 no-repaint。
    """

    def _check_no_repaint(self, candles: list[Candle], name: str) -> None:
        """对每个相邻前缀对 (k, k+1) 验证枢轴前缀不变量。"""
        start = ORDER * 2 + 3   # 最小喂入量（让 MarketStructure 能确认枢轴）
        for k in range(start, len(candles)):
            p_k = pivots_from_structure(candles[:k], order=ORDER)
            p_k1 = pivots_from_structure(candles[:k + 1], order=ORDER)
            assert p_k == p_k1[:len(p_k)], (
                f"[{name}] k={k}: 前缀枢轴在 k+1 时被回改（repaint）。\n"
                f"  p_k[:3]={p_k[:3]}\n"
                f"  p_k1[:len(p_k)][:3]={p_k1[:len(p_k)][:3]}"
            )

    def test_no_repaint_zigzag_80(self) -> None:
        """80 根锯齿：相邻前缀枢轴列表满足 no-repaint。"""
        self._check_no_repaint(_zigzag(80), "zigzag80")

    def test_no_repaint_large_zigzag_120(self) -> None:
        """120 根大振幅：更多枢轴，no-repaint 成立。"""
        self._check_no_repaint(_large_zigzag(120), "large_zigzag120")

    def test_no_repaint_gartley_bull(self) -> None:
        """Gartley 牛市几何序列：no-repaint 成立。"""
        self._check_no_repaint(_gartley_bull(), "gartley_bull")

    def test_no_repaint_incremental_state_pivots_match(self) -> None:
        """HarmonicState 内部 _ms.swings 与重建 pivots_from_structure 的 raw swings 一致。

        验证增量状态机的 swing 流与全量重建完全等价（是 parity 的底层保证）。
        """
        from smc_tracker.indicators.harmonic import _alternate_immutable, _CandleAdapter
        from smc_tracker.smc.structure import MarketStructure

        cs = _zigzag(80)
        hs = HarmonicState(order=ORDER, tol=TOL)

        for k, c in enumerate(cs, start=1):
            hs.update(c)

            # 全量重建 MarketStructure 获取 raw swings
            ms_full = MarketStructure(lookback=ORDER)
            for i, cc in enumerate(cs[:k]):
                ms_full.update(_CandleAdapter(cc, i))

            # 对比 swing 流（index + kind + price）
            swings_inc = [(sw.index, sw.price, sw.kind) for sw in hs._ms.swings]
            swings_full = [(sw.index, sw.price, sw.kind) for sw in ms_full.swings]

            assert swings_inc == swings_full, (
                f"k={k}: 增量 _ms.swings 与全量重建不等。\n"
                f"  inc={swings_inc[-3:]}\n"
                f"  full={swings_full[-3:]}"
            )


# ================================================================
# 3. 边界与鲁棒性
# ================================================================

class TestParityEdgeCases:
    """边界条件：空序列、极短序列、monotone 序列。"""

    def test_empty_state_snapshot(self) -> None:
        """未喂入任何 K 线的 HarmonicState.snapshot() 返回 empty 结构。"""
        hs = HarmonicState(order=ORDER, tol=TOL)
        snap = hs.snapshot()
        assert snap["completed"] == []
        assert snap["forming"] == []
        assert snap["price"] == 0.0

    def test_single_candle_parity(self) -> None:
        """只喂一根 K 线：增量==全量（均不足以产生枢轴）。"""
        cs = [_candle(h=101.0, l=99.0, c=100.0, idx=0)]
        hs = HarmonicState(order=ORDER, tol=TOL)
        snap_inc = hs.update(cs[0])
        snap_full = analyze_candles(cs, order=ORDER, tol=TOL)
        _assert_snapshots_equal(snap_inc, snap_full, "single_candle")

    def test_monotone_rising_parity(self) -> None:
        """单调上涨序列：无枢轴，completed/forming 均空，price 一致。"""
        cs = [_candle(h=100.0 + i + 0.5, l=100.0 + i - 0.5, c=100.0 + i, idx=i)
              for i in range(30)]
        hs = HarmonicState(order=ORDER, tol=TOL)
        for k, c in enumerate(cs, start=1):
            snap_inc = hs.update(c)
            snap_full = analyze_candles(cs[:k], order=ORDER, tol=TOL)
            _assert_snapshots_equal(snap_inc, snap_full, f"monotone k={k}")

    def test_order_2_produces_more_pivots(self) -> None:
        """order=2 比 order=3 产生更多枢轴（灵敏度验证）。"""
        cs = _zigzag(80)
        p2 = pivots_from_structure(cs, order=2)
        p3 = pivots_from_structure(cs, order=3)
        # order=2 通常产生 >=order=3 的枢轴数
        assert len(p2) >= len(p3), (
            f"order=2 应产生 >=order=3 的枢轴数，实际 p2={len(p2)}, p3={len(p3)}"
        )

    def test_parity_longer_sequence_with_zigzag_150(self) -> None:
        """150 根序列：更长前缀验证，涵盖 _SWINGS_MAX 裁剪前阶段。"""
        cs = _zigzag(150)
        hs = HarmonicState(order=ORDER, tol=TOL)
        # 仅验证最后 50 根（避免超长测试），先喂前缀
        warmup = 100
        for c in cs[:warmup]:
            hs.update(c)
        # 对剩余部分验证 parity（逐根）
        for k_extra, c in enumerate(cs[warmup:], start=1):
            snap_inc = hs.update(c)
            k = warmup + k_extra
            snap_full = analyze_candles(cs[:k], order=ORDER, tol=TOL)
            _assert_snapshots_equal(snap_inc, snap_full, f"long150 k={k}")
