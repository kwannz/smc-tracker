"""谐波形态计算 TDD 测试。

严格合成数据（精确比率）确定性测试：
- detect_xabcd: Gartley/Bat/Butterfly/Crab bull&bear 命中
- 故意破坏比率 → 不命中（校验真在起作用）
- project_prz: 前瞻预测（XABC 已知，D 未知）→ forming
- find_pivots: 交替 H/L 正确，不足 5 点 → []
- _within/_ratio 边界守卫（分母 0）
"""
from __future__ import annotations

import math
import pytest

# ---- 测试前这些导入全部应失败（RED 阶段：模块未建）----
from smc_tracker.indicators.harmonic import (
    HARMONIC_RATIOS,
    _ratio,
    _within,
    find_pivots,
    detect_xabcd,
    project_prz,
    analyze_candles,
)


# ========== 辅助：合成 Candle-like 对象 ==========

class _C:
    """轻量合成 K 线（只需 h/l/c 字段）。"""
    __slots__ = ("h", "l", "c")

    def __init__(self, h: float, l: float, c: float | None = None) -> None:
        self.h = h
        self.l = l
        self.c = c if c is not None else (h + l) / 2


def _make_candles_from_pivots(pivots: list[tuple[float, str]]) -> list[_C]:
    """从 [(price, 'H'|'L'), ...] 生成锯齿 K 线序列（每个枢轴一根 K 线）。

    H 枢轴：h=price, l=price*0.995
    L 枢轴：h=price*1.005, l=price
    中间 K 线用直线插值（各 1 根）填充，保证 scipy 能找到极值。
    """
    candles: list[_C] = []
    for price, kind in pivots:
        if kind == "H":
            candles.append(_C(h=price, l=price * 0.995, c=price))
        else:
            candles.append(_C(h=price * 1.005, l=price, c=price))
    return candles


def _build_gartley_bull_pivots() -> list[tuple[float, str]]:
    """精确 Gartley 牛市枢轴（Scott Carney 标准比率正中心）。

    X=0  A=100  B=61.8(XA回撤 0.618)  C=AB 回撤 0.618  D=XA 0.786 投影
    XA=100, AB=XA*0.618=61.8, BC=AB*0.618=38.19, CD=BC*1.618≈61.79 → D≈38.21
    实际 D：D = A - |XA|*0.786 = 100 - 100*0.786 = 21.4  (Gartley D在 0.786 XA)
    """
    X = 0.0
    A = 100.0
    XA = abs(A - X)

    # B = A - XA*0.618 (回撤 bull: B 在 XA 0.618)
    B = A - XA * 0.618

    # BC = AB*0.618 (bull: C 从 B 反弹)
    AB = abs(B - A)
    C = B + AB * 0.618

    # CD = BC * 1.618; D 从 C 向下
    BC = abs(C - B)
    D = C - BC * 1.618

    return [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]


def _build_bat_bull_pivots() -> list[tuple[float, str]]:
    """精确 Bat 牛市枢轴（Carney Bat）。

    Bat: B=XA 0.382~0.50, D=XA 0.886
    取 B=XA*0.382, BC/AB=0.618, CD/BC≈2.618, D=XA*0.886
    """
    X = 0.0
    A = 100.0
    XA = abs(A - X)

    B = A - XA * 0.382
    AB = abs(B - A)
    C = B + AB * 0.618
    # D 以 XA 0.886 为准
    D = A - XA * 0.886

    return [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]


def _build_butterfly_bull_pivots() -> list[tuple[float, str]]:
    """精确 Butterfly 牛市枢轴。

    Butterfly: B=XA 0.786, D=XA 1.272
    BC/AB=0.618, CD/BC=1.618
    """
    X = 0.0
    A = 100.0
    XA = abs(A - X)

    B = A - XA * 0.786
    AB = abs(B - A)
    C = B + AB * 0.618
    D = A - XA * 1.272   # 扩展超过 X (D < X 在 bull)

    return [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]


def _build_crab_bull_pivots() -> list[tuple[float, str]]:
    """精确 Crab 牛市枢轴。

    Crab: B=XA 0.382~0.618, D=XA 1.618
    取 B=XA*0.382, BC/AB=0.618, D=XA*1.618
    """
    X = 0.0
    A = 100.0
    XA = abs(A - X)

    B = A - XA * 0.382
    AB = abs(B - A)
    C = B + AB * 0.618
    D = A - XA * 1.618   # D 远低于 X

    return [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]


# ========== 单元测试 ==========

class TestRatioWithin:
    def test_ratio_normal(self) -> None:
        r = _ratio(61.8, 100.0)
        assert abs(r - 0.618) < 1e-9

    def test_ratio_zero_denominator(self) -> None:
        # 分母为 0 → 返回 inf（守卫：不崩溃，不返回 NaN）
        r = _ratio(1.0, 0.0)
        assert math.isinf(r)

    def test_within_inside(self) -> None:
        assert _within(0.618, 0.618, 0.618, tol=0.05)

    def test_within_outside(self) -> None:
        assert not _within(0.50, 0.618, 0.618, tol=0.05)

    def test_within_edge(self) -> None:
        # 恰好在容差边缘（lo*(1-tol) 到 hi*(1+tol)）
        lo, hi, tol = 0.382, 0.618, 0.05
        assert _within(lo * (1 - tol), lo, hi, tol=tol)
        assert _within(hi * (1 + tol), lo, hi, tol=tol)
        assert not _within(lo * (1 - tol) - 0.001, lo, hi, tol=tol)


class TestFindPivots:
    def test_alternating_zigzag(self) -> None:
        """锯齿 close：交替 H/L，所有枢轴正确交替。"""
        # 生成标准锯齿：下降→上升→下降→... 共 20 根
        import numpy as np
        prices = [100.0 + 10 * math.sin(i * math.pi / 3) for i in range(30)]
        candles = [_C(h=p + 1, l=p - 1, c=p) for p in prices]
        pivots = find_pivots(candles, order=2)
        # 应有 >= 5 个枢轴
        assert len(pivots) >= 5
        # 交替性检验：相邻类型不同
        for i in range(1, len(pivots)):
            assert pivots[i][2] != pivots[i - 1][2], (
                f"枢轴 {i-1}({pivots[i-1][2]}) 和 {i}({pivots[i][2]}) 类型相同，交替错误"
            )

    def test_too_few_points_returns_empty(self) -> None:
        """不足 5 个枢轴 → 返回 []。"""
        # 仅 3 根单调上涨 K 线，无法找到足够的极值
        candles = [_C(h=float(i + 1), l=float(i), c=float(i) + 0.5) for i in range(3)]
        result = find_pivots(candles, order=2)
        assert result == []

    def test_returns_sorted_ascending(self) -> None:
        """枢轴按下标升序排列。"""
        prices = [100, 90, 110, 80, 120, 70, 130, 60, 140]
        candles = [_C(h=p + 1, l=p - 1, c=float(p)) for p in prices]
        pivots = find_pivots(candles, order=1)
        indices = [p[0] for p in pivots]
        assert indices == sorted(indices)


