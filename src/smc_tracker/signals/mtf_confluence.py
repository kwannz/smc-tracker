"""多时间框架(MTF)分层入场决策（用户规范）。

三层共振门控(顶层定向、中层确认、底层触发),只在层层对齐时入场,否则 hold:
  - **顶层 high_direction** = 12h+1d 多数（定方向）
  - **中层 mid_direction**  = 1h+4h 多数（确认）；**顶层与中层必须同向,否则 hold**
  - **底层** 5m+15m 至少一个支持该方向才入场，取同向中**最高 confidence** 的 decision

第一性:大周期定趋势方向(抗噪)、中周期确认(防假突破)、小周期择时入场(精确触发);
层层不同向=趋势未对齐=不交易(纪律性 hold)。纯函数,无副作用,易测。
"""
from __future__ import annotations

from typing import Any

# 各层时间框架(大小写不敏感匹配,兼容 12h/12H、1d/1D 等写法)
_TOP = ("12h", "1d")
_MID = ("1h", "4h")
_BOTTOM = ("5m", "15m")


def _get(decisions: dict[str, Any], tf: str) -> dict | None:
    """大小写不敏感取某 tf 的 decision。"""
    for k, v in decisions.items():
        if k.lower() == tf.lower():
            return v if isinstance(v, dict) else None
    return None


def _majority(decisions: dict[str, Any], tfs: tuple[str, ...]) -> str | None:
    """该层多数方向:'long'/'short'/None(无表态或平局=无明确方向)。"""
    votes = []
    for tf in tfs:
        d = _get(decisions, tf)
        dir_ = d.get("direction") if d else None
        if dir_ in ("long", "short"):
            votes.append(dir_)
    if not votes:
        return None
    longs, shorts = votes.count("long"), votes.count("short")
    if longs > shorts:
        return "long"
    if shorts > longs:
        return "short"
    return None                       # 平局 → 无明确方向


def mtf_decision(decisions: dict[str, Any]) -> dict | None:
    """分层 MTF 入场决策。decisions={tf: {"direction","confidence", ...}}。

    返回入场 decision(底层同向中 confidence 最高的那条,附 direction/层向元信息)或 None(hold)。
    """
    high = _majority(decisions, _TOP)
    mid = _majority(decisions, _MID)
    # 顶层无向 / 中层无向 / 顶中不同向 → 纪律性 hold
    if high is None or mid is None or high != mid:
        return None
    direction = high
    # 底层(5m/15m)至少一个支持该方向
    bottom = []
    for tf in _BOTTOM:
        d = _get(decisions, tf)
        if d and d.get("direction") == direction:
            bottom.append({**d, "_tf": tf})
    if not bottom:
        return None                   # 底层无支持 → hold
    # 取同向中最高 confidence 的 decision 作入场
    best = max(bottom, key=lambda d: float(d.get("confidence", 0.0) or 0.0))
    return {**best, "direction": direction, "high_dir": high, "mid_dir": mid,
            "entry_tf": best["_tf"]}


def fmt_mtf(coin: str, decision: dict | None) -> str:
    """MTF 决策的简短文本。None → hold(未对齐)。"""
    if decision is None:
        return f"🔭 {coin} MTF: ⏸ HOLD（顶/中层未对齐或底层无触发）"
    d = "做多🟢" if decision["direction"] == "long" else "做空🔴"
    return (f"🔭 {coin} MTF入场 {d} @ {decision['entry_tf']} "
            f"置信{float(decision.get('confidence', 0) or 0):.0%}"
            f"（顶{decision['high_dir']}=中{decision['mid_dir']}对齐）")
