"""谐波扩展形态检测：Cypher、Shark、AB=CD。

本模块实现三种需要独立几何的谐波形态，复用 harmonic.py 既有 helper（_ratio、_within）。
不修改 harmonic.py 任何代码；merge agent 在 P2 阶段负责把结果并入 analyze_candles。

返回 dict 契约与 harmonic.detect_xabcd 完全一致：
  {
      pattern:    形态名 str,
      direction:  "bull" | "bear",
      points:     {X:(idx,px), A:(idx,px), B:(idx,px), C:(idx,px), D:(idx,px)},
      prz:        (lo, hi),
      completed:  bool,
      confidence: float,   # completed ≤ 0.90 / forming ≤ 0.85
      confluence: int,
  }

比率来源（交叉校验）：
  - pyharmonics (pyharmonics.readthedocs.io / github.com/niall-oc/pyharmonics)
  - djoffrey/HarmonicPatterns (github.com/djoffrey/HarmonicPatterns)
  - Scott Carney "Harmonic Trading" Vol.1/2
  - 5-0 Shark：Carney 《Harmonic Trading Vol.2》第 16 章；
    pyharmonics Shark 定义：
      B/XA : 1.13–1.618（B 超过 X，即扩展）
      C/AB : 1.618–2.24
      C/OX : 0.886–1.13 （Shark 的 OX anchor；本模块用 X 点作为 O，OX≡XA）
    D 为 AB 腿 0.50 回撤（5-0 入场），即 D = C - 0.50*BC（bull 向下）。

⚠️ 实测胜率警示：
  - Cypher：胜率暂无大样本实测数据，谨慎对待信号。
  - Shark：属于 5-0 家族极端扩展，实测样本稀少，胜率未知。
  - AB=CD：理论上最对称/最干净，但与其他形态 D 重叠概率高，止损必执行。

置信封顶：completed ≤ 0.90，forming ≤ 0.85（CLAUDE.md 诚实标注，不加分）。
"""
from __future__ import annotations

import math
from typing import Any

# 复用既有 helper，不重写几何
from .harmonic import _ratio, _within


# ---------------------------------------------------------------------------
# Cypher 比率表
# ---------------------------------------------------------------------------
# 来源：Carney "Harmonic Trading" Vol.2；pyharmonics Cypher 定义
# 结构（bull）：X(L) A(H) B(L) C(H, C>A) D(L)
# XC-anchored：D = 0.786 retrace of XC（从 X 到 C 测量）
# 注：C>A（C 超过 A 高点）是 Cypher 定义核心，detect_xabcd 的 B<C<A 校验会拒掉 Cypher，
#     因此 Cypher 必须独立实现。
_CYPHER_RATIOS = {
    "b_xa":  (0.382, 0.618),   # B/XA：B 回撤 0.382–0.618 of XA
    "c_xa":  (1.272, 1.414),   # C/XA：C 扩展 1.272–1.414 of XA（C 超越 A）
    "d_xc":  (0.786, 0.786),   # D/XC：D 回撤 0.786 of XC（核心 PRZ）
}

# ---------------------------------------------------------------------------
# Shark 比率表（5-0 / OXAB 标定；pyharmonics 公开定义）
# ---------------------------------------------------------------------------
# 结构（bull）：X(L) A(H) B(L,B<X 即 B 超过 X 向下) C(H) D(L)
# 用 XA 作为参考腿（等价 OX 腿）：
#   B/XA ：1.13–1.618（扩展，B 超越 X 向下）
#   C/AB ：1.618–2.24
#   D     ：0.886·XA retrace（从 A 向下）；D 亦可解释为 C 的 0.50 BC retrace
# 来源：Carney Harmonic Trading Vol.2 §Shark + pyharmonics/harmonic_patterns.py
_SHARK_RATIOS = {
    "b_xa":  (1.13, 1.618),    # B 超过 X（扩展），相对 XA
    "c_ab":  (1.618, 2.24),    # C 相对 AB
    "d_xa":  (0.826, 0.946),   # D/XA：0.886 ±tol（从 A 测量）
}