class TestDetectXABCD:
    def test_gartley_bull_detected(self) -> None:
        """精确 Gartley 牛市比率 → 命中 pattern='Gartley', direction='bull'。"""
        pts = _build_gartley_bull_pivots()
        # 直接构造 5 个枢轴点（手工，绕过 find_pivots）
        pivots = [
            (i, price, kind)
            for i, (price, kind) in enumerate(pts)
        ]
        results = detect_xabcd(pivots, tol=0.06)
        patterns = [r["pattern"] for r in results]
        directions = [r["direction"] for r in results]
        assert "Gartley" in patterns, f"Gartley 未命中，命中: {patterns}"
        idx = patterns.index("Gartley")
        assert directions[idx] == "bull"

    def test_bat_bull_detected(self) -> None:
        """精确 Bat 牛市比率 → 命中 Bat。"""
        pts = _build_bat_bull_pivots()
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "Bat" in patterns, f"Bat 未命中，命中: {patterns}"

    def test_butterfly_bull_detected(self) -> None:
        """精确 Butterfly 牛市比率 → 命中 Butterfly。"""
        pts = _build_butterfly_bull_pivots()
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "Butterfly" in patterns, f"Butterfly 未命中，命中: {patterns}"

    def test_crab_bull_detected(self) -> None:
        """精确 Crab 牛市比率 → 命中 Crab。"""
        pts = _build_crab_bull_pivots()
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "Crab" in patterns, f"Crab 未命中，命中: {patterns}"

    def test_broken_ratio_not_detected(self) -> None:
        """故意把 B 的 XA 回撤改成 0.1（远偏 Gartley 的 0.618±tol）→ Gartley 不命中。

        证明校验真在起作用，非永真。
        """
        X = 0.0
        A = 100.0
        # 破坏：B = A - XA * 0.1（远低于 Gartley 要求的 0.618）
        B = A - abs(A - X) * 0.1
        AB = abs(B - A)
        C = B + AB * 0.618
        BC = abs(C - B)
        D = C - BC * 1.618
        pivots = [
            (0, X, "L"), (1, A, "H"), (2, B, "L"), (3, C, "H"), (4, D, "L")
        ]
        results = detect_xabcd(pivots, tol=0.05)
        patterns = [r["pattern"] for r in results]
        assert "Gartley" not in patterns, "Gartley 错误命中（比率校验无效）"

    def test_bear_gartley_detected(self) -> None:
        """熊市 Gartley：X→A 下行（X 高点，A 低点）→ direction='bear'。"""
        X = 100.0
        A = 0.0
        XA = abs(X - A)  # = 100
        B = A + XA * 0.618  # 从 A 反弹
        AB = abs(B - A)
        C = B - AB * 0.618
        BC = abs(C - B)
        D = C + BC * 1.618
        pivots = [
            (0, X, "H"), (1, A, "L"), (2, B, "H"), (3, C, "L"), (4, D, "H")
        ]
        results = detect_xabcd(pivots, tol=0.06)
        patterns = [r["pattern"] for r in results]
        directions = [r["direction"] for r in results]
        assert "Gartley" in patterns, f"熊市 Gartley 未命中，命中: {patterns}"
        idx = patterns.index("Gartley")
        assert directions[idx] == "bear"

    def test_completed_flag_true(self) -> None:
        """detect_xabcd 返回的结果 completed=True。"""
        pts = _build_gartley_bull_pivots()
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.06)
        gartley_hits = [r for r in results if r["pattern"] == "Gartley"]
        assert gartley_hits, "Gartley 应命中"
        assert gartley_hits[0]["completed"] is True

    def test_prz_contains_d(self) -> None:
        """完整形态的 PRZ 区间应包含 D 点价格（PRZ 是 D 附近的目标区）。"""
        pts = _build_gartley_bull_pivots()
        D_price = pts[4][0]
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.06)
        gartley_hits = [r for r in results if r["pattern"] == "Gartley"]
        assert gartley_hits
        prz_lo, prz_hi = gartley_hits[0]["prz"]
        # PRZ 应在 D 附近，宽容一定偏差
        assert prz_lo <= D_price * 1.05 and prz_hi >= D_price * 0.95, (
            f"PRZ ({prz_lo:.2f}, {prz_hi:.2f}) 不覆盖 D={D_price:.2f}"
        )

    def test_confidence_range(self) -> None:
        """confidence 在 [0, 1] 之间。"""
        pts = _build_gartley_bull_pivots()
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.06)
        for r in results:
            assert 0.0 <= r["confidence"] <= 1.0, (
                f"confidence={r['confidence']} 超出 [0,1]"
            )


