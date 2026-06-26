"""谐波形态(Harmonic Patterns)计算核心。

纯计算，无 I/O，可测。覆盖 4 种 XA-anchored 经典形态（Scott Carney 标准）：
Gartley, Bat, Butterfly, Crab。

注意：
- 真 AB=CD 为 4 点结构，列入 backlog 单独实现，不混入 5 点检测。
- ABCD 已从本模块移除，避免过检测噪音。
- Cypher（D=0.786·XC，C 超越 A；XC-anchored）与 Shark（O-X-A-B-C 的 5-0 标定）
  均需独立几何实现，无法用本模块的 XA-anchored schema 正确验证，已列入 backlog。

比率表来源（与开源交叉校验对齐）：
  - djoffrey/HarmonicPatterns (github.com/djoffrey/HarmonicPatterns)
  - pyharmonics (pyharmonics.readthedocs.io)
  - Scott Carney "Harmonic Trading" Vol.1/2

每形态字段含义：
  b_xa  : B 相对 XA 的回撤比率区间 (lo, hi)
  bc_ab : BC 相对 AB 的回撤/扩展比率区间
  cd_bc : CD 相对 BC 的回撤/扩展比率区间
  d_xa  : D 相对 XA 的最终投射比率区间（PRZ 核心）

枢轴检测说明：
  find_pivots 使用 patterns.swing_highs/swing_lows（分形严格比较），
  无 scipy 依赖，test=production 路径一致。
  注意摆动点需右侧 order 根确认，故识别相对实时滞后 order 根（卡片副标题诚实披露）。
"""
from __future__ import annotations

import math
import logging
from typing import Any

import numpy as np

from .patterns import swing_highs, swing_lows
from ..smc.structure import MarketStructure

log = logging.getLogger("harmonic")

# 完整形态可操作性：D（反转区）须在现价 ±此比例内才算「入场触发」。
# 否则是早已演完的远古形态（如 1W 长历史里 D 距现价数倍），标「入场触发」误导。
_COMPLETED_MAX_DIST = 0.15

# ---- 形态比率表（与 djoffrey/HarmonicPatterns + pyharmonics 交叉校验）----
# 每个形态: {b_xa, bc_ab, cd_bc, d_xa} 各为 (lo, hi) 区间
# 参考：github.com/djoffrey/HarmonicPatterns/blob/main/harmonic.py
# 注：ABCD（4 点结构）已移除，列入 backlog 单独实现，不混入 5 点 XABCD 检测。
# 注：Cypher(D=0.786·XC,C超A) 与 Shark(O-X-A-B-C 5-0 标定) 需独立几何，列入 backlog。
HARMONIC_RATIOS: dict[str, dict] = {
    # Gartley：B=0.618 XA，D=0.786 XA（最经典谐波，Carney 1932年 Gartley原型）
    "Gartley": {
        "b_xa":  (0.566, 0.686),   # B 回撤 0.618 XA，±tol
        "bc_ab": (0.382, 0.886),
        "cd_bc": (1.20, 1.618),    # CD/BC 下限 1.13→1.20（更贴合 Carney 标准 Gartley）
        "d_xa":  (0.726, 0.846),   # D 在 0.786 XA，±tol
    },
    # Bat：B=0.382~0.50 XA，D=0.886 XA（比 Gartley 回撤更浅）
    "Bat": {
        "b_xa":  (0.30, 0.55),     # B 0.382~0.50 XA
        "bc_ab": (0.382, 0.886),
        "cd_bc": (1.618, 2.618),
        "d_xa":  (0.826, 0.946),   # D 在 0.886 XA，±tol
    },
    # Butterfly：B=0.786 XA，D=1.272~1.618 XA（扩展超过 X，Carney 标准）
    "Butterfly": {
        "b_xa":  (0.736, 0.836),   # B 在 0.786 XA，±tol
        "bc_ab": (0.382, 0.886),
        "cd_bc": (1.618, 2.618),
        "d_xa":  (1.272, 1.618),   # D 超过 X 扩展（Carney 标准，收紧自旧 1.17~1.72）
    },
    # Crab：B=0.382~0.618 XA，D=1.618 XA（最极端扩展）
    # 注：实测胜率偏低，render 时附 ⚠Crab实测胜率偏低警示（CLAUDE.md 诚实标注）
    "Crab": {
        "b_xa":  (0.326, 0.636),
        "bc_ab": (0.382, 0.886),
        "cd_bc": (2.618, 3.618),   # CD/BC 下限 2.0→2.618 对齐 Carney 标准 Crab(2.618/3.14/3.618)，#146；
                                   # 软项(检测为 3-of-4，D 必过)，收紧只提精度，与其它形态贴标一致
        "d_xa":  (1.518, 1.718),   # D 在 1.618 XA，±tol
    },
}