# ---------------------------------------------------------------------------
# AB=CD 比率表（4 点结构：A(H) B(L) C(H) D(L)）
# ---------------------------------------------------------------------------
# 来源：Carney + pyharmonics ABCD
# BC/AB：0.382–0.886（B→C 回撤 AB）
# CD/BC：1.272–1.618（C→D 扩展 BC）
# 且 CD ≈ AB（对称等长，±tol 校验）
_ABCD_RATIOS = {
    "bc_ab": (0.382, 0.886),
    "cd_bc": (1.272, 1.618),
}


# ===========================================================================
# Cypher 检测
# ===========================================================================

def detect_cypher(
    pivots: list[tuple[int, float, str]],
    tol: float = 0.05,
) -> list[dict]:
    """滑动 5 枢轴窗口检测 Cypher 形态（XA-anchored → XC-anchored）。

    Cypher bull 结构：X(L) A(H) B(L) C(H, C>A) D(L)
    Cypher bear 结构：X(H) A(L) B(H) C(L, C<A) D(H)

    与 detect_xabcd 的关键区别：
    - C 超越 A（bull: C>A；bear: C<A），detect_xabcd 的 B<C<A 结构校验拒掉此类。
    - D 从 X→C 测量（XC-anchored PRZ），不是从 X→A。

    Args:
        pivots: [(idx, price, 'H'|'L'), ...] 交替枚举，升序
        tol:    容差系数（默认 0.05 = 5%）

    Returns:
        命中形态列表，置信 completed≤0.90。
        ⚠️ Cypher 实测胜率未知，谨慎对待。
    """
    best_by_d: dict[int, dict] = {}
    n = len(pivots)

    for i in range(n - 4):
        window = pivots[i:i + 5]
        X_idx, X_px, X_type = window[0]
        A_idx, A_px, A_type = window[1]
        B_idx, B_px, B_type = window[2]
        C_idx, C_px, C_type = window[3]
        D_idx, D_px, D_type = window[4]

        # 方向：X→A 上涨 → bull
        if A_px > X_px:
            direction = "bull"
            expected_types = ("L", "H", "L", "H", "L")
        else:
            direction = "bear"
            expected_types = ("H", "L", "H", "L", "H")

        # 枢轴类型必须与方向一致
        if (X_type, A_type, B_type, C_type, D_type) != expected_types:
            continue

        # Cypher 核心约束：C 必须超越 A
        # bull: C>A（C 高于 A 高点）；bear: C<A（C 低于 A 低点）
        if direction == "bull":
            if C_px <= A_px:
                continue  # 不是 Cypher，C 未超越 A
        else:
            if C_px >= A_px:
                continue

        XA = abs(A_px - X_px)
        AB = abs(B_px - A_px)
        XC = abs(C_px - X_px)  # Cypher PRZ 锚：XC

        if XA < 1e-10 or XC < 1e-10:
            continue

        # 比率计算
        r_b_xa = _ratio(AB, XA)           # B/XA
        r_c_xa = _ratio(abs(C_px - X_px), XA)  # C/XA（C 扩展 XA）
        r_d_xc = _ratio(abs(D_px - C_px), XC)  # D/XC（PRZ 核心）

        hits = 0
        deviations: list[float] = []

        # B/XA：0.382–0.618
        b_lo, b_hi = _CYPHER_RATIOS["b_xa"]
        if _within(r_b_xa, b_lo, b_hi, tol):
            hits += 1
            mid = (b_lo + b_hi) / 2
            deviations.append(abs(r_b_xa - mid) / (mid + 1e-10))

        # C/XA：1.272–1.414
        c_lo, c_hi = _CYPHER_RATIOS["c_xa"]
        if _within(r_c_xa, c_lo, c_hi, tol):
            hits += 1
            mid = (c_lo + c_hi) / 2
            deviations.append(abs(r_c_xa - mid) / (mid + 1e-10))

        # D/XC：0.786（PRZ 核心约束）
        d_lo, d_hi = _CYPHER_RATIOS["d_xc"]
        d_ok = _within(r_d_xc, d_lo, d_hi, tol)
        if d_ok:
            hits += 1
            mid = (d_lo + d_hi) / 2
            deviations.append(abs(r_d_xc - mid) / (mid + 1e-10))

        # 需满足 ≥3 约束且含 D 约束（与 detect_xabcd 同标准）
        if hits < 3 or not d_ok:
            continue

        avg_dev = sum(deviations) / len(deviations)
        confidence = max(0.0, min(0.90, 1.0 - avg_dev))  # 封顶 0.90

        # PRZ：以 D 为中心窄带（D 已知）
        prz_lo = min(D_px * (1.0 - tol), D_px * (1.0 + tol))
        prz_hi = max(D_px * (1.0 - tol), D_px * (1.0 + tol))

        candidate = {
            "pattern":   "Cypher",
            "direction": direction,
            "points": {
                "X": (X_idx, X_px),
                "A": (A_idx, A_px),
                "B": (B_idx, B_px),
                "C": (C_idx, C_px),
                "D": (D_idx, D_px),
            },
            "prz":        (prz_lo, prz_hi),
            "completed":  True,
            "confidence": confidence,
            "confluence": hits,
        }

        existing = best_by_d.get(D_idx)
        if existing is None or confidence > existing["confidence"]:
            best_by_d[D_idx] = candidate

    return list(best_by_d.values())