class TestProjectPrz:
    def test_gartley_forming_returned(self) -> None:
        """给 XABC（精确 Gartley），project_prz 返回 forming 列表，含 Gartley，completed=False。"""
        pts = _build_gartley_bull_pivots()
        X, A, B, C, D = [p[0] for p in pts]
        results = project_prz(X, A, B, C, direction="bull")
        patterns = [r["pattern"] for r in results]
        assert len(results) > 0, "project_prz 返回空"
        assert "Gartley" in patterns, f"Gartley 未出现在 forming: {patterns}"
        for r in results:
            assert r["completed"] is False

    def test_prz_contains_theoretical_d(self) -> None:
        """Gartley bull: 理论 D = A - |XA|*0.786，PRZ 应包含或接近理论 D。"""
        pts = _build_gartley_bull_pivots()
        X, A, B, C, D_actual = [p[0] for p in pts]
        XA = abs(A - X)
        D_theory = A - XA * 0.786   # Gartley D 在 XA 的 0.786

        results = project_prz(X, A, B, C, direction="bull")
        gartley_hits = [r for r in results if r["pattern"] == "Gartley"]
        assert gartley_hits, "Gartley 应出现在 forming"
        prz_lo, prz_hi = gartley_hits[0]["prz"]
        # PRZ 宽松判断：理论 D 在 PRZ ± 10%（PRZ 是区间，D_theory 在内或接近）
        assert prz_lo <= D_theory * 1.10 and prz_hi >= D_theory * 0.90, (
            f"PRZ ({prz_lo:.2f}, {prz_hi:.2f}) 未覆盖理论 D={D_theory:.2f}"
        )

    def test_sorted_by_confidence_desc(self) -> None:
        """project_prz 结果按 confidence 降序排列。"""
        pts = _build_gartley_bull_pivots()
        X, A, B, C, _ = [p[0] for p in pts]
        results = project_prz(X, A, B, C, direction="bull")
        confs = [r["confidence"] for r in results]
        assert confs == sorted(confs, reverse=True), "project_prz 结果未按 confidence 降序"

    def test_bear_direction(self) -> None:
        """熊市方向：X 高, A 低, 前瞻投射 D 在 A 上方（bear 形态）。"""
        X = 100.0
        A = 0.0
        XA = abs(X - A)
        B = A + XA * 0.618
        AB = abs(B - A)
        C = B - AB * 0.618
        results = project_prz(X, A, B, C, direction="bear")
        assert len(results) > 0, "bear 方向 project_prz 应返回结果"
        # bear: D 应在 A 上方（高于 A=0）
        for r in results:
            prz_lo, prz_hi = r["prz"]
            # bear Gartley D = A + |XA|*d_xa
            assert prz_hi > A, f"bear PRZ ({prz_lo:.2f},{prz_hi:.2f}) 应高于 A={A}"


class TestAnalyzeCandles:
    def test_returns_dict_with_keys(self) -> None:
        """analyze_candles 返回含 completed/forming/price 键的 dict。"""
        # 构造足够长的锯齿序列
        prices = [100.0 + 10 * math.sin(i * math.pi / 4) for i in range(50)]
        candles = [_C(h=p + 1, l=p - 1, c=p) for p in prices]
        result = analyze_candles(candles, order=2, tol=0.07)
        assert "completed" in result
        assert "forming" in result
        assert "price" in result

    def test_price_is_last_close(self) -> None:
        """price 字段为最后一根 K 线收盘价。"""
        prices = [float(i) + 100 for i in range(30)]
        candles = [_C(h=p + 1, l=p - 1, c=p) for p in prices]
        result = analyze_candles(candles, order=2)
        assert result["price"] == pytest.approx(prices[-1], abs=0.01)

    def test_too_short_returns_empty_lists(self) -> None:
        """极短 K 线序列（不足以找 5 枢轴）→ completed=[], forming=[]。"""
        candles = [_C(h=float(i), l=float(i) - 0.5, c=float(i) - 0.25) for i in range(5)]
        result = analyze_candles(candles, order=3)
        assert result["completed"] == []
        assert result["forming"] == []


class TestHarmonicRatios:
    def test_ratios_dict_has_required_patterns(self) -> None:
        """HARMONIC_RATIOS 必须包含 4 个 XA-anchored 命名形态（不含 ABCD/Shark/Cypher）。"""
        # Bug-1 修复后: 只保留 4 个 XA-anchored 形态；Cypher/Shark 需独立几何，已移至 backlog
        required = {"Gartley", "Bat", "Butterfly", "Crab"}
        assert required.issubset(set(HARMONIC_RATIOS.keys())), (
            f"缺少形态: {required - set(HARMONIC_RATIOS.keys())}"
        )

    def test_each_pattern_has_required_keys(self) -> None:
        """每个形态 dict 须含 b_xa, bc_ab, cd_bc, d_xa 键（基本比率结构）。"""
        for name, ratios in HARMONIC_RATIOS.items():
            for key in ("b_xa", "bc_ab", "cd_bc", "d_xa"):
                assert key in ratios, f"形态 {name} 缺少键 {key}"

    def test_gartley_b_xa_near_0618(self) -> None:
        """Gartley B 相对 XA 回撤应接近 0.618（与 Carney 标准/开源交叉校验对齐）。"""
        g = HARMONIC_RATIOS["Gartley"]
        lo, hi = g["b_xa"]
        # 0.618 应在区间内
        assert lo <= 0.618 <= hi, f"Gartley b_xa={g['b_xa']} 不包含 0.618"

    def test_bat_d_xa_near_0886(self) -> None:
        """Bat D 相对 XA 应在 0.886 附近（Carney Bat 特征）。"""
        b = HARMONIC_RATIOS["Bat"]
        lo, hi = b["d_xa"]
        assert lo <= 0.886 <= hi, f"Bat d_xa={b['d_xa']} 不包含 0.886"

    def test_crab_d_xa_near_1618(self) -> None:
        """Crab D 相对 XA 应在 1.618 附近（深度扩展）。"""
        c = HARMONIC_RATIOS["Crab"]
        lo, hi = c["d_xa"]
        assert lo <= 1.618 <= hi, f"Crab d_xa={c['d_xa']} 不包含 1.618"


# ========== TDD 新增缺陷测试（先 RED，再 GREEN）==========


class TestAbcdRemoved:
    """ABCD catch-all 已从 HARMONIC_RATIOS 删除，Shark/Cypher 也已移至 backlog。

    ABCD 是 4 点结构，列入 backlog 单独实现，不混入 5 点检测。
    Cypher/Shark 需独立几何（XC-anchored/5-0 标定），无法用 XA-schema 正确验证，已移至 backlog。
    """

    def test_abcd_not_in_harmonic_ratios(self) -> None:
        """'ABCD' 不应存在于 HARMONIC_RATIOS（已从 5 点检测中移除）。"""
        assert "ABCD" not in HARMONIC_RATIOS, (
            "ABCD 仍在 HARMONIC_RATIOS 中——应删除，避免过检测（缺陷4）"
        )

    def test_only_four_named_patterns(self) -> None:
        """HARMONIC_RATIOS 应只含 4 个 XA-anchored 命名形态（Bug-1 修复后）。"""
        expected = {"Gartley", "Bat", "Butterfly", "Crab"}
        assert set(HARMONIC_RATIOS.keys()) == expected, (
            f"形态集合不符: 实际={set(HARMONIC_RATIOS.keys())}, 期望={expected}"
        )


