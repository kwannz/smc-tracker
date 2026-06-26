"""谐波扩展形态测试：Cypher / Shark / AB=CD。

严格合成几何构造测试策略：
1. 按精确比率构造完美形态的枢轴序列 → 断言能检出且方向/点位正确。
2. 故意偏离比率 → 断言不检出（防止过检测，验证校验不为永真）。
3. 验证 dict 契约字段与 detect_xabcd 完全一致。
4. 验证置信封顶（completed≤0.90, forming≤0.85）。

⚠️ 各形态实测胜率均未知，测试仅验证几何/比率逻辑正确性，不代表信号可靠性。
"""
from __future__ import annotations

import pytest

from smc_tracker.indicators.harmonic_ext import (
    detect_cypher,
    project_cypher_prz,
    detect_shark,
    project_shark_prz,
    detect_abcd,
    project_abcd_prz,
    detect_all_ext,
)


# ===========================================================================
# 辅助：dict 契约字段集合（与 detect_xabcd 一致）
# ===========================================================================
_REQUIRED_KEYS_COMPLETED = frozenset(
    {"pattern", "direction", "points", "prz", "completed", "confidence", "confluence"}
)
_REQUIRED_KEYS_FORMING = frozenset(
    {"pattern", "direction", "prz", "completed", "confidence", "confluence"}
)
_REQUIRED_POINT_KEYS = frozenset({"X", "A", "B", "C", "D"})


def _assert_contract(result: dict, completed: bool) -> None:
    """断言单条结果满足 dict 契约与置信封顶。

    completed=True：需含 points 字段（与 detect_xabcd 一致）。
    completed=False：forming 阶段 D 未确认，无 points（与 project_prz 一致）。
    """
    required = _REQUIRED_KEYS_COMPLETED if completed else _REQUIRED_KEYS_FORMING
    assert required.issubset(result.keys()), (
        f"缺少字段: {required - result.keys()}"
    )
    if completed:
        assert _REQUIRED_POINT_KEYS.issubset(result["points"].keys()), (
            f"points 缺少键: {_REQUIRED_POINT_KEYS - result['points'].keys()}"
        )
    prz = result["prz"]
    assert isinstance(prz, tuple) and len(prz) == 2
    assert prz[0] <= prz[1], f"PRZ 顺序错误: {prz}"
    assert result["completed"] == completed
    conf = result["confidence"]
    assert 0.0 <= conf
    if completed:
        assert conf <= 0.90, f"completed 置信超限: {conf}"
    else:
        assert conf <= 0.85, f"forming 置信超限: {conf}"


def _make_pivots(pts: list[tuple[float, str]]) -> list[tuple[int, float, str]]:
    """从 [(price,'H'|'L'), ...] 生成 [(idx, price, kind), ...] 序列。"""
    return [(i, price, kind) for i, (price, kind) in enumerate(pts)]


# ===========================================================================
# Cypher 测试
# ===========================================================================