def project_cypher_prz(
    X: float,
    A: float,
    B: float,
    C: float,
    direction: str,
    tol: float = 0.05,
) -> list[dict]:
    """前瞻投射：已知 XABC，反推 Cypher D 的 PRZ（XC 0.786 回撤）。

    仅 Cypher 的 B/XA 和 C/XA 均满足时才投射（避免噪音）。

    Args:
        X, A, B, C: 枢轴价格
        direction: "bull" | "bear"
        tol: 容差

    Returns:
        [{"pattern","direction","prz","completed":False,"confidence","confluence"}]
        ⚠️ Cypher 前瞻实测胜率未知。
    """
    XA = abs(A - X)
    AB = abs(B - A)
    XC = abs(C - X)

    if XA < 1e-10 or XC < 1e-10:
        return []

    # Cypher 核心：C 必须超越 A
    if direction == "bull" and C <= A:
        return []
    if direction == "bear" and C >= A:
        return []

    r_b_xa = _ratio(AB, XA)
    r_c_xa = _ratio(XC, XA)

    hits = 0
    deviations: list[float] = []

    b_lo, b_hi = _CYPHER_RATIOS["b_xa"]
    if _within(r_b_xa, b_lo, b_hi, tol):
        hits += 1
        mid = (b_lo + b_hi) / 2
        deviations.append(abs(r_b_xa - mid) / (mid + 1e-10))

    c_lo, c_hi = _CYPHER_RATIOS["c_xa"]
    if _within(r_c_xa, c_lo, c_hi, tol):
        hits += 1
        mid = (c_lo + c_hi) / 2
        deviations.append(abs(r_c_xa - mid) / (mid + 1e-10))

    if hits < 2:
        return []

    # D 投射：0.786 * XC retrace
    d_ratio = 0.786
    if direction == "bull":
        d_est = C - XC * d_ratio  # bull：从 C 向下 0.786*XC
    else:
        d_est = C + XC * d_ratio  # bear：从 C 向上 0.786*XC

    price_ref = max(abs(X), abs(C), XC, 1.0)
    half_span = price_ref * tol * 0.5
    prz_lo = min(d_est - half_span, d_est + half_span)
    prz_hi = max(d_est - half_span, d_est + half_span)

    avg_dev = sum(deviations) / max(len(deviations), 1)
    confidence = max(0.0, min(0.85, 1.0 - avg_dev))  # forming 封顶 0.85

    return [{
        "pattern":   "Cypher",
        "direction": direction,
        "prz":       (prz_lo, prz_hi),
        "completed": False,
        "confidence": confidence,
        "confluence": hits,
    }]


