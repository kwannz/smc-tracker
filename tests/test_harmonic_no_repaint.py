"""B1: 消除谐波 repaint —— pivots_from_structure + _alternate_immutable TDD 测试。

合成数据（确定性，用真 Candle，无网络）。
覆盖 spec B1.3 所有 5 条测试用例，额外覆盖 _SWINGS_MAX=500 裁剪边界。
"""
from __future__ import annotations

import math
import pytest

from smc_tracker.models import Candle
from smc_tracker.indicators.harmonic import (
    pivots_from_structure,
    _alternate_immutable,
    analyze_candles,
)


# ============================================================
# 辅助：构造真 Candle 对象（MarketStructure.update 需要 .close_time_ms）
# ============================================================

def _candle(h: float, l: float, c: float | None = None, idx: int = 0) -> Candle:
    """合成 Candle（coin="TEST", interval="1m"）。"""
    price = c if c is not None else (h + l) / 2
    return Candle(
        coin="TEST",
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


def _zigzag_candles(n: int, amplitude: float = 10.0, base: float = 100.0) -> list[Candle]:
    """生成 n 根锯齿 K 线（正弦波，确保含清晰交替 H/L）。

    每根 H = base + amplitude*sin(...) + 1, L = base + amplitude*sin(...) - 1。
    """
    out: list[Candle] = []
    for i in range(n):
        price = base + amplitude * math.sin(i * math.pi / 4)
        out.append(_candle(h=price + 1.0, l=price - 1.0, c=price, idx=i))
    return out


def _monotone_candles(n: int, start: float = 100.0, step: float = 1.0) -> list[Candle]:
    """单调上涨 K 线（无极值，不应找到枢轴）。"""
    out: list[Candle] = []
    for i in range(n):
        p = start + i * step
        out.append(_candle(h=p + 0.5, l=p - 0.5, c=p, idx=i))
    return out


# ============================================================
# 1. test_pivots_match_legacy_contract
#    确认新函数返回正确格式 [(idx, price, 'H'|'L')] 升序、交替、长度>=5
# ============================================================

class TestPivotsMatchLegacyContract:
    def test_returns_list_of_tuples(self) -> None:
        """pivots_from_structure 返回 list[tuple[int, float, str]]。"""
        cs = _zigzag_candles(60, amplitude=10.0)
        result = pivots_from_structure(cs, order=3)
        # 可能不足 5 个，但如果有，每个必须是 3 元组
        for item in result:
            assert len(item) == 3
            idx, price, kind = item
            assert isinstance(idx, int)
            assert isinstance(price, float)
            assert kind in ("H", "L")

    def test_alternating_and_sorted(self) -> None:
        """返回序列必须升序且严格交替 H/L。"""
        cs = _zigzag_candles(80, amplitude=15.0)
        pivots = pivots_from_structure(cs, order=3)
        if len(pivots) < 2:
            pytest.skip("锯齿不足，跳过交替性测试")
        # 升序
        indices = [p[0] for p in pivots]
        assert indices == sorted(indices), "枢轴下标必须升序"
        # 交替
        for i in range(1, len(pivots)):
            assert pivots[i][2] != pivots[i - 1][2], (
                f"枢轴 {i - 1}({pivots[i-1][2]}) 与 {i}({pivots[i][2]}) 同类型，交替违规"
            )

    def test_long_zigzag_returns_five_or_more(self) -> None:
        """足够长的锯齿序列，应返回 >=5 个枢轴。"""
        cs = _zigzag_candles(100, amplitude=20.0)
        pivots = pivots_from_structure(cs, order=3)
        assert len(pivots) >= 5, f"应 >=5 个枢轴，实际 {len(pivots)}"

    def test_too_short_returns_empty(self) -> None:
        """太短的序列（不足以确认 swing）→ 返回 []。"""
        cs = _zigzag_candles(5, amplitude=10.0)
        result = pivots_from_structure(cs, order=3)
        assert result == []

    def test_monotone_returns_empty(self) -> None:
        """单调序列无极值 → 返回 []（不足 5 枢轴）。"""
        cs = _monotone_candles(30)
        result = pivots_from_structure(cs, order=3)
        assert result == []

    def test_empty_candles_returns_empty(self) -> None:
        """空列表 → 安全返回 []。"""
        result = pivots_from_structure([], order=3)
        assert result == []


# ============================================================
# 2. test_immutable_prefix_invariant (核心)
#    对每个切点 k: pivots_from_structure(cs[:k]) == 前缀 of pivots_from_structure(cs[:k+1])
# ============================================================

class TestImmutablePrefixInvariant:
    def test_prefix_never_changes(self) -> None:
        """已确认枢轴永不因新 K 线改变。

        对 cs[:k] 和 cs[:k+1], 前者的输出必须是后者的严格前缀。
        """
        cs = _zigzag_candles(80, amplitude=20.0)
        # 从 order*2+5 开始（确保有一些 pivot 可比较）
        order = 3
        start = order * 2 + 5
        for k in range(start, len(cs)):
            p_k = pivots_from_structure(cs[:k], order=order)
            p_k1 = pivots_from_structure(cs[:k + 1], order=order)
            # p_k 必须是 p_k1 的前缀
            assert p_k == p_k1[:len(p_k)], (
                f"k={k}: 已确认枢轴在 k+1 时改变。"
                f"\n  p_k({len(p_k)})={p_k[:3]}..."
                f"\n  p_k1[:len(p_k)]={p_k1[:len(p_k)][:3]}..."
            )

    def test_prefix_invariant_with_large_amplitude(self) -> None:
        """大振幅锯齿（更多极值）下 prefix 不变量仍成立。"""
        cs = _zigzag_candles(120, amplitude=50.0)
        order = 2
        start = order * 2 + 3
        for k in range(start, min(len(cs), start + 60)):
            p_k = pivots_from_structure(cs[:k], order=order)
            p_k1 = pivots_from_structure(cs[:k + 1], order=order)
            assert p_k == p_k1[:len(p_k)], (
                f"大振幅 prefix 违规 k={k}"
            )


# ============================================================
# 3. test_confirmed_xabcd_does_not_repaint (端到端)
#    一旦 D_idx 出现在 completed，后续所有 k' 的同 D_idx 点位不变
# ============================================================

def _build_gartley_bull_candles(n_extra: int = 10) -> list[Candle]:
    """构造一段在前 5 枚枢轴形成精确 Gartley 牛市形态的 K 线序列。

    用 _zigzag_candles 生成背景，然后在精确位置嵌入 Gartley 比率锯齿。
    """
    # 使用较大振幅和足够根数，确保 MarketStructure(lookback=3) 能确认枢轴
    # 每个枢轴中心需左右各 3 根支撑，合理分配索引距离
    # 方案：每个枢轴占 8 根（中心 +3 两侧 +填充），5 个枢轴 = 40 根 + extra
    order = 3
    spacing = order * 3  # 枢轴间距：每段 9 根

    # Gartley 精确比率
    X_px, A_px = 0.0, 100.0
    XA = abs(A_px - X_px)
    B_px = A_px - XA * 0.618   # 38.2
    AB = abs(B_px - A_px)
    C_px = B_px + AB * 0.618   # ~76.4
    D_px = C_px - abs(C_px - B_px) * 1.618  # 实际 D

    pivots_spec = [
        (X_px, "L"),
        (A_px, "H"),
        (B_px, "L"),
        (C_px, "H"),
        (D_px, "L"),
    ]

    candles: list[Candle] = []
    idx = 0

    # 先填充几根让 MarketStructure 有预热空间
    for _ in range(order + 2):
        candles.append(_candle(h=50.5, l=49.5, c=50.0, idx=idx))
        idx += 1

    for seg_i, (px, kind) in enumerate(pivots_spec):
        # 过渡段：从前一个价格线性插值到本枢轴（spacing 根）
        if seg_i > 0:
            prev_px = pivots_spec[seg_i - 1][0]
            for j in range(1, spacing):
                t = j / spacing
                interp = prev_px + (px - prev_px) * t
                noise = 0.3 * (0.5 - (j % 2))  # 微小噪声，让中间根不成为极值
                h = interp + 0.2 + abs(noise)
                l = interp - 0.2 - abs(noise)
                candles.append(_candle(h=h, l=l, c=interp, idx=idx))
                idx += 1

        # 枢轴中心根（必须是明确极值）
        if kind == "H":
            candles.append(_candle(h=px + 5.0, l=px - 1.0, c=px, idx=idx))
        else:
            candles.append(_candle(h=px + 1.0, l=px - 5.0, c=px, idx=idx))
        idx += 1

    # 额外 K 线（喂入更多数据，验证已确认 XABCD 不漂移）
    last_px = pivots_spec[-1][0]
    for j in range(n_extra):
        noise_h = last_px + 1.5 + (j % 3) * 0.1
        noise_l = last_px - 1.5 - (j % 3) * 0.1
        candles.append(_candle(h=noise_h, l=noise_l, c=last_px, idx=idx))
        idx += 1

    return candles


class TestConfirmedXabcdDoesNotRepaint:
    def test_completed_xabcd_d_idx_points_stable(self) -> None:
        """一旦某 D_idx 的 XABCD 在 completed 出现，后续所有 k 该 D_idx 的点位不变。

        允许因可操作性过滤（_COMPLETED_MAX_DIST/recent_cutoff）而"消失"，
        但若出现则点位必须与首次一致。
        """
        cs = _build_gartley_bull_candles(n_extra=20)
        order = 3

        # 按增量逐 k 调用 analyze_candles
        first_seen: dict[int, dict] = {}  # D_idx -> first complete record

        start = order * 2 + 5
        for k in range(start, len(cs) + 1):
            result = analyze_candles(cs[:k], order=order, tol=0.06)
            for r in result.get("completed", []):
                d_idx = r["points"]["D"][0]
                if d_idx not in first_seen:
                    first_seen[d_idx] = r
                else:
                    prev = first_seen[d_idx]
                    # 同 D_idx 的五点坐标必须与首次相同
                    for pt in ("X", "A", "B", "C", "D"):
                        assert r["points"][pt] == prev["points"][pt], (
                            f"k={k}: D_idx={d_idx} 点 {pt} 改变! "
                            f"首次={prev['points'][pt]}, 当前={r['points'][pt]}"
                        )
                    assert r["pattern"] == prev["pattern"], (
                        f"k={k}: D_idx={d_idx} pattern 改变 "
                        f"{prev['pattern']} → {r['pattern']}"
                    )


# ============================================================
# 4. test_alternate_first_wins
#    _alternate_immutable: 相邻同类型保留 index 更小者(first-wins)
#    vs _clean_alternating: 取更极端者
# ============================================================

class TestAlternateFirstWins:
    def test_adjacent_highs_keeps_first(self) -> None:
        """[H(idx=0,px=90), H(idx=1,px=100), L(idx=2,px=50)] →
        保留 idx=0 的 H(px=90)，丢弃 idx=1 的 H(px=100)（first-wins，非取更高）。
        """
        swings = [(0, 90.0, "H"), (1, 100.0, "H"), (2, 50.0, "L")]
        result = _alternate_immutable(swings)
        assert result[0] == (0, 90.0, "H"), (
            f"first-wins 应保留 (0,90,'H')，实际 {result[0]}"
        )
        assert len(result) == 2
        assert result[1] == (2, 50.0, "L")

    def test_adjacent_lows_keeps_first(self) -> None:
        """[H(idx=0), L(idx=1,px=80), L(idx=2,px=50)] →
        保留 idx=1 的 L(px=80)，丢弃 idx=2 的 L(px=50)（first-wins，非取更低）。
        """
        swings = [(0, 100.0, "H"), (1, 80.0, "L"), (2, 50.0, "L")]
        result = _alternate_immutable(swings)
        assert result[1] == (1, 80.0, "L"), (
            f"first-wins 应保留 (1,80,'L')，实际 {result[1]}"
        )

    def test_already_alternating_unchanged(self) -> None:
        """已严格交替序列 → 输出与输入完全相同。"""
        swings = [(0, 100.0, "H"), (1, 50.0, "L"), (2, 120.0, "H"), (3, 40.0, "L")]
        result = _alternate_immutable(swings)
        assert result == swings

    def test_prefix_invariant_first_wins(self) -> None:
        """first-wins 的核心不变量: _alternate_immutable(s[:k]) 是 _alternate_immutable(s[:k+1]) 的前缀。"""
        swings = [
            (0, 100.0, "H"), (1, 110.0, "H"),  # 两相邻 H
            (2, 50.0, "L"), (3, 40.0, "L"),      # 两相邻 L
            (4, 90.0, "H"), (5, 30.0, "L"),
        ]
        for k in range(1, len(swings) + 1):
            r_k = _alternate_immutable(swings[:k])
            r_k1 = _alternate_immutable(swings[:k + 1]) if k < len(swings) else r_k
            assert r_k == r_k1[:len(r_k)], (
                f"k={k}: prefix 违规\n  r_k={r_k}\n  r_k1[:len]={r_k1[:len(r_k)]}"
            )

    def test_first_wins_vs_clean_alternating_differ(self) -> None:
        """验证 first-wins 与 _clean_alternating 在相邻同类型时行为确实不同。

        _clean_alternating 取更极端(H取更高/L取更低)；
        _alternate_immutable 取先确认者(first-wins)。
        """
        from smc_tracker.indicators.harmonic import _clean_alternating

        # 相邻两个 H: 第一个 px=90，第二个 px=100（更高）
        swings = [(0, 90.0, "H"), (1, 100.0, "H"), (2, 50.0, "L")]
        old_result = _clean_alternating(swings)
        new_result = _alternate_immutable(swings)

        # 旧行为：保留更高的 H (px=100)
        assert old_result[0][1] == 100.0, f"_clean_alternating 应取更高 H=100，实际 {old_result}"
        # 新行为：保留先确认的 H (px=90)
        assert new_result[0][1] == 90.0, f"_alternate_immutable 应取 first-wins H=90，实际 {new_result}"


# ============================================================
# 5. 回归：现有 find_pivots 行为（保留 find_pivots 供测试用）
#    find_pivots 仍可导入（改为 @deprecated，不删，保留旧测试兼容）
# ============================================================

class TestFindPivotsStillImportable:
    def test_find_pivots_still_importable(self) -> None:
        """find_pivots 仍可从 harmonic 导入（向后兼容，标记 deprecated）。"""
        from smc_tracker.indicators.harmonic import find_pivots
        assert callable(find_pivots)

    def test_analyze_candles_uses_pivots_from_structure(self) -> None:
        """analyze_candles 内部改用 pivots_from_structure 后，行为与旧版一致（契约不变）。

        只验证返回字典含 completed/forming/price 键，不验证内部调用（黑盒）。
        """
        cs = _zigzag_candles(80, amplitude=20.0)
        result = analyze_candles(cs, order=3, tol=0.06)
        assert "completed" in result
        assert "forming" in result
        assert "price" in result
        assert isinstance(result["completed"], list)
        assert isinstance(result["forming"], list)


# ============================================================
# 6. _SWINGS_MAX=500 裁剪边界测试（spec B1.4 风险缓解）
# ============================================================

class TestSwingsMaxBoundary:
    def test_long_sequence_does_not_crash(self) -> None:
        """超过 _SWINGS_MAX=500 的 swing 序列：analyze_candles 不崩溃。"""
        # 生成 2500 根 K 线（超过 _MAX=512 多次裁剪）
        cs = _zigzag_candles(2500, amplitude=10.0)
        # 不应抛出任何异常
        result = analyze_candles(cs, order=3, tol=0.06)
        assert "completed" in result
        assert "price" in result

    def test_confirmed_pivots_not_lost_near_swings_max(self) -> None:
        """喂入 >500 swing 对应的 K 线序列后，pivots_from_structure 仍返回非空结果。

        _SWINGS_MAX=500 裁剪只删最旧 swing；近段枢轴应仍存在。
        """
        # 生成足够多 K 线以触发 _SWINGS_MAX 裁剪
        # order=2 产生更密枢轴（每 5 根可有 1 枢轴），触发裁剪需 ~2500 根
        cs = _zigzag_candles(2500, amplitude=10.0)
        pivots = pivots_from_structure(cs, order=2)
        # 裁剪后近段枢轴仍在，应返回非空
        assert len(pivots) > 0, "裁剪后近段枢轴应仍存在"

    def test_prefix_invariance_within_swings_max(self) -> None:
        """在 _SWINGS_MAX=500 裁剪范围内（近段 60 根），prefix 不变量成立。

        spec B1.4: "_SWINGS_MAX 裁剪只删最旧 swing，近段枢轴稳定。"
        注意: 跨越裁剪边界时，极远古枢轴被丢弃，prefix 对旧段不保证；
        但在单次调用内部，append-only 语义保证近段不变。

        本测试验证：在一个合理的序列长度（不触发 _SWINGS_MAX 或仅在末尾裁剪），
        近段 50 根的 prefix 不变量成立。
        """
        order = 3
        # 使用不超过 _SWINGS_MAX=500 对应 K 线的序列（order=3 每5根约1个swing，500swing≈2500根）
        # 用 400 根即可触发 _MAX=512 的价格缓冲裁剪但不触发 swing 裁剪
        cs = _zigzag_candles(400, amplitude=10.0)
        start = order * 2 + 5
        for k in range(start, len(cs)):
            p_k = pivots_from_structure(cs[:k], order=order)
            p_k1 = pivots_from_structure(cs[:k + 1], order=order)
            assert p_k == p_k1[:len(p_k)], (
                f"近段 prefix 违规 k={k}"
            )