def _ratio(leg_a: float, leg_b: float) -> float:
    """计算两腿比率 |leg_a| / |leg_b|。分母为 0 返回 inf（守卫：不崩溃，不返回 NaN）。"""
    if leg_b == 0.0:
        return math.inf
    return abs(leg_a) / abs(leg_b)


def _within(x: float, lo: float, hi: float, tol: float = 0.05) -> bool:
    """x 是否在 [lo*(1-tol), hi*(1+tol)] 范围内。"""
    return lo * (1.0 - tol) <= x <= hi * (1.0 + tol)


def find_pivots(
    candles: list[Any],
    order: int = 3,
) -> list[tuple[int, float, str]]:
    """用 patterns.swing_highs/swing_lows 找摆动高/低点，交替排列（H/L 相间）。

    使用分形严格比较（>/<），无 scipy 依赖，test=production 路径一致。
    注意：摆动点需右侧 order 根确认，故识别相对实时滞后 order 根。

    Args:
        candles: K 线列表，需有 .h/.l/.c 属性
        order:   分形邻域大小（传给 swing_highs/lows 的 lookback）

    Returns:
        [(idx, price, 'H'|'L'), ...] 升序，长度 < 5 返回 []。
    """
    if len(candles) < 2 * order + 3:
        return []

    highs = [(i, p, "H") for i, p in swing_highs(candles, order)]
    lows = [(i, p, "L") for i, p in swing_lows(candles, order)]
    combined = sorted(highs + lows, key=lambda x: x[0])
    pivots = _clean_alternating(combined)

    if len(pivots) < 5:
        return []
    return pivots


def _clean_alternating(
    pivots: list[tuple[int, float, str]],
) -> list[tuple[int, float, str]]:
    """把枢轴序列清洗为严格交替 H/L。

    相邻同类型：取更极端者（H 取更高，L 取更低）。

    @deprecated 此函数存在 repaint 问题（新增 K 线若产生更极端同类型枢轴，
    会回改历史段已选枢轴）。运行时已弃用，改用 _alternate_immutable。
    保留仅供历史对照测试。
    """
    if not pivots:
        return []
    out: list[tuple[int, float, str]] = [pivots[0]]
    for cur in pivots[1:]:
        prev = out[-1]
        if cur[2] == prev[2]:
            # 同类型：保留更极端
            if cur[2] == "H" and cur[1] > prev[1]:
                out[-1] = cur
            elif cur[2] == "L" and cur[1] < prev[1]:
                out[-1] = cur
            # 否则丢弃 cur
        else:
            out.append(cur)
    return out