class TestFormingPrzWidthBounded:
    """缺陷1修复验证：project_prz 发散时不 emit 或 PRZ 宽 <6%。

    根因：当前代码 prz_lo=min(...) prz_hi=max(...) 取并集，发散时带宽 15%+。
    修复后：发散(gap>2*tol) 应跳过不 emit；收敛则 PRZ 宽 <6%。
    """

    @staticmethod
    def _make_divergent_xabc() -> tuple[float, float, float, float, str]:
        """构造 d_est1/d_est2 极度发散的 XABC：
        - 用精确 Gartley 比率确保 hits>=2（B 和 BC 都在范围内）
        - 故意使 C 位置令 BC 很长，使 cd_bc 投射远偏 d_xa 投射。
        """
        X = 0.0
        A = 100.0
        XA = abs(A - X)
        # B 在精确 Gartley 0.618
        B = A - XA * 0.618   # B=38.2
        AB = abs(B - A)      # AB=61.8
        # BC 故意超长：BC=AB*5（远超正常 0.886），使 d_est2 发散
        # 注意这会导致 r_bc_ab > Gartley bc_ab 上限但若 tol 大可能还在
        # 改用 BC=AB*0.618（合法），但令 C 在特殊位置使 cd_bc*BC 投射极远
        # 实际手法：让 C 远高于 B，使 BC 巨大
        C = B + AB * 4.0     # C=38.2+4*61.8=285.4（远偏正常，造成发散 d_est2）
        return X, A, B, C, "bull"

    def test_divergent_prz_not_emitted_or_narrow(self) -> None:
        """d_est1/d_est2 发散(>2*tol) → 不 emit，或 PRZ 宽 <6%。

        当前代码（缺陷1）: 会 emit 且 PRZ 很宽(15%+)。
        修复后: 发散形态应被跳过(not emit)，或若收敛则 PRZ <6%。
        """
        X, A, B, C, direction = self._make_divergent_xabc()
        price_ref = abs(A)  # 基准价格

        results = project_prz(X, A, B, C, direction=direction, tol=0.05)

        # 修复后所有 emit 的结果 PRZ 宽度必须 <6%
        for r in results:
            prz_lo, prz_hi = r["prz"]
            width_pct = (prz_hi - prz_lo) / max(price_ref, 1.0)
            assert width_pct < 0.06, (
                f"PRZ 过宽: {r['pattern']} prz={r['prz']} width={width_pct:.1%} (>6%)"
            )


class TestFormingConfidenceCappedAndHonest:
    """缺陷2修复验证：
    1. 仅 1 约束命中的 XABC → 不 emit (hits<2)。
    2. 2 约束+收敛 → confidence <=0.85 (封顶，诚实不过 85%)。
    """

    def test_single_hit_not_emitted(self) -> None:
        """仅 B 约束命中（BC 不在范围）→ project_prz 不 emit 任何结果。

        当前代码(缺陷2): hits<1 才跳过，所以 hits=1 会 emit。
        修复后: 需 hits>=2，hits=1 时不 emit。
        """
        # 用 Gartley B 比率但故意破坏 BC（使 BC/AB 远偏所有形态）
        X = 0.0
        A = 100.0
        XA = abs(A - X)
        # B 精确在 Gartley 0.618
        B = A - XA * 0.618
        AB = abs(B - A)
        # BC/AB = 10.0 → 极远偏，所有形态 bc_ab 上限 <2.618，不可能命中
        C = B + AB * 10.0

        results = project_prz(X, A, B, C, direction="bull", tol=0.05)

        assert len(results) == 0, (
            f"单约束命中不应 emit，但得到 {len(results)} 条: "
            f"{[r['pattern'] for r in results]}"
        )

    def test_confidence_capped_at_085(self) -> None:
        """2 约束+收敛情形下，confidence 不超过 0.85（诚实封顶）。

        当前代码(缺陷2): confidence 可能 0.98+（单约束近中心 ×0.5→0.98）。
        修复后: 所有 forming confidence <= 0.85。
        """
        pts = _build_gartley_bull_pivots()
        X, A, B, C, _ = [p[0] for p in pts]

        results = project_prz(X, A, B, C, direction="bull", tol=0.05)

        assert len(results) > 0, "精确 Gartley XABC 应有 forming 结果"
        for r in results:
            assert r["confidence"] <= 0.85, (
                f"confidence 超过 0.85 上限: {r['pattern']} conf={r['confidence']:.3f}"
            )


class TestFormingRequiresConvergence:
    """缺陷1修复：d_est1/d_est2 gap>2*tol → 不 emit（降噪）。"""

    def test_large_gap_not_emitted(self) -> None:
        """构造 BC 特别长使 d_est2 远离 d_est1（gap > 2*tol=0.10) → 不 emit。

        当前代码(缺陷1): 此情形仍 emit 且 PRZ 覆盖整个发散区间。
        修复后: gap>2*tol 直接跳过。
        """
        # 精确命中 Gartley B+BC 约束，但令 CD 投射使两估计极度发散
        X = 0.0
        A = 1000.0
        XA = abs(A - X)

        # B 精确在 Gartley 0.618
        B = A - XA * 0.618   # B = 382

        AB = abs(B - A)       # 618
        # BC/AB = 0.618（合法 Gartley bc_ab）
        C = B + AB * 0.618    # C = 382 + 381.9 ≈ 763.9

        # d_est1 = A - XA * 0.786 = 1000 - 786 = 214
        # BC 正常，cd_bc_mid = (1.13+1.618)/2 = 1.374 → d_est2 = C - BC*1.374
        BC_val = abs(C - B)   # ~381.9
        d_est1 = A - XA * 0.786   # 214
        cd_bc_mid = (1.13 + 1.618) / 2
        d_est2 = C - BC_val * cd_bc_mid  # 763.9 - 525 ≈ 238.8
        gap_ratio = abs(d_est1 - d_est2) / max(A, 1.0)
        # gap~0.025 对于这组数据实际收敛

        # 换一组：使 C 极高使 BC 巨大，d_est2 远低
        C2 = B + AB * 2.0    # C = 382 + 1236 = 1618
        BC2 = abs(C2 - B)    # 1236
        # d_est2 = C2 - BC2 * cd_bc_mid = 1618 - 1236*1.374 ≈ 1618 - 1698 ≈ -80
        d_est2_new = C2 - BC2 * cd_bc_mid  # 约 -80
        gap_new = abs(d_est1 - d_est2_new) / max(A, 1.0)  # (214-(-80))/1000 = 0.294 >> 0.10

        # 验证 gap 确实大
        assert gap_new > 0.10, f"gap={gap_new:.3f} 不够大，测试前提不成立"

        results = project_prz(X, A, B, C2, direction="bull", tol=0.05)

        for r in results:
            prz_lo, prz_hi = r["prz"]
            width_pct = (prz_hi - prz_lo) / max(A, 1.0)
            assert width_pct < 0.06, (
                f"发散 PRZ 过宽: {r['pattern']} prz={r['prz']} width={width_pct:.1%}"
            )


