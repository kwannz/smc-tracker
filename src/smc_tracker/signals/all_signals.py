"""共享聚合 helper：collect_all_signals。

读取 11 张信号表，归一化为统一行结构，按 ts 倒序合并返回。
表不存在时优雅跳过（sqlite3.OperationalError），不阻塞调用方。

统一行字段：
  type          机器键（信号类型英文标识）
  type_label    中文标签（如 跟庄 / 背离 / 共识 等）
  coin          交易对/代币名
  direction     long / short / None
  ts            时间戳 ms（整数）
  price         参考价格 REAL（无则 None）
  score         信号置信度/分数 REAL（无则 None）
  evidence      dict（该类型专属证据字段，可直接序列化为 JSON）
  evidence_text 一行人类可读证据摘要（非空）

每张表各限 20 行最新记录（ts DESC）；合并后按 ts 倒序输出。
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from ..util import fmt_px, to_float

if TYPE_CHECKING:
    from ..storage.db import Store


# ---- 内部辅助 ----

def _safe_fetchall(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
) -> list[tuple]:
    """执行查询；表不存在（OperationalError）时返回 []，不抛。"""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _sf(v: Any, default: float | None = None) -> float | None:
    """安全转 float；None/空/NaN/inf → default。"""
    if v is None:
        return default
    f = to_float(v, float("nan"))
    import math
    return default if math.isnan(f) else f


def _addr_short(addr: str | None) -> str:
    """地址缩写：0x 前缀保留 6 位尾 + 中间省略。"""
    if not addr:
        return ""
    s = str(addr)
    if len(s) > 14:
        return s[:8] + ".." + s[-6:]
    return s


def _fmt_usd(v: float | None) -> str:
    """名义金额格式化：万/亿 中文单位。"""
    if v is None:
        return "?"
    a = abs(v)
    if a >= 1e8:
        return f"{v/1e8:.2f}亿"
    if a >= 1e4:
        return f"{v/1e4:.1f}万"
    return fmt_px(v)


# ---- 各表解析函数 ----

def _parse_signals(rows: list[tuple]) -> list[dict]:
    """解析 signals（SMC 共振信号）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, coin, direction, score, structure_bias, flow_bias,
         flow_net_usd, oi_change_pct, onchain_usd,
         entry, stop, target, rr, reason) = row
        ev: dict[str, Any] = {
            "structure_bias": _sf(structure_bias),
            "flow_bias": _sf(flow_bias),
            "flow_net_usd": _sf(flow_net_usd),
            "oi_change_pct": _sf(oi_change_pct),
            "onchain_usd": _sf(onchain_usd),
            "entry": _sf(entry),
            "stop": _sf(stop),
            "target": _sf(target),
            "rr": _sf(rr),
            "reason": reason,
        }
        # 证据摘要：方向+入场价+RR+原因
        parts = [f"{direction or '?'} entry={fmt_px(entry)}"]
        if rr:
            parts.append(f"RR={to_float(rr):.1f}")
        if reason:
            parts.append(str(reason)[:40])
        result.append({
            "type": "signal",
            "type_label": "SMC共振",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": _sf(entry),
            "score": _sf(score),
            "evidence": ev,
            "evidence_text": " ".join(parts),
        })
    return result


