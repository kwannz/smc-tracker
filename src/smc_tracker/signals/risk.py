"""信号风险参数：基于 SMC 结构计算入场/止损/目标/盈亏比。

第一性原理：止损放在「结构失效位」—— 做多放最近摆动低点或看涨 OB 下沿之下，
做空放最近摆动高点或看跌 OB 上沿之上；目标按盈亏比投射。止损过远（setup 差）则拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RiskPlan:
    entry: float
    stop: float
    target: float
    rr: float
    stop_pct: float       # 止损距离占入场价比例


def compute_risk(
    direction: str,
    price: float,
    swing_low: float | None,
    swing_high: float | None,
    ob_bottom: float | None,
    ob_top: float | None,
    *,
    target_rr: float = 2.0,
    buffer_pct: float = 0.001,
    default_stop_pct: float = 0.02,
    min_stop_pct: float = 0.002,
    max_stop_pct: float = 0.08,
) -> RiskPlan | None:
    """计算交易计划；止损过近（<min_stop_pct）或过远（>max_stop_pct）返回 None（劣质 setup）。"""
    if price <= 0:
        return None
    entry = price
    if direction == "long":
        # 取入场下方最近的结构位作止损基准（多个候选取最高=最紧），叠加缓冲；
        # 无结构位时退化为固定百分比止损（不再叠 buffer）。
        # 用 is not None 而非真值判断，避免合法 0 价位被误杀
        cands = [x for x in (swing_low, ob_bottom) if x is not None and 0 < x < entry]
        stop = max(cands) * (1 - buffer_pct) if cands else entry * (1 - default_stop_pct)
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + target_rr * risk
    elif direction == "short":
        # 用 is not None 而非真值判断，避免合法 0 价位被误杀
        cands = [x for x in (swing_high, ob_top) if x is not None and x > entry]
        stop = min(cands) * (1 + buffer_pct) if cands else entry * (1 + default_stop_pct)
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - target_rr * risk
    else:
        return None

    stop_pct = abs(entry - stop) / entry
    # 止损过近（噪音级）或过远（风险过高）均视为劣质 setup
    if stop_pct < min_stop_pct or stop_pct > max_stop_pct:
        return None
    return RiskPlan(entry=entry, stop=stop, target=target, rr=target_rr, stop_pct=stop_pct)


# ── 仓位管理：固定分数风险法 (Fixed Fractional Position Sizing) ─────────────────


@dataclass(slots=True)
class PositionSize:
    """仓位计算结果。"""
    qty: float        # 数量（币/合约张）
    notional: float   # 名义价值 USD = qty * entry
    risk_usd: float   # 风险金额 USD：未缩仓=account_usd*risk_pct；capped=True 时为缩仓后真实风险 qty*|entry-stop|
    leverage: float   # 杠杆 = notional / account_usd（提示用）
    capped: bool      # 是否因超 max_leverage 被缩仓


def compute_position_size(
    account_usd: float,
    risk_pct: float,
    entry: float,
    stop: float,
    *,
    max_leverage: float = 10.0,
) -> PositionSize | None:
    """固定分数风险法仓位计算。

    逻辑：
    1. 守卫：劣质输入（零/负账户、非法风险比例、零或负入场价、止损等于入场）→ None。
    2. risk_usd = account_usd * risk_pct（每笔愿意亏的金额）。
    3. per_unit_risk = abs(entry - stop)（每单位风险）。
    4. qty = risk_usd / per_unit_risk；notional = qty * entry；leverage = notional / account_usd。
    5. 若 leverage > max_leverage：缩仓至 max_leverage 上限，capped=True，
       并重算 risk_usd = qty * per_unit_risk（缩仓后真实风险，避免高估组合风险加总）；
       否则 capped=False，risk_usd 保持 account_usd * risk_pct。
    6. 全程无 NaN/inf（守卫已排除零除）。
    """
    # 守卫：劣质输入 → 拒绝，不产生静默错误
    if account_usd <= 0:
        return None
    if risk_pct <= 0 or risk_pct > 1:
        return None
    if entry <= 0:
        return None
    per_unit_risk = abs(entry - stop)
    if per_unit_risk <= 0:
        return None

    risk_usd = account_usd * risk_pct
    qty = risk_usd / per_unit_risk
    notional = qty * entry
    leverage = notional / account_usd  # account_usd > 0 已守卫，无除零风险

    if leverage > max_leverage:
        # 缩仓：按最大杠杆反算数量，止损位不变，实际风险随之降低。
        # risk_usd 同步重算为缩仓后真实每笔风险，避免高估组合风险加总。
        qty = (max_leverage * account_usd) / entry
        notional = qty * entry
        leverage = notional / account_usd  # 等于 max_leverage，浮点精度内
        risk_usd = qty * per_unit_risk     # 缩仓后真实风险（< account_usd * risk_pct）
        capped = True
    else:
        capped = False

    return PositionSize(
        qty=qty,
        notional=notional,
        risk_usd=risk_usd,
        leverage=leverage,
        capped=capped,
    )
