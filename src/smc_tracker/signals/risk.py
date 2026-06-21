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