class TestCompletedPrzContainsD:
    """缺陷3修复验证：detect_xabcd 完整形态 PRZ 必含已知 D 点。

    根因：当前代码重投射宽带，D 可能不在 PRZ 内。
    修复后：PRZ = (D_px*(1-tol), D_px*(1+tol))，必含 D。
    """

    def test_completed_prz_contains_d_gartley(self) -> None:
        """精确 Gartley 完整形态：PRZ 必须严格包含 D 点价格。

        当前代码(缺陷3): PRZ 用 d_xa 区间投射，D 不一定在内。
        修复后: prz[0] <= D_px <= prz[1] 严格成立。
        """
        pts = _build_gartley_bull_pivots()
        D_px = pts[4][0]
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]

        results = detect_xabcd(pivots, tol=0.06)
        gartley_hits = [r for r in results if r["pattern"] == "Gartley"]
        assert gartley_hits, "Gartley 应命中（精确比率）"

        for hit in gartley_hits:
            prz_lo, prz_hi = hit["prz"]
            assert prz_lo <= D_px <= prz_hi, (
                f"PRZ ({prz_lo:.4f}, {prz_hi:.4f}) 不含 D_px={D_px:.4f}"
            )

    def test_completed_confidence_capped_at_090(self) -> None:
        """完整形态 confidence 不超过 0.90（诚实封顶）。"""
        pts = _build_gartley_bull_pivots()
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.06)

        for r in results:
            assert r["confidence"] <= 0.90, (
                f"完整形态 confidence 超过 0.90: {r['pattern']} conf={r['confidence']:.3f}"
            )


class TestDedupOnePatternPerWindow:
    """缺陷4修复验证：同一 5 枢轴窗口多形态命中 → 只保留最高 confidence 1 条。

    当前代码：同窗所有命中都 emit（导致 ETH 1H 6个 ABCD 重叠等噪音）。
    修复后：每窗只保留最高 confidence 的 1 条。
    """

    def test_dedup_one_pattern_per_d_pivot(self) -> None:
        """同一 D 枢轴 idx，若多形态命中，只保留 confidence 最高的 1 个。

        构造方法：Bat 和 Crab 的 B 区间有重叠(均含 0.382 XA)，取精确中间值
        可能同时命中多个，修复后同 D_idx 只 1 条。
        """
        # 用 Bat 精确枢轴（D_idx=4）
        pts = _build_bat_bull_pivots()
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.10)  # 放宽 tol 使多形态可能同时命中

        # 按 D_idx 分组
        from collections import defaultdict
        by_d_idx: dict = defaultdict(list)
        for r in results:
            d_idx = r["points"]["D"][0]
            by_d_idx[d_idx].append(r)

        for d_idx, hits in by_d_idx.items():
            assert len(hits) == 1, (
                f"D_idx={d_idx} 有 {len(hits)} 条形态: "
                f"{[h['pattern'] for h in hits]} (应去重为 1 条)"
            )

    def test_no_abcd_in_completed(self) -> None:
        """完整形态检测结果中不含 'ABCD'（已从 5 点检测删除）。"""
        pts = _build_gartley_bull_pivots()
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.06)
        patterns = [r["pattern"] for r in results]
        assert "ABCD" not in patterns, "ABCD 不应出现在 detect_xabcd 结果中"


class TestFindPivotsStrictComparison:
    """缺陷4修复：find_pivots 用严格 greater/less，避免平台点产生重复枢轴。"""

    def test_flat_plateau_no_duplicate_pivot(self) -> None:
        """水平平台（连续相同 high）不应产生多个 H 枢轴在相同价格。

        当前代码(缺陷4): argrelextrema greater_equal 会把平台每个点都标为极值。
        修复后: 严格 greater 只取唯一极值，再经 _clean_alternating 去重。
        """
        import numpy as np
        # 构造: 上升到平台再下降，high 值: 90,100,100,100,90,80,90,85,80
        # 平台 100 出现 3 次
        highs = [90.0, 100.0, 100.0, 100.0, 90.0, 80.0, 90.0, 85.0, 80.0,
                 95.0, 85.0, 75.0, 90.0, 85.0, 80.0, 70.0, 80.0, 75.0, 70.0]
        lows  = [85.0,  95.0,  95.0,  95.0, 85.0, 75.0, 85.0, 80.0, 75.0,
                 90.0, 80.0, 70.0, 85.0, 80.0, 75.0, 65.0, 75.0, 70.0, 65.0]

        class _FC:
            __slots__ = ("h", "l", "c")
            def __init__(self, h: float, l: float) -> None:
                self.h = h; self.l = l; self.c = (h + l) / 2

        candles = [_FC(h, l) for h, l in zip(highs, lows)]
        pivots = find_pivots(candles, order=1)

        # 检验：100 不应出现 3 次
        h_prices = [p[1] for p in pivots if p[2] == "H"]
        assert h_prices.count(100.0) <= 1, (
            f"平台枢轴 100.0 出现 {h_prices.count(100.0)} 次（应<=1），缺陷4未修"
        )


# ========== TDD Bug-1..T-5 新增测试（先 RED，再 GREEN）==========