def _parse_divergence(rows: list[tuple]) -> list[dict]:
    """解析 divergence（CEX⟂DEX 背离）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, coin, direction, score, funding,
         oi_change_pct, dex_flow_usd, reason) = row
        ev: dict[str, Any] = {
            "funding": _sf(funding),
            "oi_change_pct": _sf(oi_change_pct),
            "dex_flow_usd": _sf(dex_flow_usd),
            "reason": reason,
        }
        parts = [f"{direction or '?'}"]
        if funding is not None:
            parts.append(f"资金费={to_float(funding)*100:.3f}%")
        if dex_flow_usd is not None:
            parts.append(f"DEX流向={_fmt_usd(_sf(dex_flow_usd))}")
        if reason:
            parts.append(str(reason)[:40])
        result.append({
            "type": "divergence",
            "type_label": "背离",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": None,
            "score": _sf(score),
            "evidence": ev,
            "evidence_text": " ".join(parts),
        })
    return result


def _parse_whale_signals(rows: list[tuple]) -> list[dict]:
    """解析 whale_signals（跟庄）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, address, label, coin, action, direction,
         notional, px, pos_after, taker) = row
        ev: dict[str, Any] = {
            "address": address,
            "label": label,
            "action": action,
            "notional": _sf(notional),
            "px": _sf(px),
            "pos_after": _sf(pos_after),
            "taker": bool(taker) if taker is not None else None,
        }
        addr_str = label or _addr_short(address)
        ntl_str = _fmt_usd(_sf(notional))
        taker_str = " taker" if taker else ""
        parts = [f"庄{addr_str} {action or ''} {direction or '?'} 净{ntl_str}{taker_str}"]
        result.append({
            "type": "whale_signal",
            "type_label": "跟庄",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": _sf(px),
            "score": None,
            "evidence": ev,
            "evidence_text": parts[0],
        })
    return result


def _parse_position_changes(rows: list[tuple]) -> list[dict]:
    """解析 position_changes（换仓）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, address, label, coin, kind, direction,
         prev_notional, new_notional) = row
        ev: dict[str, Any] = {
            "address": address,
            "label": label,
            "kind": kind,
            "prev_notional": _sf(prev_notional),
            "new_notional": _sf(new_notional),
        }
        addr_str = label or _addr_short(address)
        change_str = f"{_fmt_usd(_sf(prev_notional))}→{_fmt_usd(_sf(new_notional))}"
        text = f"庄{addr_str} {kind or '?'} {direction or '?'} {change_str}"
        result.append({
            "type": "position_change",
            "type_label": "换仓",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": None,
            "score": None,
            "evidence": ev,
            "evidence_text": text,
        })
    return result


def _parse_consensus(rows: list[tuple]) -> list[dict]:
    """解析 consensus（多庄共识）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, coin, direction, n_agree, n_oppose,
         net_notional, score, labels) = row
        ev: dict[str, Any] = {
            "n_agree": n_agree,
            "n_oppose": n_oppose,
            "net_notional": _sf(net_notional),
            "labels": labels,
        }
        text = (
            f"{n_agree or 0}庄一致 净{_fmt_usd(_sf(net_notional))}"
            f" 反对{n_oppose or 0}"
        )
        result.append({
            "type": "consensus",
            "type_label": "共识",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": None,
            "score": _sf(score),
            "evidence": ev,
            "evidence_text": text,
        })
    return result


def _parse_confluence(rows: list[tuple]) -> list[dict]:
    """解析 confluence_signals（超级信号）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, coin, direction, n_sources, sources, opposing, score) = row
        ev: dict[str, Any] = {
            "n_sources": n_sources,
            "sources": sources,
            "opposing": opposing,
        }
        text = f"{n_sources or 0}源共振: {sources or ''} 反对={opposing or 0}"
        result.append({
            "type": "confluence",
            "type_label": "超级共振",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": None,
            "score": _sf(score),
            "evidence": ev,
            "evidence_text": text,
        })
    return result


def _parse_flagged(rows: list[tuple]) -> list[dict]:
    """解析 flagged_addresses（可疑地址）行。

    flagged_addresses 没有 ts 列，用 last_seen_ms 作为时间戳；
    查询时已将 last_seen_ms 别名为 ts 返回。
    """
    result: list[dict] = []
    for row in rows:
        (ts, address, coin, reason, net_usd, promoted) = row
        ev: dict[str, Any] = {
            "address": address,
            "reason": reason,
            "net_usd": _sf(net_usd),
            "promoted": bool(promoted) if promoted is not None else False,
        }
        text = (
            f"可疑地址 {_addr_short(address)}"
            f" 净{_fmt_usd(_sf(net_usd))}"
            f" {reason or ''}"
        )
        result.append({
            "type": "flagged_address",
            "type_label": "可疑地址",
            "coin": coin or "?",
            "direction": None,
            "ts": int(ts),
            "price": None,
            "score": None,
            "evidence": ev,
            "evidence_text": text,
        })
    return result


def _parse_flow_predictions(rows: list[tuple]) -> list[dict]:
    """解析 flow_predictions（前瞻资金流）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, coin, direction, score, vel, accel, book_imb) = row
        ev: dict[str, Any] = {
            "vel": _sf(vel),
            "accel": _sf(accel),
            "book_imb": _sf(book_imb),
        }
        vel_str = f"{_fmt_usd(_sf(vel))}/min" if vel is not None else "?"
        accel_str = f"加速度{_fmt_usd(_sf(accel))}" if accel is not None else ""
        text = f"{direction or '?'} 流速={vel_str} {accel_str} 挂单失衡={to_float(book_imb):.2f}"
        result.append({
            "type": "flow_prediction",
            "type_label": "前瞻资金流",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": None,
            "score": _sf(score),
            "evidence": ev,
            "evidence_text": text.strip(),
        })
    return result