# ===========================================================================
# Shark 检测
# ===========================================================================

def detect_shark(
    pivots: list[tuple[int, float, str]],
    tol: float = 0.05,
) -> list[dict]:
    """滑动 5 枢轴窗口检测 Shark / 5-0 形态。

    Shark bull 结构：X(L) A(H) B(L, B < X 即超越 X 向下) C(H) D(L)
    Shark bear 结构：X(H) A(L) B(H, B > X 即超越 X 向上) C(L) D(H)

    关键几何：B 扩展超越 X（bull: B<X; bear: B>X），相对 XA ≈ 1.13–1.618。
    D = 0.886 ·XA retrace（从 A 向下/上），这是 Shark PRZ 核心。

    来源：Carney Harmonic Trading Vol.2 §Shark；pyharmonics Shark 定义。
    ⚠️ Shark 属于极端扩展家族，实测样本稀少，胜率未知，谨慎对待。

    Args:
        pivots: [(idx, price, 'H'|'L'), ...] 交替枚举，升序
        tol:    容差系数

    Returns:
        命中形态列表，置信 completed≤0.90。
    """
    best_by_d: dict[int, dict] = {}
    n = len(pivots)

    for i in range(n - 4):
        window = pivots[i:i + 5]
        X_idx, X_px, X_type = window[0]
        A_idx, A_px, A_type = window[1]
        B_idx, B_px, B_type = window[2]
        C_idx, C_px, C_type = window[3]
        D_idx, D_px, D_type = window[4]

        # 方向：X→A 上涨 → bull
        if A_px > X_px:
            direction = "bull"
            expected_types = ("L", "H", "L", "H", "L")
        else:
            direction = "bear"
            expected_types = ("H", "L", "H", "L", "H")

        if (X_type, A_type, B_type, C_type, D_type) != expected_types:
            continue

        # Shark 核心约束：B 超越 X
        # bull: B < X（B 比 X 还低）；bear: B > X（B 比 X 还高）
        if direction == "bull":
            if B_px >= X_px:
                continue  # B 未超越 X，不是 Shark
        else:
            if B_px <= X_px:
                continue

        XA = abs(A_px - X_px)
        AB = abs(B_px - A_px)
        BC = abs(C_px - B_px)

        if XA < 1e-10 or AB < 1e-10:
            continue

        # 比率计算
        r_b_xa = _ratio(AB, XA)    # B/XA（Shark: 1.13–1.618 扩展）
        r_c_ab = _ratio(BC, AB)    # C/AB（Shark: 1.618–2.24）
        r_d_xa = _ratio(abs(D_px - A_px), XA)  # D/XA（从 A 测量；0.886 retrace）

        hits = 0
        deviations: list[float] = []

        # B/XA：1.13–1.618
        b_lo, b_hi = _SHARK_RATIOS["b_xa"]
        if _within(r_b_xa, b_lo, b_hi, tol):
            hits += 1
            mid = (b_lo + b_hi) / 2
            deviations.append(abs(r_b_xa - mid) / (mid + 1e-10))

        # C/AB：1.618–2.24
        c_lo, c_hi = _SHARK_RATIOS["c_ab"]
        if _within(r_c_ab, c_lo, c_hi, tol):
            hits += 1
            mid = (c_lo + c_hi) / 2
            deviations.append(abs(r_c_ab - mid) / (mid + 1e-10))

        # D/XA：0.826–0.946（0.886 PRZ 核心）
        d_lo, d_hi = _SHARK_RATIOS["d_xa"]
        d_ok = _within(r_d_xa, d_lo, d_hi, tol)
        if d_ok:
            hits += 1
            mid = (d_lo + d_hi) / 2
            deviations.append(abs(r_d_xa - mid) / (mid + 1e-10))

        if hits < 3 or not d_ok:
            continue

        avg_dev = sum(deviations) / len(deviations)
        confidence = max(0.0, min(0.90, 1.0 - avg_dev))

        prz_lo = min(D_px * (1.0 - tol), D_px * (1.0 + tol))
        prz_hi = max(D_px * (1.0 - tol), D_px * (1.0 + tol))

        candidate = {
            "pattern":   "Shark",
            "direction": direction,
            "points": {
                "X": (X_idx, X_px),
                "A": (A_idx, A_px),
                "B": (B_idx, B_px),
                "C": (C_idx, C_px),
                "D": (D_idx, D_px),
            },
            "prz":        (prz_lo, prz_hi),
            "completed":  True,
            "confidence": confidence,
            "confluence": hits,
        }

        existing = best_by_d.get(D_idx)
        if existing is None or confidence > existing["confidence"]:
            best_by_d[D_idx] = candidate

    return list(best_by_d.values())