class TestCypherSharkRemoved:
    """Bug-1 修复验证：Cypher 和 Shark 从 HARMONIC_RATIOS 删除，只留 4 个 XA-anchored 形态。"""

    def test_cypher_not_in_ratios(self) -> None:
        """'Cypher' 不在 HARMONIC_RATIOS（用 XC 定位，与 XA-schema 不兼容）。"""
        assert "Cypher" not in HARMONIC_RATIOS, (
            "Cypher 仍在 HARMONIC_RATIOS——需删除（XC-anchored D，无法用 XA-schema 正确验证）"
        )

    def test_shark_not_in_ratios(self) -> None:
        """'Shark' 不在 HARMONIC_RATIOS（O-X-A-B-C 5-0 标定，与 XABCD schema 不兼容）。"""
        assert "Shark" not in HARMONIC_RATIOS, (
            "Shark 仍在 HARMONIC_RATIOS——需删除（5-0 标定，与 XABCD schema 不兼容）"
        )

    def test_exactly_four_patterns(self) -> None:
        """HARMONIC_RATIOS 恰好包含 4 个 XA-anchored 形态。"""
        expected = {"Gartley", "Bat", "Butterfly", "Crab"}
        assert set(HARMONIC_RATIOS.keys()) == expected, (
            f"期望 4 个形态 {expected}，实际: {set(HARMONIC_RATIOS.keys())}"
        )


class TestNoMislabelCypherAsGartley:
    """Bug-1 根治：Cypher 从 HARMONIC_RATIOS 删除后，不再以 "Cypher" 名称输出任何结果。

    旧代码误用 XA-schema 套 Cypher 的 XC-anchored D，导致 Cypher 比例输入被误标。
    修复后：HARMONIC_RATIOS 无 Cypher，不会再以 "Cypher" 标签输出任何形态。
    注意：真 Cypher 的价格参数(X=0,A=100,B=38,C=130,D=27.8) 恰好部分满足 Gartley
    比率约束（B/XA=0.62 满足，D/XA=0.722 在 Gartley D 区间内），这是合法的 Gartley 命中，
    与"Cypher 被误标"是不同问题——后者是指 Cypher 标签本身不应存在于输出中。
    """

    def test_cypher_label_never_output(self) -> None:
        """任何输入都不会以 'Cypher' 标签输出（Cypher 已从 HARMONIC_RATIOS 删除）。"""
        X, A, B, C, D = 0.0, 100.0, 38.0, 130.0, 27.8
        pivots = [
            (0, X, "L"), (1, A, "H"), (2, B, "L"), (3, C, "H"), (4, D, "L")
        ]
        results = detect_xabcd(pivots, tol=0.06)
        cypher_hits = [r for r in results if r["pattern"] == "Cypher"]
        assert len(cypher_hits) == 0, (
            f"Cypher 已从 HARMONIC_RATIOS 删除，不应出现 'Cypher' 标签: {cypher_hits}"
        )

    def test_shark_label_never_output(self) -> None:
        """任何输入都不会以 'Shark' 标签输出（Shark 已从 HARMONIC_RATIOS 删除）。"""
        X, A, B, C, D = 0.0, 100.0, 38.0, 130.0, 27.8
        pivots = [
            (0, X, "L"), (1, A, "H"), (2, B, "L"), (3, C, "H"), (4, D, "L")
        ]
        results = detect_xabcd(pivots, tol=0.06)
        shark_hits = [r for r in results if r["pattern"] == "Shark"]
        assert len(shark_hits) == 0, (
            f"Shark 已从 HARMONIC_RATIOS 删除，不应出现 'Shark' 标签: {shark_hits}"
        )


class TestButterflyDxaBoundary:
    """Bug-4 修复：Butterfly d_xa 收紧为 (1.272, 1.618)，与 Carney 标准一致。"""

    def test_d_xa_170_not_butterfly(self) -> None:
        """r_d_xa=1.70 的窗口不命中 Butterfly（旧 1.72 上界会误纳，新 1.618 上界不纳）。

        构造 bull 窗口：X=0,A=100,B=21.4(0.786 XA),C=B+AB*0.618,D=A-100*1.70=-70。
        r_d_xa = |D-A|/XA = 170/100 = 1.70 > 1.618*(1+0.05)=1.699（刚好在新界外）。
        """
        X = 0.0
        A = 100.0
        XA = 100.0
        # B=0.786 (Butterfly b_xa 中心)
        B = A - XA * 0.786    # 21.4
        AB = abs(B - A)        # 78.6
        C = B + AB * 0.618     # 21.4 + 48.6 = 70.0
        D = A - XA * 1.70      # 100 - 170 = -70
        pivots = [
            (0, X, "L"), (1, A, "H"), (2, B, "L"), (3, C, "H"), (4, D, "L")
        ]
        results = detect_xabcd(pivots, tol=0.05)
        butterfly_hits = [r for r in results if r["pattern"] == "Butterfly"]
        assert len(butterfly_hits) == 0, (
            f"r_d_xa=1.70 不应命中 Butterfly（新上界 1.618），但得到: {butterfly_hits}"
        )

    def test_d_xa_140_is_butterfly(self) -> None:
        """r_d_xa=1.40 的窗口命中 Butterfly（在新 1.272~1.618 范围内）。

        D = A - XA*1.40，在新区间 (1.272, 1.618) 的中部，应命中。
        """
        X = 0.0
        A = 100.0
        XA = 100.0
        B = A - XA * 0.786    # 21.4
        AB = abs(B - A)        # 78.6
        C = B + AB * 0.618     # 70.0
        D = A - XA * 1.40      # -40
        pivots = [
            (0, X, "L"), (1, A, "H"), (2, B, "L"), (3, C, "H"), (4, D, "L")
        ]
        results = detect_xabcd(pivots, tol=0.05)
        butterfly_hits = [r for r in results if r["pattern"] == "Butterfly"]
        assert len(butterfly_hits) > 0, (
            f"r_d_xa=1.40 应命中 Butterfly（在 1.272~1.618 内），但未命中。"
            f" 全部命中: {[r['pattern'] for r in results]}"
        )


