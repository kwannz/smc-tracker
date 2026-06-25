"""trade_setup.py — 谐波形态 + 斐波那契汇合 + 风险/仓位 + KNN 历史验证组合成可执行交易 setup。

核心逻辑（第一性原理，复用已有接口）：
  1. 遍历 harmonic_result 的 completed + forming 形态。
  2. 进场区：completed=D±1.5%（X 失效位前收窄）；forming=PRZ 全宽（D 未定）。
  3. 止损/目标 via compute_risk：completed 用 X 点作失效位；forming 用 prz_lo/prz_hi。
  4. 仓位 via compute_position_size。
  5. KNN 历史验证 via validate_direction（样本不足则 knn_supports=None）。
  6. ATR2 动量确认 via atr2_confirmation：
     - atr_stop = entry_mid ∓ 1.5×atr（long 减/short 加）
     - atr2_bias = 返回的 bias 字段
     - atr2_confirm = (bias 与 setup 方向一致)；candles 不足→三字段 None 不崩
  7. 综合置信 = 谐波 confidence × KNN × ATR2（封顶 0.90）：
     - atr2_confirm=True → ×1.05；False → ×0.92；None → 不调整
  8. 返回按 completed 优先、再置信降序的 TradeSetup 列表。

诚实标注（CLAUDE.md §二）：KNN≈随机基线，高 lift≠赚钱，ATR2 仅辅助动量确认，不构成投资建议。

修复（审计确认）：
  🔴-1: TradeSetup 新增 src_key 字段，build_setups 按 D 点/PRZ 生成唯一键，
        harmonic_monitor 按相同规则精确注入，消除同名形态碰撞。
  🟡-1: completed 进场区收窄到 D±1.5%（不再用 PRZ 10% 宽）。
  🟡-2: completed 不再因 Fib 汇合 ×1.1；fib_note 改诚实说明，不宣称"黄金口袋加分"。
  🟡-5: completed 止损基准改用 X 点（形态失效位）；过远返回 None 跳过（诚实）。
  任务C: 集成 ATR2 动量确认（atr_stop/atr2_bias/atr2_confirm 字段）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..indicators.atr2_signals import atr2_confirmation
from ..indicators.fibonacci import (
    fib_levels,
    golden_pocket_zone,
    intersect_zone,
    nearest_fib,
)
from .knn_validator import validate_direction
from .orderflow_confirm import OrderflowConfirm
from .risk import RiskPlan, compute_position_size, compute_risk

# ── 诚实免责声明（所有 setup note 都含此项，CLAUDE.md §二） ───────────────────
_HONEST_NOTE = (
    "谐波+Fib+KNN 综合，仅辅助，加订单流确认更可靠；"
    "KNN≈随机基线(CLAUDE.md §二)，历史高 lift≠赚钱，不构成投资建议"
)

# ── 综合置信上限（诚实：不过度夸大模型能力） ────────────────────────────────
_MAX_CONFIDENCE = 0.90

# ── completed 进场区半宽（D±1.5%，🟡-1 收窄进场区） ─────────────────────────
_COMPLETED_ENTRY_HALF_PCT = 0.015


@dataclass(slots=True)
class TradeSetup:
    """可执行交易 setup：进场区/出场/止损止盈/仓位/置信。

    Attributes:
        coin: 币种标识（如 "BTC"）。
        tf: 时间周期（如 "1h"）。
        direction: 方向，"long" 或 "short"（bull→long, bear→short）。
        pattern: 谐波形态名称（如 "Gartley", "Bat"）。
        completed: True=完整形态(入场触发) / False=成形中(前瞻预警)。
        entry_lo: 进场区下沿（completed=D×(1-1.5%)；forming=PRZ 下沿）。
        entry_hi: 进场区上沿（completed=D×(1+1.5%)；forming=PRZ 上沿）。
        stop: 止损价（completed=X 失效位；forming=PRZ 边界）。
        target1: 第一目标（按 target_rr 投射）。
        target2: 第二目标（按 2×target_rr 投射，更激进）。
        rr: 实际盈亏比（target1 基准）。
        fib_note: Fib 说明（completed=诚实说明非独立确认；forming=PRZ 近似说明）。
        knn_supports: KNN 历史是否支持方向，None=样本不足。
        knn_note: KNN 诚实标注（含 KNN≈随机基线警告）。
        position_qty: 建议数量（币/合约），None=仓位计算失败。
        position_notional: 名义价值 USD，None=同上。
        confidence: 综合置信度（封顶 0.90）。
        note: 诚实免责标注。
        src_key: 唯一来源标识，防止同名形态注入碰撞（🔴-1 修复）。
                 completed → f"C|{pattern}|{direction}|{D_idx}"（D_idx=D点价格）
                 forming   → f"F|{pattern}|{direction}|{round(prz_lo, 8)}"
        orderflow: 订单流确认结果（monitor 层注入，build_setups 默认 None）。
                   confirmed=True 表示 PRZ 处有同向挂单墙+失衡确认；None=无订单流数据。
                   ⚠诚实：墙可能 spoof/吸收≠必反转，仅辅助确认非保证。
    """
    coin: str
    tf: str
    direction: str
    pattern: str
    completed: bool
    entry_lo: float
    entry_hi: float
    stop: float
    target1: float
    target2: float
    rr: float
    fib_note: str
    knn_supports: bool | None
    knn_note: str
    position_qty: float | None
    position_notional: float | None
    confidence: float
    note: str
    src_key: str  # 🔴-1: 唯一来源标识，用于注入精确匹配
    # 订单流确认（monitor 层注入；build_setups 纯函数无 ob_provider，留 None）
    orderflow: OrderflowConfirm | None = field(default=None)
    # ATR2 动量确认（任务C：atr2_signals 集成）
    # atr_stop: ATR 止损价（entry_mid ∓ 1.5×atr，long 减/short 加）；candles 不足→None
    atr_stop: float | None = None
    # atr2_bias: ATR2 动量偏向（"long"/"short"/"neutral"）；candles 不足→None
    atr2_bias: str | None = None
    # atr2_confirm: ATR2 bias 与 setup 方向是否一致；candles 不足→None（不调权）
    atr2_confirm: bool | None = None
    # 前瞻确认备注（forward_confirm.apply_forward 注入；含用了/跳过了哪些领先分量）
    # None=未施加前瞻确认（无 provider 或无数据）；completed+forming 都可被注入（解除 completed 门控）
    forward: str | None = None
    # §4 D 斐波那契入场强化：入场区来源标识
    # "fib_intersect"=黄金口袋∩形态区有交集（已收窄）
    # "no_fib_confluence"=无交集，回退原区
    # None=旧路径（向后兼容）
    entry_src: str | None = None


def _direction_map(harmonic_direction: str) -> str | None:
    """将谐波方向 "bull"/"bear" 映射到 "long"/"short"；其它返回 None。"""
    if harmonic_direction == "bull":
        return "long"
    if harmonic_direction == "bear":
        return "short"
    return None


def _fib_note_completed() -> str:
    """🟡-2: completed 形态的诚实 Fib 说明（非独立确认，不宣称加分）。"""
    return "D=形态定义比率位(0.786·XA 等，非独立确认)"


def _fib_note_forming() -> str:
    """forming 形态的 Fib 说明（PRZ 近似，待 D 确认）。"""
    return "(成形:PRZ近似,待D确认)"


def _build_one(
    coin: str,
    tf: str,
    pattern: dict[str, Any],
    candles: list[Any],
    account_usd: float,
    risk_pct: float,
    target_rr: float,
) -> TradeSetup | None:
    """从单个形态 dict 构建 TradeSetup；劣质 setup 返回 None。"""
    # ── 1. 方向映射 ──────────────────────────────────────────────────────────
    harmonic_dir: str = pattern.get("direction", "")
    direction = _direction_map(harmonic_dir)
    if direction is None:
        # 非法方向：跳过，不静默降级
        return None

    pat_name: str = str(pattern.get("pattern", "Unknown"))
    is_completed: bool = bool(pattern.get("completed", False))

    # ── 2. PRZ（始终需要，forming 用全宽，completed 用于计算 src_key 后再收窄） ──
    prz = pattern.get("prz", (None, None))
    if prz is None or len(prz) < 2 or prz[0] is None or prz[1] is None:
        return None

    prz_lo: float = float(prz[0])
    prz_hi: float = float(prz[1])
    if prz_lo >= prz_hi:
        return None

    # ── 3. 进场区 & 止损基准（🟡-1 + 🟡-5 修复） ────────────────────────────
    points: dict | None = pattern.get("points")

    # §4 D 斐波那契入场强化：入场区来源标识（向后兼容，默认 None）
    entry_src: str | None = None

    if is_completed and points:
        # 🟡-1: completed 进场区初始为 D±1.5%
        d_price: float = float(points["D"][1])
        base_lo = d_price * (1 - _COMPLETED_ENTRY_HALF_PCT)
        base_hi = d_price * (1 + _COMPLETED_ENTRY_HALF_PCT)

        # §4 D: 入场精炼 — XA 段黄金口袋∩(D±1.5%)
        x_price: float = float(points["X"][1])
        a_price: float = float(points["A"][1])
        # XA 方向：bull=up（X 低 A 高），bear=down（X 高 A 低）
        xa_dir = "up" if direction == "long" else "down"
        xa_high = max(x_price, a_price)
        xa_low = min(x_price, a_price)
        gp_lo, gp_hi = golden_pocket_zone(xa_high, xa_low, xa_dir)
        intersect = intersect_zone(gp_lo, gp_hi, base_lo, base_hi)
        if intersect is not None:
            # 有交集：收窄入场区到交集（最高概率位）
            entry_lo, entry_hi = intersect
            # 诚实说明：汇合区仅为最优位参考，非独立确认，不宣称"加分"
            fib_note = (
                f"XA黄金口袋∩D±1.5%汇合({entry_lo:.4f},{entry_hi:.4f})"
                f";非独立确认,仅收窄入场参考"
            )
            entry_src = "fib_intersect"
        else:
            # 无交集：回退原区，诚实标注
            entry_lo, entry_hi = base_lo, base_hi
            fib_note = f"无Fib汇合,用形态区(D±1.5%);{_fib_note_completed()}"
            entry_src = "no_fib_confluence"
        entry_mid = (entry_lo + entry_hi) / 2.0

        # 🔴-1: src_key 含 D 点价格，不同 D_idx 的同名形态不碰撞
        src_key = f"C|{pat_name}|{direction}|{d_price}"

        # 🟡-5: completed 止损基准用 X 点（形态失效位）
        # 风险计算（stop/target）
        if direction == "long":
            plan: RiskPlan | None = compute_risk(
                direction="long",
                price=entry_mid,
                swing_low=x_price,   # 🟡-5: X 点作失效位
                swing_high=None,
                ob_bottom=None,
                ob_top=None,
                target_rr=target_rr,
            )
        else:
            plan = compute_risk(
                direction="short",
                price=entry_mid,
                swing_low=None,
                swing_high=x_price,  # 🟡-5: X 点作失效位
                ob_bottom=None,
                ob_top=None,
                target_rr=target_rr,
            )

    else:
        # forming（或 completed 但意外无 points）：保留 PRZ 全宽，止损基于 PRZ 边界
        entry_lo = prz_lo
        entry_hi = prz_hi
        entry_mid = (entry_lo + entry_hi) / 2.0

        # 🔴-1: forming src_key 含 prz_lo，不同 PRZ 不碰撞
        src_key = f"F|{pat_name}|{direction}|{round(prz_lo, 8)}"

        # §4 D 入场精炼（forming）：XA 黄金口袋∩PRZ
        forming_points: dict | None = pattern.get("points")
        if forming_points and "X" in forming_points and "A" in forming_points:
            fp_x: float = float(forming_points["X"][1])
            fp_a: float = float(forming_points["A"][1])
            xa_dir_f = "up" if direction == "long" else "down"
            xa_high_f = max(fp_x, fp_a)
            xa_low_f = min(fp_x, fp_a)
            gp_lo_f, gp_hi_f = golden_pocket_zone(xa_high_f, xa_low_f, xa_dir_f)
            intersect_f = intersect_zone(gp_lo_f, gp_hi_f, entry_lo, entry_hi)
            if intersect_f is not None:
                entry_lo, entry_hi = intersect_f
                entry_mid = (entry_lo + entry_hi) / 2.0
                fib_note = (
                    f"XA黄金口袋∩PRZ汇合({entry_lo:.4f},{entry_hi:.4f})"
                    f";非独立确认,仅收窄入场参考"
                )
                entry_src = "fib_intersect"
            else:
                fib_note = f"无Fib汇合,用形态区;{_fib_note_forming()}"
                entry_src = "no_fib_confluence"
        else:
            # forming 且无 points：回退 PRZ 近似说明
            fib_note = _fib_note_forming()

        # forming 止损基于 PRZ 边界（标注，无 points）
        if direction == "long":
            plan = compute_risk(
                direction="long",
                price=entry_mid,
                swing_low=prz_lo,
                swing_high=None,
                ob_bottom=None,
                ob_top=None,
                target_rr=target_rr,
            )
        else:
            plan = compute_risk(
                direction="short",
                price=entry_mid,
                swing_low=None,
                swing_high=prz_hi,
                ob_bottom=None,
                ob_top=None,
                target_rr=target_rr,
            )

    # 劣质 setup（止损过近/过远）→ 跳过（诚实，不产劣质）
    if plan is None:
        return None

    # §4 D: Fib 扩展目标 — AD 段 1.272/1.618 扩展位，与 RR 目标取更保守者
    # 尝试从 points 取 A/D
    rr_target1 = plan.target  # 现有 RR 目标（target_rr 投射）
    risk_amt = abs(entry_mid - plan.stop)
    rr_target2 = (
        entry_mid + 2.0 * target_rr * risk_amt
        if direction == "long"
        else entry_mid - 2.0 * target_rr * risk_amt
    )
    # 默认使用 RR 目标，fib_note 将在下面追加来源说明
    fib_target1_src = "RR"
    fib_target2_src = "RR"

    _pts: dict | None = pattern.get("points")
    if _pts and "A" in _pts and "D" in _pts:
        _a_px: float = float(_pts["A"][1])
        _d_px: float = float(_pts["D"][1])
        _ad_rng: float = abs(_a_px - _d_px)
        if _ad_rng > 0:
            if direction == "long":
                # bull：A 高 D 低，扩展在 D 下方（逆向扩展：A+AD×ext 会在 A 上方，不对）
                # 谐波 AD 扩展：bull 完成后目标通常是 A 点上方 AD 段扩展
                # 1.272/1.618 扩展：从 D 出发，以 AD 幅度为基础向上延伸
                fib_t1_raw = _d_px + 1.272 * _ad_rng  # 1.272 扩展（保守目标）
                fib_t2_raw = _d_px + 1.618 * _ad_rng  # 1.618 扩展（激进目标）
                # 取更保守者（值更小的，接近入场点）
                target1 = min(rr_target1, fib_t1_raw)
                target2 = min(rr_target2, fib_t2_raw)
                fib_target1_src = "Fib1.272" if fib_t1_raw < rr_target1 else "RR"
                fib_target2_src = "Fib1.618" if fib_t2_raw < rr_target2 else "RR"
            else:
                # bear：A 低 D 高，扩展在 D 下方（从 D 向下延伸）
                fib_t1_raw = _d_px - 1.272 * _ad_rng
                fib_t2_raw = _d_px - 1.618 * _ad_rng
                # short 方向取更保守者（值更大的，接近入场点）
                target1 = max(rr_target1, fib_t1_raw)
                target2 = max(rr_target2, fib_t2_raw)
                fib_target1_src = "Fib1.272" if fib_t1_raw > rr_target1 else "RR"
                fib_target2_src = "Fib1.618" if fib_t2_raw > rr_target2 else "RR"
        else:
            # AD 幅度为零，退化到 RR 目标
            target1 = rr_target1
            target2 = rr_target2
    else:
        # 无 points（forming 且缺 A/D），直接用 RR 目标
        target1 = rr_target1
        target2 = rr_target2

    # 追加 Fib 目标来源到 fib_note
    fib_note = f"{fib_note};T1={fib_target1_src},T2={fib_target2_src}"

    # ── 4. 仓位计算 ──────────────────────────────────────────────────────────
    pos = compute_position_size(
        account_usd=account_usd,
        risk_pct=risk_pct,
        entry=entry_mid,
        stop=plan.stop,
    )
    qty: float | None = pos.qty if pos is not None else None
    notional: float | None = pos.notional if pos is not None else None

    # ── 5. KNN 历史验证 ───────────────────────────────────────────────────────
    verdict = validate_direction(candles, direction)
    if verdict is None:
        knn_supports: bool | None = None
        knn_note = "样本不足"
    else:
        knn_supports = verdict.supports
        knn_note = verdict.note

    # ── 5b. ATR2 动量确认（任务C） ──────────────────────────────────────────────
    atr2_result = atr2_confirmation(candles)
    if atr2_result is not None:
        # candles 足够：填充三字段
        atr2_bias_val: str | None = atr2_result["bias"]
        # atr_stop：entry_mid ∓ 1.5×atr（long 减/short 加）
        _atr_val = atr2_result["atr"]
        if direction == "long":
            atr_stop_val: float | None = entry_mid - 1.5 * _atr_val
        else:
            atr_stop_val = entry_mid + 1.5 * _atr_val
        # atr2_confirm：bias 与 setup 方向是否一致
        atr2_confirm_val: bool | None = (atr2_bias_val == direction)
    else:
        # candles 不足：三字段均 None，不崩不加权
        atr2_bias_val = None
        atr_stop_val = None
        atr2_confirm_val = None

    # ── 6. 综合置信（🟡-2: 去掉 fib_mult，不再对 completed 虚高 ×1.1） ─────────
    base_conf: float = float(pattern.get("confidence", 0.5))
    conf = base_conf  # 🟡-2: 不再乘 fib_mult

    # KNN 降级为纯展示（QA P1-5）：KNN≈随机基线（项目自承），用随机信号乘性调权 = 给确定性
    # 几何分注入噪声。停止调权，保留 knn_supports/knn_note 字段作透明展示，不影响 confidence。
    # （前瞻边缘改由 forward_confirm 的领先信号 OI/funding/flow 提供，而非回看的 KNN。）

    # ATR2 调权：同向×1.05，**相反×0.80（重罚）**；None 不调整（candles 不足）。
    # autosearch Round1 实证(causal前向): ATR2 同向子集胜率 82.5% vs 反向 50%(随机) vs 基线 74%
    # → 反向是低质信号(随机水平)，应重罚而非轻调；故 ×0.80（原 ×0.92 太轻）。
    if atr2_confirm_val is True:
        conf *= 1.05
    elif atr2_confirm_val is False:
        conf *= 0.80
    # atr2_confirm_val is None 时不调整

    # 封顶
    conf = min(conf, _MAX_CONFIDENCE)

    # ── 7. 组装 TradeSetup ─────────────────────────────────────────────────────
    return TradeSetup(
        coin=coin,
        tf=tf,
        direction=direction,
        pattern=pat_name,
        completed=is_completed,
        entry_lo=entry_lo,
        entry_hi=entry_hi,
        stop=plan.stop,
        target1=target1,   # §4 D: Fib 扩展 or RR，取更保守者
        target2=target2,   # §4 D: Fib 扩展 or RR，取更保守者
        rr=plan.rr,
        fib_note=fib_note,
        knn_supports=knn_supports,
        knn_note=knn_note,
        position_qty=qty,
        position_notional=notional,
        confidence=conf,
        note=_HONEST_NOTE,
        src_key=src_key,  # 🔴-1
        atr_stop=atr_stop_val,
        atr2_bias=atr2_bias_val,
        atr2_confirm=atr2_confirm_val,
        entry_src=entry_src,  # §4 D: 入场区来源标识
    )


def build_setups(
    coin: str,
    tf: str,
    candles: list[Any],
    harmonic_result: dict[str, Any],
    *,
    account_usd: float = 10_000.0,
    risk_pct: float = 0.01,
    target_rr: float = 2.0,
) -> list[TradeSetup]:
    """从谐波分析结果构建可执行交易 setup 列表。

    遍历 harmonic_result 的 completed + forming 形态，整合：
    - 进场区（completed=D±1.5%，forming=PRZ）
    - 止损/止盈（compute_risk，completed 用 X 失效位，劣质 setup 跳过）
    - 仓位（compute_position_size，固定分数风险法）
    - KNN 历史方向验证（样本不足时 knn_supports=None，不崩溃）
    - 综合置信（谐波×KNN，不含 Fib 虚高乘数，诚实封顶 0.90）

    排序规则：completed 优先，再按置信降序。
    每个 setup 含唯一 src_key（D 点或 PRZ 下沿），用于 harmonic_monitor 精确注入。

    Args:
        coin: 币种标识。
        tf: 时间周期字符串。
        candles: K 线列表（list[dict] with open/high/low/close/volume）。
        harmonic_result: analyze_candles 输出（含 "completed" 和 "forming" 两列表）。
        account_usd: 账户资金量（USD），用于仓位计算。
        risk_pct: 单笔风险比例（默认 1%）。
        target_rr: 目标盈亏比（默认 2.0）。

    Returns:
        list[TradeSetup]，completed 优先、同级按置信降序；可为空列表。
    """
    # 数据质量守卫
    if not isinstance(harmonic_result, dict):
        return []

    completed_patterns: list[dict] = harmonic_result.get("completed") or []
    forming_patterns: list[dict] = harmonic_result.get("forming") or []

    completed_setups: list[TradeSetup] = []
    forming_setups: list[TradeSetup] = []

    for pattern in completed_patterns:
        setup = _build_one(
            coin=coin,
            tf=tf,
            pattern=pattern,
            candles=candles,
            account_usd=account_usd,
            risk_pct=risk_pct,
            target_rr=target_rr,
        )
        if setup is not None:
            completed_setups.append(setup)

    for pattern in forming_patterns:
        setup = _build_one(
            coin=coin,
            tf=tf,
            pattern=pattern,
            candles=candles,
            account_usd=account_usd,
            risk_pct=risk_pct,
            target_rr=target_rr,
        )
        if setup is not None:
            forming_setups.append(setup)

    # 同组内按置信降序
    completed_setups.sort(key=lambda s: s.confidence, reverse=True)
    forming_setups.sort(key=lambda s: s.confidence, reverse=True)

    # completed 优先
    return completed_setups + forming_setups