def project_shark_prz(
    X: float,
    A: float,
    B: float,
    C: float,
    direction: str,
    tol: float = 0.05,
) -> list[dict]:
    """前瞻投射：已知 XABC，反推 Shark D 的 PRZ（0.886 XA retrace from A）。

    Args:
        X, A, B, C: 枢轴价格
        direction: "bull" | "bear"
        tol: 容差

    Returns:
        [{"pattern","direction","prz","completed":False,"confidence","confluence"}]
        ⚠️ Shark 前瞻实测胜率未知。
    """
    XA = abs(A - X)
    AB = abs(B - A)
    BC = abs(C - B)

    if XA < 1e-10 or AB < 1e-10:
        return []

    # Shark：B 必须超越 X
    if direction == "bull" and B >= X:
        return []
    if direction == "bear" and B <= X:
        return []

    r_b_xa = _ratio(AB, XA)
    r_c_ab = _ratio(BC, AB)

    hits = 0
    deviations: list[float] = []

    b_lo, b_hi = _SHARK_RATIOS["b_xa"]
    if _within(r_b_xa, b_lo, b_hi, tol):
        hits += 1
        mid = (b_lo + b_hi) / 2
        deviations.append(abs(r_b_xa - mid) / (mid + 1e-10))

    c_lo, c_hi = _SHARK_RATIOS["c_ab"]
    if _within(r_c_ab, c_lo, c_hi, tol):
        hits += 1
        mid = (c_lo + c_hi) / 2
        deviations.append(abs(r_c_ab - mid) / (mid + 1e-10))

    if hits < 2:
        return []

    # D 投射：0.886 * XA from A
    d_ratio = 0.886
    if direction == "bull":
        d_est = A - XA * d_ratio
    else:
        d_est = A + XA * d_ratio

    price_ref = max(abs(A), XA, 1.0)
    half_span = price_ref * tol * 0.5
    prz_lo = min(d_est - half_span, d_est + half_span)
    prz_hi = max(d_est - half_span, d_est + half_span)

    avg_dev = sum(deviations) / max(len(deviations), 1)
    confidence = max(0.0, min(0.85, 1.0 - avg_dev))

    return [{
        "pattern":   "Shark",
        "direction": direction,
        "prz":       (prz_lo, prz_hi),
        "completed": False,
        "confidence": confidence,
        "confluence": hits,
    }]


# ===========================================================================
# AB=CD 检测（4 点结构）
# ===========================================================================