class TestCypher:
    """Cypher 形态：bull 结构 X(L) A(H) B(L) C(H,C>A) D(L)。

    比率：B/XA=0.382–0.618；C/XA=1.272–1.414；D/XC=0.786。
    ⚠️ 实测胜率未知。
    """

    def _build_bull(self) -> list[tuple[float, str]]:
        """构造精确 Cypher 牛市枢轴。

        X=0, A=100(XA=100)
        B = A - XA*0.50 = 50 (B/XA=0.50，在 0.382–0.618 范围内)
        C = X + XA*1.35 = 135 (C/XA=1.35，在 1.272–1.414 范围内，C>A✓)
        XC = |C - X| = 135
        D = C - XC*0.786 = 135 - 135*0.786 ≈ 28.89 (D/XC=0.786)
        """
        X = 0.0
        A = 100.0
        XA = abs(A - X)
        B = A - XA * 0.50    # B/XA=0.50
        C = X + XA * 1.35    # C/XA=1.35（C>A，Cypher 核心）
        XC = abs(C - X)
        D = C - XC * 0.786   # D = C - 0.786*XC
        return [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]

    def _build_bear(self) -> list[tuple[float, str]]:
        """构造精确 Cypher 熊市枢轴（bull 镜像）。

        X=100, A=0(XA=100, A<X)
        B = A + XA*0.50 = 50 (B/XA=0.50)
        C = X - XA*1.35 = 100-135 = -35 (C<A, C<X；bear: C<A)
        XC = |C - X| = 135
        D = C + XC*0.786
        """
        X = 100.0
        A = 0.0
        XA = abs(A - X)
        B = A + XA * 0.50
        C = X - XA * 1.35    # C<A（bear Cypher 核心）
        XC = abs(C - X)
        D = C + XC * 0.786
        return [(X, "H"), (A, "L"), (B, "H"), (C, "L"), (D, "H")]

    def test_cypher_bull_detected(self) -> None:
        """精确 Cypher 牛市比率 → 命中 Cypher bull。"""
        pts = self._build_bull()
        pivots = _make_pivots(pts)
        results = detect_cypher(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        directions = [r["direction"] for r in results]
        assert "Cypher" in patterns, f"Cypher 未命中，命中: {patterns}"
        idx = patterns.index("Cypher")
        assert directions[idx] == "bull"

    def test_cypher_bull_contract(self) -> None:
        """Cypher bull 结果满足 dict 契约。"""
        pts = self._build_bull()
        pivots = _make_pivots(pts)
        results = detect_cypher(pivots, tol=0.07)
        assert results, "Cypher 未检出"
        _assert_contract(results[0], completed=True)

    def test_cypher_bear_detected(self) -> None:
        """精确 Cypher 熊市 → 命中 Cypher bear。"""
        pts = self._build_bear()
        pivots = _make_pivots(pts)
        results = detect_cypher(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "Cypher" in patterns, f"Cypher bear 未命中，命中: {patterns}"
        idx = patterns.index("Cypher")
        assert results[idx]["direction"] == "bear"

    def test_cypher_wrong_b_ratio_not_detected(self) -> None:
        """B/XA=0.10（远偏 0.382–0.618）→ Cypher 不检出（防过检测）。"""
        X = 0.0
        A = 100.0
        XA = abs(A - X)
        B = A - XA * 0.10  # 故意偏离：B/XA=0.10
        C = X + XA * 1.35
        XC = abs(C - X)
        D = C - XC * 0.786
        pts = [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]
        pivots = _make_pivots(pts)
        results = detect_cypher(pivots, tol=0.05)
        patterns = [r["pattern"] for r in results]
        assert "Cypher" not in patterns, f"不应命中 Cypher，但命中了: {patterns}"

    def test_cypher_c_not_exceeding_a_not_detected(self) -> None:
        """C 不超越 A（C=0.90*A < A）→ 不是 Cypher，不应命中。"""
        X = 0.0
        A = 100.0
        XA = abs(A - X)
        B = A - XA * 0.50
        C = A - XA * 0.10  # C < A，不满足 Cypher C>A
        XC = abs(C - X)
        D = C - XC * 0.786
        pts = [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]
        # 注意：C<A 时，交替序列中 C 不会以 H 出现在正确位置（几何矛盾），但枚举仍要检查
        # 手工绕过枢轴找点，直接构造 [(idx, price, kind)]
        pivots = [
            (0, X, "L"), (1, A, "H"), (2, B, "L"), (3, C, "H"), (4, D, "L")
        ]
        results = detect_cypher(pivots, tol=0.05)
        patterns = [r["pattern"] for r in results]
        assert "Cypher" not in patterns, "C<A 不应命中 Cypher"

    def test_cypher_points_correct(self) -> None:
        """Cypher 检出结果的 points 价格与构造值匹配（±0.01 精度）。"""
        pts = self._build_bull()
        X_expected = pts[0][0]
        D_expected = pts[4][0]
        pivots = _make_pivots(pts)
        results = detect_cypher(pivots, tol=0.07)
        assert results
        r = results[0]
        assert abs(r["points"]["X"][1] - X_expected) < 0.01
        assert abs(r["points"]["D"][1] - D_expected) < 0.01

    def test_cypher_confidence_capped(self) -> None:
        """Cypher 完整形态置信封顶 0.90。"""
        pts = self._build_bull()
        pivots = _make_pivots(pts)
        results = detect_cypher(pivots, tol=0.07)
        for r in results:
            assert r["confidence"] <= 0.90

    def test_project_cypher_prz_bull(self) -> None:
        """前瞻投射：已知 XABC → 产出 forming PRZ，置信≤0.85。"""
        pts = self._build_bull()
        X, A, B, C = pts[0][0], pts[1][0], pts[2][0], pts[3][0]
        results = project_cypher_prz(X, A, B, C, direction="bull", tol=0.07)
        assert results, "Cypher 前瞻未产出"
        r = results[0]
        _assert_contract(r, completed=False)
        assert r["pattern"] == "Cypher"
        assert r["direction"] == "bull"

    def test_project_cypher_bad_c_no_output(self) -> None:
        """C 未超越 A → 前瞻不产出。"""
        X, A, B, C = 0.0, 100.0, 50.0, 90.0  # C=90<A=100
        results = project_cypher_prz(X, A, B, C, direction="bull", tol=0.05)
        assert results == [], "C<A 不应产出 Cypher 前瞻"


# ===========================================================================
# Shark 测试
# ===========================================================================

class TestShark:
    """Shark 形态：bull 结构 X(L) A(H) B(L,B<X) C(H) D(L)。

    比率：B/XA=1.13–1.618（扩展）；C/AB=1.618–2.24；D/XA=0.826–0.946（0.886）。
    ⚠️ 实测样本稀少，胜率未知。
    """

    def _build_bull(self) -> list[tuple[float, str]]:
        """构造精确 Shark 牛市枢轴。

        X=100, A=200 (XA=100, bull: A>X)
        B = A - XA*1.35 = 200-135 = 65 → B<X(100)✓ (B/XA=1.35)
        AB = |B-A| = 135
        C = B + AB*1.90 = 65 + 256.5 = 321.5 (C/AB=1.90，在 1.618–2.24)
        D = A - XA*0.886 = 200-88.6 = 111.4 (D/XA=0.886)
        """
        X = 100.0
        A = 200.0
        XA = abs(A - X)
        B = A - XA * 1.35    # B/XA=1.35，B<X(100)✓
        AB = abs(B - A)
        C = B + AB * 1.90    # C/AB=1.90
        D = A - XA * 0.886   # D/XA=0.886（PRZ 核心）
        return [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]

    def _build_bear(self) -> list[tuple[float, str]]:
        """构造精确 Shark 熊市枢轴（bull 镜像）。

        X=200, A=100 (XA=100, bear: A<X)
        B = A + XA*1.35 = 100+135 = 235 → B>X(200)✓ (B/XA=1.35)
        AB = |B-A| = 135
        C = B - AB*1.90 (C/AB=1.90)
        D = A + XA*0.886
        """
        X = 200.0
        A = 100.0
        XA = abs(A - X)
        B = A + XA * 1.35    # B>X(200)✓
        AB = abs(B - A)
        C = B - AB * 1.90    # C/AB=1.90
        D = A + XA * 0.886
        return [(X, "H"), (A, "L"), (B, "H"), (C, "L"), (D, "H")]

    def test_shark_bull_detected(self) -> None:
        """精确 Shark 牛市比率 → 命中 Shark bull。"""
        pts = self._build_bull()
        pivots = _make_pivots(pts)
        results = detect_shark(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "Shark" in patterns, f"Shark 未命中，命中: {patterns}"
        idx = patterns.index("Shark")
        assert results[idx]["direction"] == "bull"

    def test_shark_bull_contract(self) -> None:
        """Shark bull 结果满足 dict 契约。"""
        pts = self._build_bull()
        pivots = _make_pivots(pts)
        results = detect_shark(pivots, tol=0.07)
        assert results, "Shark 未检出"
        _assert_contract(results[0], completed=True)

    def test_shark_bear_detected(self) -> None:
        """精确 Shark 熊市 → 命中 Shark bear。"""
        pts = self._build_bear()
        pivots = _make_pivots(pts)
        results = detect_shark(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "Shark" in patterns, f"Shark bear 未命中，命中: {patterns}"
        idx = patterns.index("Shark")
        assert results[idx]["direction"] == "bear"

    def test_shark_b_not_exceeding_x_not_detected(self) -> None:
        """B 未超越 X（B/XA=0.50，B>X）→ 不是 Shark，不应命中。"""
        X = 100.0
        A = 200.0
        XA = abs(A - X)
        B = A - XA * 0.50  # B=150 > X=100，未超越 X，不是 Shark
        AB = abs(B - A)
        C = B + AB * 1.90
        D = A - XA * 0.886
        pivots = [
            (0, X, "L"), (1, A, "H"), (2, B, "L"), (3, C, "H"), (4, D, "L")
        ]
        results = detect_shark(pivots, tol=0.05)
        patterns = [r["pattern"] for r in results]
        assert "Shark" not in patterns, f"B 未超越 X 不应命中 Shark，但命中了: {patterns}"

    def test_shark_wrong_b_ratio_not_detected(self) -> None:
        """B/XA=0.50（远低于 Shark 要求 1.13–1.618）→ 不检出。"""
        X = 100.0
        A = 200.0
        XA = abs(A - X)
        # B/XA=0.50 → B = A - 0.50*XA = 150 > X=100，同时比率偏离
        B = A - XA * 0.50  # B/XA=0.50，B>X
        AB = abs(B - A)
        C = B + AB * 1.90
        D = A - XA * 0.886
        pivots = [
            (0, X, "L"), (1, A, "H"), (2, B, "L"), (3, C, "H"), (4, D, "L")
        ]
        results = detect_shark(pivots, tol=0.05)
        patterns = [r["pattern"] for r in results]
        assert "Shark" not in patterns

    def test_shark_points_correct(self) -> None:
        """Shark 检出结果的 X/D 价格与构造值匹配。"""
        pts = self._build_bull()
        X_expected = pts[0][0]
        D_expected = pts[4][0]
        pivots = _make_pivots(pts)
        results = detect_shark(pivots, tol=0.07)
        assert results
        r = results[0]
        assert abs(r["points"]["X"][1] - X_expected) < 0.01
        assert abs(r["points"]["D"][1] - D_expected) < 0.01

    def test_shark_confidence_capped(self) -> None:
        """Shark 完整形态置信封顶 0.90。"""
        pts = self._build_bull()
        pivots = _make_pivots(pts)
        results = detect_shark(pivots, tol=0.07)
        for r in results:
            assert r["confidence"] <= 0.90

    def test_project_shark_prz_bull(self) -> None:
        """前瞻投射：已知 XABC → 产出 forming PRZ，置信≤0.85。"""
        pts = self._build_bull()
        X, A, B, C = pts[0][0], pts[1][0], pts[2][0], pts[3][0]
        results = project_shark_prz(X, A, B, C, direction="bull", tol=0.07)
        assert results, "Shark 前瞻未产出"
        r = results[0]
        _assert_contract(r, completed=False)
        assert r["pattern"] == "Shark"

    def test_project_shark_b_not_exceeding_x_no_output(self) -> None:
        """B 未超越 X → 前瞻不产出。"""
        X, A, B, C = 100.0, 200.0, 150.0, 350.0  # B=150>X=100
        results = project_shark_prz(X, A, B, C, direction="bull", tol=0.05)
        assert results == [], "B 未超越 X 不应产出 Shark 前瞻"


# ===========================================================================
# AB=CD 测试
# ===========================================================================

class TestABCD:
    """AB=CD 形态：4 点结构 A(H) B(L) C(H) D(L)（bull）。

    约束：BC/AB=0.382–0.886；CD/BC=1.272–1.618；|CD|≈|AB|。
    ⚠️ 与其他形态 D 点重叠率高，止损必执行。
    """

    def _build_bull(self) -> list[tuple[float, str]]:
        """构造精确 AB=CD 牛市枢轴。

        A=100(H), B=50(L): AB=50
        BC/AB=0.618 → BC=30.9 → C = B + BC = 80.9(H)
        CD/BC=1.618 → CD=49.98≈AB✓ → D = C - CD = 30.92(L)
        """
        A = 100.0
        B = 50.0
        AB = abs(B - A)
        BC = AB * 0.618     # BC/AB=0.618
        C = B + BC          # C(H)
        CD = BC * 1.618     # CD/BC=1.618；CD≈AB(=50) ✓
        D = C - CD          # D(L)
        return [(A, "H"), (B, "L"), (C, "H"), (D, "L")]

    def _build_bear(self) -> list[tuple[float, str]]:
        """构造精确 AB=CD 熊市枢轴（bull 镜像）。

        A=0(L), B=50(H): AB=50
        BC=30.9 → C=B-BC=19.1(L)
        CD=49.98 → D=C+CD=69.1(H)
        """
        A = 0.0
        B = 50.0
        AB = abs(B - A)
        BC = AB * 0.618
        C = B - BC          # C(L)
        CD = BC * 1.618
        D = C + CD          # D(H)
        return [(A, "L"), (B, "H"), (C, "L"), (D, "H")]

    def test_abcd_bull_detected(self) -> None:
        """精确 AB=CD 牛市比率 → 命中 ABCD bull。"""
        pts = self._build_bull()
        pivots = _make_pivots(pts)
        results = detect_abcd(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "ABCD" in patterns, f"ABCD 未命中，命中: {patterns}"
        idx = patterns.index("ABCD")
        assert results[idx]["direction"] == "bull"

    def test_abcd_bull_contract(self) -> None:
        """ABCD bull 结果满足 dict 契约。"""
        pts = self._build_bull()
        pivots = _make_pivots(pts)
        results = detect_abcd(pivots, tol=0.07)
        assert results, "ABCD 未检出"
        _assert_contract(results[0], completed=True)

    def test_abcd_bear_detected(self) -> None:
        """精确 AB=CD 熊市 → 命中 ABCD bear。"""
        pts = self._build_bear()
        pivots = _make_pivots(pts)
        results = detect_abcd(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "ABCD" in patterns, f"ABCD bear 未命中，命中: {patterns}"
        idx = patterns.index("ABCD")
        assert results[idx]["direction"] == "bear"

    def test_abcd_wrong_bc_ratio_not_detected(self) -> None:
        """BC/AB=0.10（远低于 0.382）→ 不检出（防过检测）。"""
        A = 100.0
        B = 50.0
        AB = abs(B - A)
        BC = AB * 0.10   # 故意偏离：BC/AB=0.10
        C = B + BC
        CD = BC * 1.618
        D = C - CD
        pivots = [
            (0, A, "H"), (1, B, "L"), (2, C, "H"), (3, D, "L")
        ]
        results = detect_abcd(pivots, tol=0.05)
        patterns = [r["pattern"] for r in results]
        assert "ABCD" not in patterns, f"不应命中 ABCD，但命中了: {patterns}"

    def test_abcd_wrong_cd_bc_ratio_not_detected(self) -> None:
        """CD/BC=3.0（远超 1.618 上限）→ 不检出。"""
        A = 100.0
        B = 50.0
        AB = abs(B - A)
        BC = AB * 0.618
        C = B + BC
        CD = BC * 3.0    # 故意偏离：CD/BC=3.0
        D = C - CD
        pivots = [
            (0, A, "H"), (1, B, "L"), (2, C, "H"), (3, D, "L")
        ]
        results = detect_abcd(pivots, tol=0.05)
        patterns = [r["pattern"] for r in results]
        assert "ABCD" not in patterns

    def test_abcd_points_correct(self) -> None:
        """ABCD 检出结果的 A/D 价格与构造值匹配。"""
        pts = self._build_bull()
        A_expected = pts[0][0]
        D_expected = pts[3][0]
        pivots = _make_pivots(pts)
        results = detect_abcd(pivots, tol=0.07)
        assert results
        r = results[0]
        assert abs(r["points"]["A"][1] - A_expected) < 0.01
        assert abs(r["points"]["D"][1] - D_expected) < 0.01

    def test_abcd_confidence_capped(self) -> None:
        """ABCD 完整形态置信封顶 0.90。"""
        pts = self._build_bull()
        pivots = _make_pivots(pts)
        results = detect_abcd(pivots, tol=0.07)
        for r in results:
            assert r["confidence"] <= 0.90

    def test_project_abcd_prz_bull(self) -> None:
        """前瞻投射：已知 ABC → 产出 forming PRZ，置信≤0.85。"""
        pts = self._build_bull()
        A, B, C = pts[0][0], pts[1][0], pts[2][0]
        results = project_abcd_prz(A, B, C, direction="bull", tol=0.07)
        assert results, "ABCD 前瞻未产出"
        r = results[0]
        _assert_contract(r, completed=False)
        assert r["pattern"] == "ABCD"
        assert r["direction"] == "bull"

    def test_project_abcd_d_location_reasonable(self) -> None:
        """AB=CD 前瞻 D 投射应接近等长位置（D ≈ C - AB）。"""
        A, B, C = 100.0, 50.0, 80.9   # AB=50, BC≈30.9
        AB = abs(B - A)
        d_expected = C - AB  # 等长投射：D ≈ C - AB = 30.9
        results = project_abcd_prz(A, B, C, direction="bull", tol=0.07)
        assert results
        prz_lo, prz_hi = results[0]["prz"]
        # PRZ 应包含预期 D（中心在 d_expected 附近）
        prz_mid = (prz_lo + prz_hi) / 2
        assert abs(prz_mid - d_expected) < abs(d_expected) * 0.10, (
            f"PRZ 中心 {prz_mid:.2f} 偏离预期 D {d_expected:.2f}"
        )

    def test_project_abcd_wrong_bc_no_output(self) -> None:
        """BC/AB=0.05（偏离 0.382–0.886 范围）→ 前瞻不产出。"""
        A, B, C = 100.0, 50.0, 52.5  # BC=2.5, AB=50, BC/AB=0.05
        results = project_abcd_prz(A, B, C, direction="bull", tol=0.05)
        assert results == [], "BC 偏离不应产出 ABCD 前瞻"


# ===========================================================================
# detect_all_ext 整合测试
# ===========================================================================

class TestDetectAllExt:
    """detect_all_ext：三形态统一接口测试。"""

    def test_detects_cypher_in_all_ext(self) -> None:
        """detect_all_ext 能检出 Cypher。"""
        X = 0.0; A = 100.0; XA = abs(A - X)
        B = A - XA * 0.50
        C = X + XA * 1.35
        XC = abs(C - X)
        D = C - XC * 0.786
        pts = [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]
        pivots = _make_pivots(pts)
        results = detect_all_ext(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "Cypher" in patterns

    def test_detects_shark_in_all_ext(self) -> None:
        """detect_all_ext 能检出 Shark。"""
        X = 100.0; A = 200.0; XA = abs(A - X)
        B = A - XA * 1.35
        AB = abs(B - A)
        C = B + AB * 1.90
        D = A - XA * 0.886
        pts = [(X, "L"), (A, "H"), (B, "L"), (C, "H"), (D, "L")]
        pivots = _make_pivots(pts)
        results = detect_all_ext(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "Shark" in patterns

    def test_detects_abcd_in_all_ext(self) -> None:
        """detect_all_ext 能检出 ABCD。"""
        A = 100.0; B = 50.0; AB = abs(B - A)
        BC = AB * 0.618; C = B + BC
        CD = BC * 1.618; D = C - CD
        pts = [(A, "H"), (B, "L"), (C, "H"), (D, "L")]
        pivots = _make_pivots(pts)
        results = detect_all_ext(pivots, tol=0.07)
        patterns = [r["pattern"] for r in results]
        assert "ABCD" in patterns

    def test_all_ext_contracts(self) -> None:
        """detect_all_ext 所有结果均满足 dict 契约。"""
        # 合并三形态测试数据
        all_pts = [
            [(0.0, "L"), (100.0, "H"), (50.0, "L"), (135.0, "H"), (135.0-135.0*0.786, "L")],  # Cypher
        ]
        for pts in all_pts:
            pivots = _make_pivots(pts)
            results = detect_all_ext(pivots, tol=0.10)
            for r in results:
                _assert_contract(r, completed=True)


# ===========================================================================
# P2 死代码验证：project_all_ext_prz 已删除，不应从 harmonic_ext 导入
# ===========================================================================

class TestProjectAllExtPrzDeleted:
    """P2：project_all_ext_prz 是死代码（无调用者），应已删除。"""

    def test_project_all_ext_prz_not_in_module(self) -> None:
        """project_all_ext_prz 应不存在于 harmonic_ext 模块中（死代码删除）。"""
        import smc_tracker.indicators.harmonic_ext as ext_mod
        assert not hasattr(ext_mod, "project_all_ext_prz"), (
            "project_all_ext_prz 是死代码，应已从 harmonic_ext 删除"
        )
