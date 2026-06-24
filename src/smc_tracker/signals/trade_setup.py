"""trade_setup.py — 谐波形态 + 斐波那契汇合 + 风险/仓位 + KNN 历史验证组合成可执行交易 setup。

核心逻辑（第一性原理，复用已有接口）：
  1. 遍历 harmonic_result 的 completed + forming 形态。
  2. 进场区：completed=D±1.5%（X 失效位前收窄）；forming=PRZ 全宽（D 未定）。
  3. 止损/目标 via compute_risk：completed 用 X 点作失效位；forming 用 prz_lo/prz_hi。
  4. 仓位 via compute_position_size。
  5. KNN 历史验证 via validate_direction（样本不足则 knn_supports=None）。
  6. 综合置信 = 谐波 confidence × KNN（不再对 completed 施加 Fib 虚高乘数），封顶 0.90。
  7. 返回按 completed 优先、再置信降序的 TradeSetup 列表。

诚实标注（CLAUDE.md §二）：KNN≈随机基线，高 lift≠赚钱，仅辅助参考。

修复（审计确认）：
  🔴-1: TradeSetup 新增 src_key 字段，build_setups 按 D 点/PRZ 生成唯一键，
        harmonic_monitor 按相同规则精确注入，消除同名形态碰撞。
  🟡-1: completed 进场区收窄到 D±1.5%（不再用 PRZ 10% 宽）。
  🟡-2: completed 不再因 Fib 汇合 ×1.1；fib_note 改诚实说明，不宣称"黄金口袋加分"。
  🟡-5: completed 止损基准改用 X 点（形态失效位）；过远返回 None 跳过（诚实）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..indicators.fibonacci import fib_levels, nearest_fib
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

    if is_completed and points:
        # 🟡-1: completed 进场区收窄到 D±1.5%
        d_price: float = float(points["D"][1])
        entry_lo = d_price * (1 - _COMPLETED_ENTRY_HALF_PCT)
        entry_hi = d_price * (1 + _COMPLETED_ENTRY_HALF_PCT)
        entry_mid = d_price  # D 就是中点

        # 🔴-1: src_key 含 D 点价格，不同 D_idx 的同名形态不碰撞
        src_key = f"C|{pat_name}|{direction}|{d_price}"

        # 🟡-2: completed fib_note = 诚实说明，不再用 nearest_fib 宣称"黄金口袋加分"
        fib_note = _fib_note_completed()

        # 🟡-5: completed 止损基准用 X 点（形态失效位）
        x_price: float = float(points["X"][1])

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

        # forming fib_note = PRZ 近似说明
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

    # 第二目标（更高 R:R = 2 × target_rr）
    risk_amt = abs(entry_mid - plan.stop)
    if direction == "long":
        target2 = entry_mid + 2.0 * target_rr * risk_amt
    else:
        target2 = entry_mid - 2.0 * target_rr * risk_amt

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

    # ── 6. 综合置信（🟡-2: 去掉 fib_mult，不再对 completed 虚高 ×1.1） ─────────
    base_conf: float = float(pattern.get("confidence", 0.5))
    conf = base_conf  # 🟡-2: 不再乘 fib_mult

    # KNN 调权（诚实：影响较小，KNN≈随机基线）
    if knn_supports is True:
        conf *= 1.05
    elif knn_supports is False:
        conf *= 0.90
    # knn_supports is None 时不调整

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
        target1=plan.target,
        target2=target2,
        rr=plan.rr,
        fib_note=fib_note,
        knn_supports=knn_supports,
        knn_note=knn_note,
        position_qty=qty,
        position_notional=notional,
        confidence=conf,
        note=_HONEST_NOTE,
        src_key=src_key,  # 🔴-1
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