def _alternate_immutable(
    swings: list[tuple[int, float, str]],
) -> list[tuple[int, float, str]]:
    """把已确认 swing 序列规整为严格交替 H/L，但保持因果不可变。

    遇相邻同类型，保留先确认者（index 更小者）、丢弃后者——绝不回改前缀。

    与 _clean_alternating 的本质差异：
    - _clean_alternating: 贪心取更极端（改历史→repaint）。
    - _alternate_immutable: first-wins（只追加不回改），保证
      「给定前缀的输出不随后续 swing 变化」。

    关键不变量: 对任意 k, _alternate_immutable(swings[:k]) 是
    _alternate_immutable(swings[:k+1]) 的前缀。

    取舍: first-wins 牺牲「同段更极端枢轴」的几何最优性，换来确定性/无 repaint。
    这是 CLAUDE.md「诚实/可验证」优先于「事后最优」的体现。
    """
    if not swings:
        return []
    out: list[tuple[int, float, str]] = [swings[0]]
    for cur in swings[1:]:
        prev = out[-1]
        if cur[2] == prev[2]:
            # 同类型：first-wins，丢弃后者（不回改已选枢轴）
            pass
        else:
            out.append(cur)
    return out


class _CandleAdapter:
    """鸭类型适配器：为缺少 close_time_ms 的轻量合成对象补零时间戳。

    MarketStructure.update() 需要 .h/.l/.c/.close_time_ms。
    真实 Candle 已有所有字段；仅合成测试对象（_C）缺 close_time_ms。
    """
    __slots__ = ("h", "l", "c", "close_time_ms")

    def __init__(self, candle: Any, idx: int) -> None:
        self.h = float(candle.h)
        self.l = float(candle.l)
        self.c = float(candle.c)
        self.close_time_ms: int = getattr(candle, "close_time_ms", idx * 60_000)


def pivots_from_structure(
    candles: list[Any],
    order: int = 3,
) -> list[tuple[int, float, str]]:
    """用 smc.MarketStructure 的不可变 swing 流构造交替枢轴序列（根治 repaint）。

    与 find_pivots 同返回契约: [(idx, price, 'H'|'L'), ...] 升序，< 5 返回 []。

    差异: swing 由 append-only 引擎确认，同一历史段不随新 K 线改变。
    交替性由 _alternate_immutable 强制，但不回改已选枢轴（冻结语义）。

    审查强制护栏:
    - MarketStructure 用与谐波同一 order 实例化（lookback=order），枢轴定义一致。
    - _alternate_immutable 用 first-wins，保证 prefix 不变量。

    Args:
        candles: K 线列表，需有 .h/.l/.c 属性（.close_time_ms 可选，缺省补 0）。
        order:   分形邻域大小（与 MarketStructure lookback 对齐）。

    Returns:
        [(idx, price, 'H'|'L'), ...] 升序，长度 < 5 返回 []。
    """
    if not candles:
        return []

    # 审查护栏①: MarketStructure 必须用与谐波同一 order 实例化
    ms = MarketStructure(lookback=order)

    for i, c in enumerate(candles):
        ms.update(_CandleAdapter(c, i))

    # ms.swings: append-only，kind ∈ {"high","low"}，index 升序
    # 映射为 (index, price, 'H'|'L')
    raw: list[tuple[int, float, str]] = [
        (sw.index, float(sw.price), "H" if sw.kind == "high" else "L")
        for sw in ms.swings
    ]

    # 交替化：first-wins（不回改已选枢轴）
    pivots = _alternate_immutable(raw)

    if len(pivots) < 5:
        return []
    return pivots