def detect_abcd(
    pivots: list[tuple[int, float, str]],
    tol: float = 0.05,
) -> list[dict]:
    """滑动 4 枢轴窗口检测 AB=CD 形态（4 点对称结构）。

    AB=CD bull 结构：A(H) B(L) C(H) D(L)
    AB=CD bear 结构：A(L) B(H) C(L) D(H)

    约束：
    1. BC/AB = 0.382–0.886（B→C 回撤 AB）
    2. CD/BC = 1.272–1.618（C→D 扩展 BC）
    3. |CD| ≈ |AB|（CD 等长 AB，对称性，±tol）

    来源：Carney Harmonic Trading Vol.1；pyharmonics ABCD。
    ⚠️ AB=CD 与其他形态 D 点重叠概率高，需结合其他信号确认，止损必执行。

    注意：返回 dict 中 "points" 字段键为 A/B/C/D（无 X），但 Merge agent 合并时
    需处理 4 点 vs 5 点 dict 差异。为保持 dict 契约完全一致（含 X 键），
    本函数以 A 填充 X 位（X=A），Merge agent 读 pattern="ABCD" 时按 4 点处理。

    Args:
        pivots: [(idx, price, 'H'|'L'), ...] 交替枚举，升序（可含 ≥4 个枢轴）
        tol:    容差系数

    Returns:
        命中形态列表，置信 completed≤0.90。
    """
    best_by_d: dict[int, dict] = {}
    n = len(pivots)

    for i in range(n - 3):
        window = pivots[i:i + 4]
        A_idx, A_px, A_type = window[0]
        B_idx, B_px, B_type = window[1]
        C_idx, C_px, C_type = window[2]
        D_idx, D_px, D_type = window[3]

        # 方向：A→B 下降 → bull（A 高 B 低）
        if A_px > B_px:
            direction = "bull"
            expected_types = ("H", "L", "H", "L")
        else:
            direction = "bear"
            expected_types = ("L", "H", "L", "H")

        if (A_type, B_type, C_type, D_type) != expected_types:
            continue

        AB = abs(B_px - A_px)
        BC = abs(C_px - B_px)
        CD = abs(D_px - C_px)

        if AB < 1e-10 or BC < 1e-10:
            continue

        r_bc_ab = _ratio(BC, AB)   # BC/AB 回撤
        r_cd_bc = _ratio(CD, BC)   # CD/BC 扩展

        hits = 0
        deviations: list[float] = []

        # BC/AB：0.382–0.886
        bc_lo, bc_hi = _ABCD_RATIOS["bc_ab"]
        if _within(r_bc_ab, bc_lo, bc_hi, tol):
            hits += 1
            mid = (bc_lo + bc_hi) / 2
            deviations.append(abs(r_bc_ab - mid) / (mid + 1e-10))

        # CD/BC：1.272–1.618
        cd_lo, cd_hi = _ABCD_RATIOS["cd_bc"]
        cd_ok = _within(r_cd_bc, cd_lo, cd_hi, tol)
        if cd_ok:
            hits += 1
            mid = (cd_lo + cd_hi) / 2
            deviations.append(abs(r_cd_bc - mid) / (mid + 1e-10))

        # 对称性：|CD| ≈ |AB|（±tol）
        ab_cd_ratio = _ratio(CD, AB)
        symmetry_ok = _within(ab_cd_ratio, 1.0, 1.0, tol)
        if symmetry_ok:
            hits += 1
            deviations.append(abs(ab_cd_ratio - 1.0) / 1.0)

        # 必须满足 ≥2 约束且含 CD/BC 约束
        if hits < 2 or not cd_ok:
            continue

        avg_dev = sum(deviations) / len(deviations)
        confidence = max(0.0, min(0.90, 1.0 - avg_dev))

        prz_lo = min(D_px * (1.0 - tol), D_px * (1.0 + tol))
        prz_hi = max(D_px * (1.0 - tol), D_px * (1.0 + tol))

        # dict 契约与 detect_xabcd 一致：X 键填充为 A（无独立 X 点）
        candidate = {
            "pattern":   "ABCD",
            "direction": direction,
            "points": {
                "X": (A_idx, A_px),  # ABCD 无 X 点，以 A 填充，消费方按 pattern="ABCD" 区分
                "A": (A_idx, A_px),
                "B": (B_idx, B_px),
                "C": (C_idx, C_px),
                "D": (D_idx, D_px),
            },
            "prz":        (prz_lo, prz_hi),
            "completed":  True,
            "confidence": confidence,
            "confluence": hits,
        }

        existing = best_by_d.get(D_idx)
        if existing is None or confidence > existing["confidence"]:
            best_by_d[D_idx] = candidate

    return list(best_by_d.values())