def _parse_okx_signals(rows: list[tuple]) -> list[dict]:
    """解析 okx_signals（OKX 背离）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, coin, direction, kind, funding, net_flow) = row
        ev: dict[str, Any] = {
            "kind": kind,
            "funding": _sf(funding),
            "net_flow": _sf(net_flow),
        }
        funding_str = f"资金费={to_float(funding)*100:.3f}%" if funding is not None else ""
        flow_str = f"净流入={_fmt_usd(_sf(net_flow))}" if net_flow is not None else ""
        text = f"OKX {kind or ''} {direction or '?'} {funding_str} {flow_str}".strip()
        result.append({
            "type": "okx_signal",
            "type_label": "OKX信号",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": None,
            "score": None,
            "evidence": ev,
            "evidence_text": text,
        })
    return result


def _parse_orderbook_walls(rows: list[tuple]) -> list[dict]:
    """解析 hl_orderbook_walls（挂单墙）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, coin, side, kind, px, notional) = row
        ev: dict[str, Any] = {
            "side": side,
            "kind": kind,
            "px": _sf(px),
            "notional": _sf(notional),
        }
        # 挂单墙方向：bid build → 支撑(long意图) / ask build → 压制(short意图)
        direction = "long" if side == "bid" else "short" if side == "ask" else None
        text = (
            f"{side or '?'}墙 {kind or ''} @{fmt_px(px)}"
            f" 名义={_fmt_usd(_sf(notional))}"
        )
        result.append({
            "type": "orderbook_wall",
            "type_label": "挂单墙",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": _sf(px),
            "score": None,
            "evidence": ev,
            "evidence_text": text,
        })
    return result


def _parse_harmonic(rows: list[tuple]) -> list[dict]:
    """解析 harmonic_setups（谐波形态）行。"""
    result: list[dict] = []
    for row in rows:
        (ts, coin, tf, kind, pattern, direction, price,
         entry_lo, entry_hi, stop, target1, target2,
         rr, confidence, prz_lo, prz_hi) = row
        ev: dict[str, Any] = {
            "tf": tf,
            "kind": kind,
            "pattern": pattern,
            "price": _sf(price),
            "entry_lo": _sf(entry_lo),
            "entry_hi": _sf(entry_hi),
            "stop": _sf(stop),
            "target1": _sf(target1),
            "target2": _sf(target2),
            "rr": _sf(rr),
            "prz_lo": _sf(prz_lo),
            "prz_hi": _sf(prz_hi),
        }
        prz_str = (
            f"PRZ {fmt_px(prz_lo)}-{fmt_px(prz_hi)}"
            if prz_lo and prz_hi
            else f"@{fmt_px(price)}"
        )
        conf_str = f"conf{to_float(confidence):.2f}" if confidence is not None else ""
        rr_str = f"RR={to_float(rr):.1f}" if rr else ""
        text = f"{pattern or '?'} {tf or ''} {kind or ''} {direction or '?'} {prz_str} {conf_str} {rr_str}".strip()
        result.append({
            "type": "harmonic_setup",
            "type_label": "谐波形态",
            "coin": coin,
            "direction": direction,
            "ts": int(ts),
            "price": _sf(price),
            "score": _sf(confidence),
            "evidence": ev,
            "evidence_text": text,
        })
    return result