class TestDirectionTypeConsistency:
    """Bug-3 修复：bull 窗口要求类型序列 [L,H,L,H,L]，bear 要求 [H,L,H,L,H]。"""

    def test_all_highs_sequence_not_detected(self) -> None:
        """全 H 类型序列（几何无效）即使价格比率凑巧也不输出形态。"""
        # 精确 Gartley bull 价格，但类型全部设为 H（非交替）
        pts = _build_gartley_bull_pivots()
        pivots = [
            (i, price, "H")  # 故意全 H
            for i, (price, kind) in enumerate(pts)
        ]
        results = detect_xabcd(pivots, tol=0.06)
        assert len(results) == 0, (
            f"全 H 序列（几何无效）不应输出形态，但得到: {[r['pattern'] for r in results]}"
        )

    def test_mixed_type_mismatch_not_detected(self) -> None:
        """类型不符合 bull [L,H,L,H,L]（如 [H,H,L,H,L]）不输出形态。"""
        pts = _build_gartley_bull_pivots()
        # 改 X 类型为 H（本应 L），破坏 bull 序列约束
        pivots = [
            (0, pts[0][0], "H"),  # X 应为 L 但改 H
            (1, pts[1][0], "H"),
            (2, pts[2][0], "L"),
            (3, pts[3][0], "H"),
            (4, pts[4][0], "L"),
        ]
        results = detect_xabcd(pivots, tol=0.06)
        assert len(results) == 0, (
            f"类型不匹配序列不应输出形态，但得到: {[r['pattern'] for r in results]}"
        )

    def test_valid_bull_type_sequence_detected(self) -> None:
        """正确 bull 类型序列 [L,H,L,H,L] → 正常命中形态。"""
        pts = _build_gartley_bull_pivots()
        pivots = [(i, price, kind) for i, (price, kind) in enumerate(pts)]
        results = detect_xabcd(pivots, tol=0.06)
        assert len(results) > 0, "有效 bull 序列应命中形态"

    def test_valid_bear_type_sequence_detected(self) -> None:
        """正确 bear 类型序列 [H,L,H,L,H] → 正常命中形态。"""
        X = 100.0
        A = 0.0
        XA = abs(X - A)
        B = A + XA * 0.618
        AB = abs(B - A)
        C = B - AB * 0.618
        BC = abs(C - B)
        D = C + BC * 1.618
        pivots = [
            (0, X, "H"), (1, A, "L"), (2, B, "H"), (3, C, "L"), (4, D, "H")
        ]
        results = detect_xabcd(pivots, tol=0.06)
        assert len(results) > 0, "有效 bear 序列应命中形态"


