"""摘要日报：从 SQLite 聚合近窗信号/背离/聪明钱净流向/链上活动，生成文本摘要。"""
from __future__ import annotations

import time
from typing import Any


def _hms(ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000)) if ms else "--:--:--"


def build_report(store: Any, since_ms: int, now_ms: int, title: str = "SMC 摘要") -> str:
    c = store.conn
    gen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ms / 1000)) if now_ms else ""
    lines: list[str] = [f"📊 {title}（近 {(now_ms - since_ms) // 60000} 分钟 · 生成于 {gen}）"]

    # 共振信号
    sigs = c.execute(
        "SELECT ts,coin,direction,score,entry,stop,target,rr FROM signals "
        "WHERE ts>=? ORDER BY ts DESC LIMIT 8", (since_ms,)).fetchall()
    lines.append(f"\n⚡ 共振信号 {len(sigs)} 条：")
    for s in sigs:
        d = "做多" if s[2] == "long" else "做空"
        plan = (f" 入{s[4]:g}/损{s[5]:g}/标{s[6]:g} RR{s[7]:g}" if s[4] else "")
        lines.append(f"  [{_hms(s[0])}] {s[1]} {d} 分{s[3]:+.2f}{plan}")
    if not sigs:
        lines.append("  （无）")

    # 背离信号
    divs = c.execute(
        "SELECT ts,coin,direction,score,funding,dex_flow_usd FROM divergence "
        "WHERE ts>=? ORDER BY ts DESC LIMIT 8", (since_ms,)).fetchall()
    lines.append(f"\n🔀 背离信号 {len(divs)} 条：")
    for d in divs:
        tag = "吸筹(看涨)" if d[2] == "bullish" else "分销(看跌)"
        lines.append(f"  [{_hms(d[0])}] {d[1]} {tag} 分{d[3]:.2f} "
                     f"funding{d[4]*100:+.3f}% flow${d[5]:,.0f}")
    if not divs:
        lines.append("  （无）")

    # 聪明钱净流向 Top（按 |净额|）
    flows = c.execute(
        "SELECT coin, SUM(CASE WHEN taker_side='B' THEN notional ELSE -notional END) net "
        "FROM hl_meme_trades WHERE time_ms>=? GROUP BY coin ORDER BY ABS(net) DESC LIMIT 6",
        (since_ms,)).fetchall()
    if flows:
        lines.append("\n🐋 聪明钱主动净流向 Top：")
        for coin, net in flows:
            arrow = "净买" if net >= 0 else "净卖"
            lines.append(f"  {coin} {arrow} ${abs(net):,.0f}")

    # 链上 + SOL 供应（表由监控器自建，可能尚不存在）
    def _count(sql: str) -> int:
        try:
            return c.execute(sql, (since_ms,)).fetchone()[0]
        except Exception:  # noqa: BLE001
            return 0
    onchain_n = _count("SELECT COUNT(*) FROM onchain_transfers WHERE ts>=?")
    n_sol = _count("SELECT COUNT(*) FROM sol_supply WHERE ts>=?")
    lines.append(f"\n⛓️ 链上大额转账 {onchain_n} 笔 · SOL 供应快照 {n_sol} 条")

    return "\n".join(lines)