# ---- 主函数 ----

def collect_all_signals(
    store: "Store",
    since_ms: int,
    now_ms: int,
    limit_each: int = 20,
) -> list[dict]:
    """聚合 11 张信号表最近数据，归一化为统一行结构，按 ts 倒序返回。

    Args:
        store:       Store 实例（必须有 .conn: sqlite3.Connection）
        since_ms:    时间窗口起始（含），ts >= since_ms 的行才纳入
        now_ms:      时间窗口终止（含），ts <= now_ms 的行才纳入
        limit_each:  每张表各取最新 N 行（默认 20，防止单表数据爆炸）

    Returns:
        list of dict，每行含统一字段（type/type_label/coin/direction/ts/
        price/score/evidence/evidence_text），按 ts DESC 排序。
    """
    conn = store.conn
    all_rows: list[dict] = []

    # 1. signals（SMC 共振）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,coin,direction,score,structure_bias,flow_bias,"
        "flow_net_usd,oi_change_pct,onchain_usd,"
        "entry,stop,target,rr,reason "
        "FROM signals WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_signals(raw))

    # 2. divergence（背离）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,coin,direction,score,funding,oi_change_pct,dex_flow_usd,reason "
        "FROM divergence WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_divergence(raw))

    # 3. whale_signals（跟庄）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,address,label,coin,action,direction,"
        "notional,px,pos_after,taker "
        "FROM whale_signals WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_whale_signals(raw))

    # 4. position_changes（换仓）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,address,label,coin,kind,direction,"
        "prev_notional,new_notional "
        "FROM position_changes WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_position_changes(raw))

    # 5. consensus（共识）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,coin,direction,n_agree,n_oppose,net_notional,score,labels "
        "FROM consensus WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_consensus(raw))

    # 6. confluence_signals（超级共振）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,coin,direction,n_sources,sources,opposing,score "
        "FROM confluence_signals WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_confluence(raw))

    # 7. flagged_addresses（可疑地址；无 ts 列，用 last_seen_ms 作为 ts）
    raw = _safe_fetchall(
        conn,
        "SELECT last_seen_ms,address,coin,reason,net_usd,promoted "
        "FROM flagged_addresses "
        "WHERE last_seen_ms>=? AND last_seen_ms<=? "
        "ORDER BY last_seen_ms DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_flagged(raw))

    # 8. flow_predictions（前瞻资金流）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,coin,direction,score,vel,accel,book_imb "
        "FROM flow_predictions WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_flow_predictions(raw))

    # 9. okx_signals（OKX 背离）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,coin,direction,kind,funding,net_flow "
        "FROM okx_signals WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_okx_signals(raw))

    # 10. hl_orderbook_walls（挂单墙）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,coin,side,kind,px,notional "
        "FROM hl_orderbook_walls WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_orderbook_walls(raw))

    # 11. harmonic_setups（谐波形态）
    raw = _safe_fetchall(
        conn,
        "SELECT ts,coin,tf,kind,pattern,direction,price,"
        "entry_lo,entry_hi,stop,target1,target2,"
        "rr,confidence,prz_lo,prz_hi "
        "FROM harmonic_setups WHERE ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
        (since_ms, now_ms, limit_each),
    )
    all_rows.extend(_parse_harmonic(raw))

    # 合并后按 ts 倒序
    all_rows.sort(key=lambda r: r["ts"], reverse=True)
    return all_rows