class TestFindPivotsUsesPatternsFallback:
    """Bug-2 修复：find_pivots 直接用 patterns.swing_highs/lows，无 scipy 依赖。"""

    def test_find_pivots_works_without_scipy(self) -> None:
        """find_pivots 在无 scipy 下正常返回交替枢轴（直接测，不 mock scipy）。"""
        import math
        prices = [100.0 + 10 * math.sin(i * math.pi / 3) for i in range(30)]
        candles = [_C(h=p + 1, l=p - 1, c=p) for p in prices]
        pivots = find_pivots(candles, order=2)
        # 无 scipy 时 patterns fallback 应正常工作
        assert len(pivots) >= 5, f"find_pivots 应返回 >=5 个枢轴，实际 {len(pivots)}"
        # 交替检验
        for i in range(1, len(pivots)):
            assert pivots[i][2] != pivots[i - 1][2], (
                f"枢轴 {i-1}({pivots[i-1][2]}) 和 {i}({pivots[i][2]}) 类型相同"
            )

    def test_find_pivots_no_scipy_import_in_module(self) -> None:
        """harmonic.py 主路径不依赖 scipy（已移除 try/import）。

        用 ast 检查模块源码：import scipy 应不存在。
        """
        import ast
        import importlib.util

        spec = importlib.util.find_spec("smc_tracker.indicators.harmonic")
        assert spec is not None and spec.origin is not None
        source = open(spec.origin).read()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "scipy" not in alias.name, (
                            "harmonic.py 主路径仍有 'import scipy'——应已移除"
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module and "scipy" in node.module:
                        raise AssertionError(
                            f"harmonic.py 主路径仍有 'from scipy...' 导入——应已移除"
                        )


class TestStructuralOrder:
    """结构次序校验：4 形态须 X<B<C<A(bull)/X>B>C>A(bear)，拒绝 C 超 A 的几何无效形态。

    回归：曾把真 Cypher(C=130>A=100，幅度比率凑巧满足) 误标为 Gartley 0.9；
    加结构次序校验后应被拒绝（不再仅靠幅度比率）。
    """

    def test_c_above_a_rejected_bull(self) -> None:
        """bull 形态中 C 超过 A（C>A）→ 不命中任何形态（结构无效）。"""
        # 真 Cypher 几何：X=0,A=100,B=38,C=130(>A),D=27.8 —— 幅度比率曾误中 Gartley
        cyp = [(0, 0.0, "L"), (10, 100.0, "H"), (20, 38.0, "L"),
               (30, 130.0, "H"), (40, 27.8, "L")]
        results = detect_xabcd(cyp, tol=0.05)
        assert results == [], f"C>A 结构无效应被拒，却命中 {[r['pattern'] for r in results]}"

    def test_normal_gartley_still_detected(self) -> None:
        """正常 Gartley（C<A）不受结构校验影响，仍正常命中。"""
        g = [(0, 100.0, "L"), (10, 200.0, "H"), (20, 138.2, "L"),
             (30, 176.4, "H"), (40, 121.4, "L")]  # C=176.4 < A=200
        results = detect_xabcd(g, tol=0.06)
        assert any(r["pattern"] == "Gartley" and r["direction"] == "bull" for r in results)

    def test_c_below_a_rejected_bear(self) -> None:
        """bear 形态中 C 低于 A（应 C>A）→ 不命中（结构无效，镜像）。"""
        bad = [(0, 100.0, "H"), (10, 0.0, "L"), (20, 62.0, "H"),
               (30, -30.0, "L"), (40, 72.2, "H")]  # C=-30 < A=0
        results = detect_xabcd(bad, tol=0.05)
        assert results == [], f"bear C<A 结构无效应被拒，却命中 {[r['pattern'] for r in results]}"


class TestAnalyzeCandlesFormingOrderValidation:
    """🟡 审计缺陷：analyze_candles forming 路径缺结构次序校验。

    analyze_candles 取最后 4 枢轴(XABC)直接 project_prz，未做
    completed 路径已有的 X<B<C<A(bull) / X>B>C>A(bear) 几何校验 →
    可能投射结构不成立的前瞻 PRZ。

    修复：在 project_prz 前校验 last4(X,A,B,C) 次序，不满足则 forming=[]。
    注：校验仅用价格，不做类型校验（last4 已经是枢轴交替序列）。
    """

    def test_forming_empty_when_invalid_bull_order(self) -> None:
        """bull(A>X)时 C>A（X<B<C<A 不满足）→ analyze_candles forming 为空。

        monkeypatch find_pivots 返回 5 枚举点（保证 len>=5 通过初始守卫），
        其中 last4=pivots[-4:]=[A,B,C,D_fake]，direction 由 last4[1]>last4[0] 决定。
        同时 monkeypatch project_prz 返回假结果，验证次序校验会拦截无效结构。

        修复前：analyze_candles 直接调用 project_prz（不做次序校验），结果透传到 forming。
        修复后：次序校验拦截，forming=[]（project_prz 不被调用）。

        构造：5 枚举点，最后4=[(10,H),(50,L),(200,H),(5,L)]。
        last4: X=10, A=50 (A>X → bull), B=200, C=5。
        bull 次序要求 X<B<C<A: 10<200<5<50 → 200<5 False → order_ok=False。
        """
        from unittest.mock import patch, MagicMock
        import smc_tracker.indicators.harmonic as _harm

        # 5 枚举点（需 >=5 通过守卫）；last4 的 B=200>C=5 使 bull 次序失败
        fake_pivots = [
            (0,  5.0,   "L"),   # 第1点（不影响 last4）
            (1,  10.0,  "H"),   # X（last4[0]）
            (2,  50.0,  "L"),   # A（last4[1]，A>X → bull）
            (3,  200.0, "H"),   # B（last4[2]，B>A → bull 次序 B<A=50 必须，但 B=200>A=50，违反）
            (4,  5.0,   "L"),   # C（last4[3]，bull 须 X<B<C<A: 10<200<5<50 不成立）
        ]

        # 让 project_prz 假装有结果（模拟有形态刚好命中的场景）
        fake_prz_result = [{"pattern": "FakePat", "direction": "bull",
                            "prz": (10.0, 20.0), "completed": False,
                            "confidence": 0.7, "confluence": 1}]

        dummy_candles = [_C(h=1, l=0, c=0.5)] * 5

        with patch.object(_harm, "find_pivots", return_value=fake_pivots):
            with patch.object(_harm, "project_prz", return_value=fake_prz_result):
                result = _harm.analyze_candles(dummy_candles, order=2, tol=0.05)

        assert result["forming"] == [], (
            f"bull 次序违规（B>A），forming 应为 []，实际 {result['forming']}。"
            f"修复前 analyze_candles 未做次序校验，project_prz 的假结果会直接透传。"
        )

    def test_forming_nonempty_when_valid_bull_order(self) -> None:
        """正常 bull XABC（X<B<C<A）→ forming 次序校验不误杀，project_prz 结果透传。

        构造 5 枚举点，last4=[X,A,B,C] 满足 bull 次序 X<B<C<A。
        monkeypatch project_prz 返回确定性结果，验证结果被透传（次序合法不被拒绝）。
        """
        from unittest.mock import patch
        import smc_tracker.indicators.harmonic as _harm

        # 5 枚举点；last4 = pivots[-4:] = [(X,L),(A,H),(B,L),(C,H)]
        # X=0<B=38<C=76<A=100 → bull 次序正确
        fake_pivots = [
            (0,  50.0,  "H"),   # 第1点（不影响 last4）
            (1,  0.0,   "L"),   # X（last4[0]）
            (2,  100.0, "H"),   # A（last4[1]，A>X → bull）
            (3,  38.0,  "L"),   # B（last4[2]，X<B: 0<38 OK）
            (4,  76.0,  "H"),   # C（last4[3]，X<B<C<A: 0<38<76<100 OK）
        ]

        fake_prz_result = [{"pattern": "Gartley", "direction": "bull",
                            "prz": (15.0, 25.0), "completed": False,
                            "confidence": 0.75, "confluence": 2}]

        dummy_candles = [_C(h=1, l=0, c=0.5)] * 5

        with patch.object(_harm, "find_pivots", return_value=fake_pivots):
            with patch.object(_harm, "project_prz", return_value=fake_prz_result):
                result = _harm.analyze_candles(dummy_candles, order=2, tol=0.06)

        # 次序正确，project_prz 的结果应被透传到 forming
        assert result["forming"] == fake_prz_result, (
            f"合法次序 forming 应透传 project_prz 结果，实际 {result['forming']}"
        )

    def test_forming_empty_when_invalid_bear_order(self) -> None:
        """bear(A<X)时 C>B（X>B>C>A 不满足）→ analyze_candles forming 为空。

        构造 5 枚举点，last4=[X,A,B,C]，bear 方向但 C>B（次序违规）。
        monkeypatch project_prz 返回假结果，验证次序校验拦截。

        last4: X=100, A=20 (A<X → bear), B=60, C=80。
        bear 次序要求 X>B>C>A: 100>60>80>20 → 60>80 False → order_ok=False。
        """
        from unittest.mock import patch
        import smc_tracker.indicators.harmonic as _harm

        # 5 枚举点；last4 = pivots[-4:] = [(X,H),(A,L),(B,H),(C,L)]
        # bear 方向(A=20<X=100)，但 C=80>B=60，违反 B>C
        fake_pivots = [
            (0,  50.0,  "L"),   # 第1点（不影响 last4）
            (1,  100.0, "H"),   # X（last4[0]）
            (2,  20.0,  "L"),   # A（last4[1]，A<X → bear）
            (3,  60.0,  "H"),   # B（last4[2]）
            (4,  80.0,  "L"),   # C（last4[3]，bear 须 B>C: 60>80 False → 违规）
        ]

        fake_prz_result = [{"pattern": "FakePat", "direction": "bear",
                            "prz": (85.0, 95.0), "completed": False,
                            "confidence": 0.6, "confluence": 1}]

        dummy_candles = [_C(h=1, l=0, c=0.5)] * 5

        with patch.object(_harm, "find_pivots", return_value=fake_pivots):
            with patch.object(_harm, "project_prz", return_value=fake_prz_result):
                result = _harm.analyze_candles(dummy_candles, order=2, tol=0.05)

        assert result["forming"] == [], (
            f"bear B<C 结构无效，forming 应为 []，实际 {result['forming']}。"
            f"修复前 analyze_candles 未做次序校验，project_prz 的假结果会直接透传。"
        )