def project_abcd_prz(
    A: float,
    B: float,
    C: float,
    direction: str,
    tol: float = 0.05,
) -> list[dict]:
    """前瞻投射：已知 ABC，反推 AB=CD 的 D 投射区（D = C - |AB|，等长投射）。

    Args:
        A, B, C: 枢轴价格（bull: A 高→B 低→C 高）
        direction: "bull" | "bear"
        tol: 容差

    Returns:
        [{"pattern","direction","prz","completed":False,"confidence","confluence"}]
        ⚠️ AB=CD 前瞻基于等长对称投射，止损必执行。
    """
    AB = abs(B - A)
    BC = abs(C - B)

    if AB < 1e-10:
        return []

    r_bc_ab = _ratio(BC, AB)

    # BC/AB 需在合理范围才投射
    bc_lo, bc_hi = _ABCD_RATIOS["bc_ab"]
    if not _within(r_bc_ab, bc_lo, bc_hi, tol):
        return []

    # D 等长投射：D = C ± AB
    if direction == "bull":
        d_est = C - AB  # bull：D 在 C 下方，等长 AB
    else:
        d_est = C + AB  # bear：D 在 C 上方

    price_ref = max(abs(A), AB, 1.0)
    half_span = price_ref * tol * 0.5
    prz_lo = min(d_est - half_span, d_est + half_span)
    prz_hi = max(d_est - half_span, d_est + half_span)

    mid = (bc_lo + bc_hi) / 2
    dev = abs(r_bc_ab - mid) / (mid + 1e-10)
    confidence = max(0.0, min(0.85, 1.0 - dev))

    return [{
        "pattern":   "ABCD",
        "direction": direction,
        "prz":       (prz_lo, prz_hi),
        "completed": False,
        "confidence": confidence,
        "confluence": 1,
    }]


# ===========================================================================
# 便捷合并接口（供 Merge agent 在 analyze_candles 中调用）
# ===========================================================================

def detect_all_ext(
    pivots: list[tuple[int, float, str]],
    tol: float = 0.05,
) -> list[dict]:
    """对 pivots 运行全部扩展形态检测（Cypher + Shark + ABCD），返回合并列表。

    Args:
        pivots: [(idx, price, 'H'|'L'), ...] 升序
        tol:    容差系数

    Returns:
        全部命中结果（completed=True），每条同 detect_xabcd 返回字段一致。
    """
    results: list[dict] = []
    results.extend(detect_cypher(pivots, tol=tol))
    results.extend(detect_shark(pivots, tol=tol))
    results.extend(detect_abcd(pivots, tol=tol))
    return results


def project_all_ext_prz(
    pivots: list[tuple[int, float, str]],
    tol: float = 0.05,
) -> list[dict]:
    """对最后 4 枢轴（XABC）运行全部扩展前瞻投射。

    Args:
        pivots: ≥4 个枢轴
        tol:    容差系数

    Returns:
        前瞻结果列表（completed=False），按 confidence 降序。
    """
    if len(pivots) < 4:
        return []

    last4 = pivots[-4:]
    X_px = last4[0][1]
    A_px = last4[1][1]
    B_px = last4[2][1]
    C_px = last4[3][1]

    direction = "bull" if A_px > X_px else "bear"

    results: list[dict] = []
    results.extend(project_cypher_prz(X_px, A_px, B_px, C_px, direction, tol=tol))
    results.extend(project_shark_prz(X_px, A_px, B_px, C_px, direction, tol=tol))

    # ABCD 前瞻用 A(=last4[0]) / B / C；方向以 A→B 判断
    A2_px = last4[0][1]
    B2_px = last4[1][1]
    C2_px = last4[2][1]
    dir2 = "bull" if A2_px > B2_px else "bear"
    results.extend(project_abcd_prz(A2_px, B2_px, C2_px, dir2, tol=tol))

    results.sort(key=lambda r: r["confidence"], reverse=True)
    return results