def detect_xabcd(
    pivots: list[tuple[int, float, str]],
    tol: float = 0.05,
) -> list[dict]:
    """滑动每 5 个交替枢轴(X,A,B,C,D)，对每个形态校验比率约束。

    修复：
    - Bull 要求窗口类型序列严格为 [L,H,L,H,L]，bear 要求 [H,L,H,L,H]（Bug-3）。
    - D 已知时 PRZ 以 D 为中心窄带（±tol），保证 PRZ 必含 D 点。
    - 同一 D 枢轴 idx 多形态命中时只保留最高 confidence（去重）。
    - confidence 封顶 0.90（诚实，完整形态也不过 90%）。

    Args:
        pivots: find_pivots 返回的 [(idx, price, 'H'|'L'), ...]
        tol:    容差系数（默认 0.05 = 5%）

    Returns:
        命中形态列表，每条:
        {
            "pattern":    形态名,
            "direction":  "bull" | "bear",
            "points":     {X:(idx,px), A:..., B:..., C:..., D:...},
            "prz":        (lo, hi),
            "completed":  True,
            "confidence": float,   # 各比率与中心值吻合度（封顶 0.90）
            "confluence": int,     # 满足约束的腿数
        }
    """
    # 按 D_idx → 最高 confidence 候选（去重用）
    best_by_d: dict[int, dict] = {}

    n = len(pivots)
    for i in range(n - 4):
        window = pivots[i:i + 5]
        X_idx, X_px, X_type = window[0]
        A_idx, A_px, A_type = window[1]
        B_idx, B_px, B_type = window[2]
        C_idx, C_px, C_type = window[3]
        D_idx, D_px, D_type = window[4]

        # 方向：X→A 上涨 → bull（D 在下方为买点）
        if A_px > X_px:
            direction = "bull"
        else:
            direction = "bear"

        # Bug-3：枢轴类型必须与方向一致
        # bull: [L, H, L, H, L] ; bear: [H, L, H, L, H]
        if direction == "bull":
            expected_types = ("L", "H", "L", "H", "L")
        else:
            expected_types = ("H", "L", "H", "L", "H")
        actual_types = (X_type, A_type, B_type, C_type, D_type)
        if actual_types != expected_types:
            continue  # 几何无效，跳过

        # 结构次序校验：4 个保留形态(Gartley/Bat/Butterfly/Crab) 的 B/C 必须满足
        # bull 须 X<B<C<A（B=X 上方回撤低，C=A 下方次高），bear 镜像。
        # 仅查幅度比率会让 C 超过 A 的结构(如真 Cypher)凑巧满足比率而被误标为 Gartley，此校验根治。
        if direction == "bull":
            if not (X_px < B_px < C_px < A_px):
                continue
        else:
            if not (X_px > B_px > C_px > A_px):
                continue

        XA = abs(A_px - X_px)
        AB = abs(B_px - A_px)
        BC = abs(C_px - B_px)
        CD = abs(D_px - C_px)

        if XA < 1e-10:
            continue  # 零长腿，跳过

        # 计算各腿比率
        r_b_xa  = _ratio(AB, XA)   # B 相对 XA 回撤
        r_bc_ab = _ratio(BC, AB)   # BC 相对 AB
        r_cd_bc = _ratio(CD, BC)   # CD 相对 BC
        r_d_xa  = _ratio(abs(D_px - A_px), XA)  # D 相对 XA（从 A 测量）

        for pat_name, pat in HARMONIC_RATIOS.items():
            hits = 0
            deviations: list[float] = []

            # 检验 B 相对 XA
            b_lo, b_hi = pat["b_xa"]
            if _within(r_b_xa, b_lo, b_hi, tol):
                hits += 1
                mid = (b_lo + b_hi) / 2
                deviations.append(abs(r_b_xa - mid) / (mid + 1e-10))

            # 检验 BC 相对 AB
            bc_lo, bc_hi = pat["bc_ab"]
            if _within(r_bc_ab, bc_lo, bc_hi, tol):
                hits += 1
                mid = (bc_lo + bc_hi) / 2
                deviations.append(abs(r_bc_ab - mid) / (mid + 1e-10))

            # 检验 CD 相对 BC
            cd_lo, cd_hi = pat["cd_bc"]
            if _within(r_cd_bc, cd_lo, cd_hi, tol):
                hits += 1
                mid = (cd_lo + cd_hi) / 2
                deviations.append(abs(r_cd_bc - mid) / (mid + 1e-10))

            # 检验 D 相对 XA（PRZ 核心）
            d_lo, d_hi = pat["d_xa"]
            if _within(r_d_xa, d_lo, d_hi, tol):
                hits += 1
                mid = (d_lo + d_hi) / 2
                deviations.append(abs(r_d_xa - mid) / (mid + 1e-10))

            # 必须至少满足 3 条约束（含 D 约束）
            d_ok = _within(r_d_xa, d_lo, d_hi, tol)
            if hits < 3 or not d_ok:
                continue

            # 置信度 = 1 - 平均相对偏差，封顶 0.90（诚实）
            avg_dev = sum(deviations) / len(deviations)
            confidence = max(0.0, min(0.90, 1.0 - avg_dev))

            # PRZ：以真实 D 为中心窄带（D 已知，不重投射）
            prz_lo = D_px * (1.0 - tol)
            prz_hi = D_px * (1.0 + tol)
            # 保证正确顺序（D 可能为负价格的理论测试场景）
            prz_lo, prz_hi = min(prz_lo, prz_hi), max(prz_lo, prz_hi)

            candidate = {
                "pattern":   pat_name,
                "direction": direction,
                "points": {
                    "X": (X_idx, X_px),
                    "A": (A_idx, A_px),
                    "B": (B_idx, B_px),
                    "C": (C_idx, C_px),
                    "D": (D_idx, D_px),
                },
                "prz":       (prz_lo, prz_hi),
                "completed": True,
                "confidence": confidence,
                "confluence": hits,
            }

            # 去重：同 D_idx 只保留最高 confidence
            existing = best_by_d.get(D_idx)
            if existing is None or confidence > existing["confidence"]:
                best_by_d[D_idx] = candidate

    return list(best_by_d.values())


