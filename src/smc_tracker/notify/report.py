"""摘要日报：从 SQLite 聚合近窗信号/背离/聪明钱净流向/链上活动，生成文本摘要。"""
from __future__ import annotations

import time
from typing import Any

from ..util import fmt_px as _fmt_px


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
        plan = (f" 入{_fmt_px(s[4])}/损{_fmt_px(s[5])}/标{_fmt_px(s[6])} RR{s[7]:.2f}" if s[4] else "")
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


def build_all_signals_report(
    store: Any,
    since_ms: int,
    now_ms: int,
    title: str = "全信号汇总",
) -> str:
    """调 collect_all_signals，按类型分组打印文本，头部带窗口/生成时间 + 免责声明。

    格式每条：「[时:分:秒] 类型 币 方向 分数 — 证据(evidence_text)」
    头部带「1h≈随机/非投资建议」免责。

    Args:
        store:     Store 实例（.conn: sqlite3.Connection）
        since_ms:  时间窗口起始 ms（含）
        now_ms:    时间窗口终止 ms（含）
        title:     报告标题（默认「全信号汇总」）

    Returns:
        格式化文本（含头部 + 各类型分组 + 每条信号明细）
    """
    from ..signals.all_signals import collect_all_signals

    gen_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ms / 1000)) if now_ms else ""
    window_min = (now_ms - since_ms) // 60_000
    lines: list[str] = [
        f"📋 {title}（近 {window_min} 分钟 · 生成于 {gen_str}）",
        "⚠️ 1h≈随机/非投资建议 — 纯算法信号，历史胜率参考诚实标注，请自担风险",
    ]

    rows = collect_all_signals(store, since_ms, now_ms)

    if not rows:
        lines.append("\n（窗口内无信号）")
        return "\n".join(lines)

    # 按 type_label 分组，保留各组内 ts DESC 顺序（collect_all_signals 已全局倒序）
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    # 保持 type_label 出现顺序（Python 3.7+ dict 有序）
    label_order: list[str] = []
    for row in rows:
        lbl = row["type_label"]
        if lbl not in groups:
            label_order.append(lbl)
        groups[lbl].append(row)

    for lbl in label_order:
        group_rows = groups[lbl]
        lines.append(f"\n【{lbl}】{len(group_rows)} 条：")
        for r in group_rows:
            ts_str = _hms(r["ts"])
            coin = r.get("coin") or "?"
            direction = r.get("direction") or "—"
            score = r.get("score")
            score_str = f" 分{score:+.2f}" if score is not None else ""
            evidence = r.get("evidence_text") or ""
            lines.append(
                f"  [{ts_str}] {lbl} {coin} {direction}{score_str} — {evidence}"
            )

    return "\n".join(lines)