def project_prz(
    X: float,
    A: float,
    B: float,
    C: float,
    direction: str,
    tol: float = 0.05,
    max_prz_width: float = 0.06,
) -> list[dict]:
    """前瞻核心：仅 XABC 四点（D 未成），对每形态反推 D 应落价区（PRZ）。

    规格：
    - 需 hits>=2（B 与 BC 均满足）才投射。
    - 收敛判定：gap = |d_est1 - d_est2| / price_ref。
      · gap > 2*tol → 估计发散，不 emit（return 跳过，降噪）。
    - 带宽上限守卫：若 (prz_hi - prz_lo)/price > max_prz_width(默认 6%)，跳过。
    - confidence = 0.5*(1-avg_pre_dev) + 0.5*(1-min(gap/(2*tol),1))，
      再 min(confidence, 0.85) 封顶（诚实不过 85%）。
    - confluence = 1 + (gap <= tol)（真实收敛证据）。

    Args:
        X, A, B, C: 已确认枢轴价格
        direction:  "bull" | "bear"
        tol:        容差（默认 0.05）
        max_prz_width: PRZ 带宽上限占价格比例（默认 0.06=6%）

    Returns:
        [{"pattern","direction","prz":(lo,hi),"completed":False,"confidence","confluence"}, ...]
        按 confidence 降序排列。
    """
    max_gap = 2.0 * tol

    XA = abs(A - X)
    AB = abs(B - A)
    BC = abs(C - B)

    if XA < 1e-10 or AB < 1e-10:
        return []

    r_b_xa  = _ratio(AB, XA)
    r_bc_ab = _ratio(BC, AB)

    # 基准价格：用 XA 范围（X/A 最大值），避免 A=0 时 price_ref=1 导致 gap 虚高
    price_ref = max(abs(X), abs(A), XA, 1.0)

    results: list[dict] = []

    for pat_name, pat in HARMONIC_RATIOS.items():
        hits = 0
        deviations: list[float] = []

        # B 约束校验
        b_lo, b_hi = pat["b_xa"]
        if _within(r_b_xa, b_lo, b_hi, tol):
            hits += 1
            mid = (b_lo + b_hi) / 2
            deviations.append(abs(r_b_xa - mid) / (mid + 1e-10))

        # BC/AB 约束校验
        bc_lo, bc_hi = pat["bc_ab"]
        if _within(r_bc_ab, bc_lo, bc_hi, tol):
            hits += 1
            mid = (bc_lo + bc_hi) / 2
            deviations.append(abs(r_bc_ab - mid) / (mid + 1e-10))

        # 需至少满足 2 条前置约束才做前瞻投射
        if hits < 2:
            continue

        # 用 d_xa 区间中心估计 D（① 路径）
        d_lo, d_hi = pat["d_xa"]
        d_xa_mid = (d_lo + d_hi) / 2
        if direction == "bull":
            d_est1 = A - XA * d_xa_mid   # ① 从 A 向下投射
        else:
            d_est1 = A + XA * d_xa_mid   # ① 从 A 向上投射

        # 用 cd_bc 区间中心估计 D（② 路径，从 C 投射）
        cd_lo, cd_hi = pat["cd_bc"]
        cd_bc_mid = (cd_lo + cd_hi) / 2
        if BC > 1e-10:
            if direction == "bull":
                d_est2 = C - BC * cd_bc_mid  # ② 从 C 向下投射
            else:
                d_est2 = C + BC * cd_bc_mid  # ② 从 C 向上投射
        else:
            d_est2 = d_est1

        # 收敛判定：发散 → 跳过不 emit（降噪）
        gap = abs(d_est1 - d_est2) / price_ref
        if gap > max_gap:
            continue

        # PRZ 构造（收敛语义）：以两估计均值为中心，半宽 = max(估计偏差, tol*0.5)*price_ref
        d_mid = (d_est1 + d_est2) / 2.0
        half_span = max(abs(d_est1 - d_est2) / 2.0, price_ref * tol * 0.5)
        prz_lo = d_mid - half_span
        prz_hi = d_mid + half_span

        # 保证顺序（理论场景 d_mid 可能为负）
        prz_lo, prz_hi = min(prz_lo, prz_hi), max(prz_lo, prz_hi)

        # 带宽上限守卫（>6% 跳过）
        width_pct = (prz_hi - prz_lo) / price_ref
        if width_pct > max_prz_width:
            continue

        # 置信诚实化：封顶 0.85（前瞻预测不宣称 >85%，CLAUDE.md 诚实）
        avg_pre_dev = sum(deviations) / max(len(deviations), 1)
        confidence_raw = 0.5 * (1.0 - avg_pre_dev) + 0.5 * (1.0 - min(gap / max_gap, 1.0))
        confidence = max(0.0, min(0.85, confidence_raw))

        # confluence = 真实收敛证据：1 + (gap<=tol)
        confluence = 1 + int(gap <= tol)

        results.append({
            "pattern":    pat_name,
            "direction":  direction,
            "prz":        (prz_lo, prz_hi),
            "completed":  False,
            "confidence": confidence,
            "confluence": confluence,
        })

    # 按 confidence 降序
    results.sort(key=lambda r: r["confidence"], reverse=True)
    return results


def _merge_completed_by_d(
    raw_lists: list[list[dict]],
) -> dict[int, dict]:
    """共享 merge helper：把多组 completed 结果（来自 detect_xabcd + detect_all_ext）
    按 D_idx 去重，同 D_idx 保留最高 confidence 候选。

    Args:
        raw_lists: 每组已是同一来源内去重后的 completed 列表（detect_xabcd 或 detect_ext）。

    Returns:
        {D_idx: best_candidate_dict}（再由调用方过滤 + 排序）。
    """
    best_by_d: dict[int, dict] = {}
    for group in raw_lists:
        for item in group:
            d_idx = item["points"]["D"][0]
            existing = best_by_d.get(d_idx)
            if existing is None or item["confidence"] > existing["confidence"]:
                best_by_d[d_idx] = item
    return best_by_d


def analyze_candles(
    candles: list[Any],
    order: int = 2,
    tol: float = 0.07,
) -> dict:
    """综合分析：找枢轴 → 完整形态 + 成形中（前瞻）。

    包含经典 4 种 XA-anchored 形态（Gartley/Bat/Butterfly/Crab）以及扩展形态
    （Cypher/Shark/ABCD）——通过 harmonic_ext 并入，共享同 D_idx 去重逻辑。

    - completed 只取 D 在最近枢轴的（可操作性过滤，排除远古形态）。
    - completed/forming 各按 confidence 降序。

    ⚡ 高灵敏模式（order=2 / tol=7%）：含更多早期形态，误检率上升，止损必执行。

    Args:
        candles: K 线列表（需 .h/.l/.c 属性）
        order:   枢轴邻域（默认 2，高灵敏）
        tol:     比率容差（默认 0.07 = 7%）

    Returns:
        {
            "completed": [... detect_xabcd + ext 合并结果 ...],
            "forming":   [... project_prz + ext 前瞻合并结果 ...],
            "price":     float,   # 最后一根 close
        }
    """
    # 局部导入避免循环（harmonic_ext 依赖本模块 _ratio/_within）
    from .harmonic_ext import (  # noqa: PLC0415
        detect_all_ext,
        project_cypher_prz,
        project_shark_prz,
    )

    price = float(candles[-1].c) if candles else 0.0
    empty = {"completed": [], "forming": [], "price": price}

    # 使用 pivots_from_structure（append-only 不可变 swing 流，根治 repaint）
    pivots = pivots_from_structure(candles, order=order)
    if len(pivots) < 5:
        return empty

    # --- completed：经典 + 扩展并入（同 D_idx 去重保留最高 confidence）---
    completed_classic = detect_xabcd(pivots, tol=tol)
    completed_ext = detect_all_ext(pivots, tol=tol)
    best_by_d = _merge_completed_by_d([completed_classic, completed_ext])

    # 只保留 D 在最近若干枢轴内的完整形态（可操作性过滤）
    # D_idx 在最后 max(8, 后 60% 枢轴) 内
    n_pivots = len(pivots)
    recent_cutoff = max(n_pivots - 8, int(n_pivots * 0.40))
    recent_d_idxs = {p[0] for p in pivots[recent_cutoff:]}
    # 完整形态须 D 接近现价才算可操作「入场触发」（远古形态 D 距现价过远 → 过滤，诚实）
    completed = [
        r for r in best_by_d.values()
        if r["points"]["D"][0] in recent_d_idxs
        and price > 0
        and abs(r["points"]["D"][1] - price) / price <= _COMPLETED_MAX_DIST
    ]

    # 按 confidence 降序
    completed.sort(key=lambda r: r["confidence"], reverse=True)

    # --- forming：经典前瞻 + 扩展前瞻（Cypher/Shark）并入（按 confidence 合并排序）---
    # 注：ABCD 前瞻不在此路径（其 4 点方向逻辑与 XABC 上下文不兼容，避免误报）。
    # Cypher/Shark 前瞻含自身几何约束门槛（C>A / B<X），与 order_ok 门槛互补，
    # 不满足自身约束时自动返回 []（内部守卫），不需额外 order_ok 过滤。
    if len(pivots) >= 4:
        last4 = pivots[-4:]
        X_px = last4[0][1]
        A_px = last4[1][1]
        B_px = last4[2][1]
        C_px = last4[3][1]
        direction = "bull" if A_px > X_px else "bear"
        # 结构次序校验（与 completed 路径保持一致）：
        # bull 须 X<B<C<A，bear 须 X>B>C>A；不满足则结构不成立，不投射前瞻 PRZ。
        if direction == "bull":
            order_ok = (X_px < B_px < C_px < A_px)
        else:
            order_ok = (X_px > B_px > C_px > A_px)
        forming_classic = (
            project_prz(X_px, A_px, B_px, C_px, direction=direction, tol=tol)
            if order_ok else []
        )
        # 扩展前瞻：Cypher/Shark（各含自身几何约束，结构不满足自动返回 []）
        forming_ext = (
            project_cypher_prz(X_px, A_px, B_px, C_px, direction, tol=tol)
            + project_shark_prz(X_px, A_px, B_px, C_px, direction, tol=tol)
        )
        # 合并：按 confidence 降序（forming 无 D_idx，不做 D_idx 去重）
        forming = sorted(
            forming_classic + forming_ext,
            key=lambda r: r["confidence"],
            reverse=True,
        )
    else:
        forming = []

    return {"completed": completed, "forming": forming, "price": price}
