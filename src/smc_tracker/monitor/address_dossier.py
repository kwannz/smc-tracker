"""完整地址档案：把系统对单个地址的所有维度信息汇总成一份可读档案。

聚合（一条命令看清一个地址全貌）：
  ① 聪明钱画像（评分/胜率/全期·近月·近周 PnL/做市判别/perp_active/偏好币）
  ② 实时逐币持仓（方向/名义/入场/杠杆/未实现盈亏/强平价，完整明细）
  ③ 协同地址 co-movers（同窗同向→庄家集团线索）
  ④ 频繁对手方（疑似关联钱包/自成交）
  ⑤ 近期成交轨迹时间线
  ⑥ PnL 快照 + 可疑标记

build_dossier 异步组装（需 HyperliquidInfo 拉实时持仓）；fmt_dossier 纯渲染（可单测）。
被 CLI `address` 子命令调用——「追踪地址完整信息 + 完整分析」的统一出口。
"""
from __future__ import annotations

from typing import Any

from ..models import Side
from ..util import fmt_hms, fmt_px, to_float
from .address_analyzer import AddressAnalyzer
from .address_correlation import AddressCorrelation
from .trader_classify import classify_trader, fmt_classify


async def build_dossier(address: str, info: Any, store: Any, now_ms: int, *,
                        lb_row: dict | None = None, window_h: float = 24.0) -> dict:
    """组装单个地址的完整档案 dict（画像 + 实时持仓 + 协同/对手方 + 轨迹 + PnL + 标记）。"""
    addr = address.lower()
    since = now_ms - int(window_h * 3_600_000)

    # 预取实时成交（一次，供画像 + 成交明细共用，避免重复拉大 payload）
    try:
        raw_fills = await info.user_fills(address)
    except Exception:  # noqa: BLE001
        raw_fills = []

    # ① 聪明钱画像（传入预取 fills 去重）
    profile = await AddressAnalyzer(store).analyze(address, info, now_ms, lb_row,
                                                   fills=raw_fills)

    # ①b 实时全币种成交明细（开/平/加/减语义 + 每笔盈亏 + 主被动，最近优先）
    recent_fills = [
        {"coin": f.coin, "side": "买" if f.side is Side.BUY else "卖",
         "dir": f.dir, "sz": f.sz, "px": f.px, "notional": f.notional,
         "pnl": f.closed_pnl, "taker": f.crossed, "time_ms": f.time_ms}
        for f in sorted(raw_fills, key=lambda x: x.time_ms, reverse=True)[:20]
    ]

    # ② 实时逐币持仓（完整明细，按 |名义| 降序）
    try:
        raw_pos = await info.positions(address)
    except Exception:  # noqa: BLE001 — 拉持仓失败不致整份档案失败
        raw_pos = []
    positions = sorted(
        ({"coin": p.coin, "side": "多" if p.is_long else "空", "szi": p.szi,
          "value": p.position_value, "entry": p.entry_px,
          "upnl": p.unrealized_pnl, "lev": p.leverage, "liq": p.liquidation_px}
         for p in raw_pos),
        key=lambda x: abs(to_float(x["value"])), reverse=True)

    # ③④ 协同地址 + 频繁对手方（本地 DB，无网络）
    corr = AddressCorrelation(store)
    try:
        co_movers = corr.correlated_with(addr, since, min_shared=2, limit=8)
    except Exception:  # noqa: BLE001
        co_movers = []
    try:
        cps = [(b if a == addr else a, c)
               for a, b, c in corr.counterparties(since, min_count=2, limit=300)
               if addr in (a, b)]
        cps.sort(key=lambda t: t[1], reverse=True)
        counterparties = cps[:8]
    except Exception:  # noqa: BLE001
        counterparties = []

    # ④b 所属庄家集团（跨币协同群，关联钱包的硬证据：min_coins≥2）
    try:
        clusters = corr.clusters_detailed(since, window_sec=120, min_shared=2, min_coins=2)
        my_cluster = next((c for c in clusters if addr in c.get("members", [])), None)
    except Exception:  # noqa: BLE001
        my_cluster = None

    # ⑤ 近期成交轨迹
    try:
        trajectory = store.address_trajectory(addr, since_ms=since, limit=15)
    except Exception:  # noqa: BLE001
        trajectory = []

    # ⑥ PnL 快照 + 可疑标记
    try:
        pnl_snapshot = store.whale_pnl_latest(addr)
    except Exception:  # noqa: BLE001
        pnl_snapshot = None
    try:
        flagged = store.is_flagged(addr)
    except Exception:  # noqa: BLE001
        flagged = False

    # ⑦ avg_hold_sec：从已平仓 fills 估算平均持仓时长（P0修复：原先缺此字段导致 whale 判定永久失效）
    # 算法：取所有已平仓 fill（closed_pnl≠0）的时间戳，用 [max-min]/n_closed 估算生命周期；
    # 样本过少(<2)时回退0，不抛。
    avg_hold_sec: float = 0.0
    try:
        closed_fills = [f for f in raw_fills if f.closed_pnl != 0]
        n_closed = len(closed_fills)
        if n_closed >= 2:
            ts_list = [f.time_ms for f in closed_fills]
            span_ms = max(ts_list) - min(ts_list)
            avg_hold_sec = to_float(span_ms / n_closed / 1000.0)  # 转换为秒，拒 NaN/inf
    except Exception:  # noqa: BLE001
        avg_hold_sec = 0.0

    return {
        "address": addr, "now_ms": now_ms, "window_h": window_h,
        "profile": profile, "positions": positions, "recent_fills": recent_fills,
        "co_movers": co_movers, "counterparties": counterparties,
        "cluster": my_cluster,
        "trajectory": trajectory, "pnl_snapshot": pnl_snapshot, "flagged": flagged,
        "avg_hold_sec": avg_hold_sec,   # 平均持仓时长(秒)，供 classify_trader 判定庄家
    }


def fmt_dossier(d: dict) -> str:
    """把 build_dossier 的 dict 渲染成中文完整档案文本。"""
    p = d.get("profile", {})
    lines = [f"📇 地址完整档案 {d.get('address', '')}  (近 {d.get('window_h', 24):g}h)"]
    if d.get("flagged"):
        lines.append("  🚩 已标记为可疑地址（动态升级追踪中）")

    # ① 画像
    pa = "" if p.get("perp_active", True) else "  ⚠️ 无永续活动·疑纯现货/休眠"
    lines.append(f"【画像】评分 {p.get('score', 0):.0f}/100{pa}")
    lines.append(
        f"  账户净值 ${p.get('account_value', 0):,.0f} · 总持仓名义 ${p.get('total_notional', 0):,.0f}"
        f" · 净敞口偏{p.get('net_bias', '?')}"
        f"(多${p.get('net_long_usd', 0):,.0f}/空${p.get('net_short_usd', 0):,.0f})")
    lines.append(
        f"  全期PnL ${p.get('alltime_pnl', 0):,.0f} · 近月 ${p.get('month_pnl', 0):,.0f}"
        f" · 近周 ${p.get('week_pnl', 0):,.0f}")
    lines.append(
        f"  近期 {p.get('n_trades', 0)}单(24h {p.get('recent_24h', 0)}单) · 胜率 {p.get('win_rate', 0) * 100:.0f}%"
        f" · 已实现 ${p.get('realized_pnl', 0):,.0f} · 成交额 ${p.get('volume_usd', 0):,.0f}"
        f" · 吃单 {p.get('taker_ratio', 0) * 100:.0f}%")
    if p.get("fav_coins"):
        lines.append(f"  偏好币种: {', '.join(p['fav_coins'])}")

    # ① 庄家 vs 游资分类（基于行为画像启发式规则）
    # avg_hold_sec：用 dossier 中位置生命周期估算（此处从 profile 无直接字段，用简单启发）
    # n_trades 近期成交；avg_hold_sec 若档案含 lifecycle 则用，否则从近期成交密度估算
    _avg_hold_sec = to_float(d.get("avg_hold_sec", 0.0))
    _clf = classify_trader(
        account_value=to_float(p.get("account_value", 0.0)),
        avg_hold_sec=_avg_hold_sec,
        n_trades=int(to_float(p.get("n_trades", 0))),
        win_rate=to_float(p.get("win_rate", 0.0)),
    )
    lines.append(f"  资金类型: {fmt_classify(_clf)}  ({_clf['reason']})")

    # ② 实时持仓
    pos = d.get("positions", [])
    lines.append(f"【实时持仓 {len(pos)} 个】" + ("（按净名义 Top）" if pos else "（当前空仓）"))
    for x in pos[:12]:
        liq = f" 强平≈{fmt_px(x['liq'])}" if x.get("liq") else ""
        lines.append(
            f"  {x['coin']:<10} {x['side']} ${abs(to_float(x['value'])):,.0f} "
            f"@{fmt_px(x['entry'])} {to_float(x['lev']):.0f}x "
            f"uPnL${to_float(x['upnl']):+,.0f}{liq}")

    # ②b 实时全币种成交明细（开/平/加/减 + 每笔盈亏）
    rf = d.get("recent_fills", [])
    if rf:
        lines.append(f"【实时成交明细 近{len(rf)}笔(全币种)】")
        for f in rf[:15]:
            pnl = to_float(f.get("pnl"))
            pnl_s = f" 平盈亏${pnl:+,.0f}" if pnl else ""
            tk = "主动" if f.get("taker") else "被动"
            lines.append(
                f"  [{fmt_hms(int(f['time_ms']))}] {f['coin']:<8} {f.get('dir', '')}"
                f" {f['side']} {fmt_px(f['sz'])}@{fmt_px(f['px'])}"
                f" ${to_float(f['notional']):,.0f} {tk}{pnl_s}")

    # ③ 协同地址
    if d.get("co_movers"):
        lines.append("【协同地址(同窗同向→庄家集团线索)】")
        for a, c in d["co_movers"]:
            lines.append(f"  {a[:12]}… ×{c}")

    # ④ 频繁对手方
    if d.get("counterparties"):
        lines.append("【频繁对手方(疑似关联钱包/自成交)】")
        for a, c in d["counterparties"]:
            lines.append(f"  {a[:12]}… ×{c}")

    # ④b 所属庄家集团（关联钱包群）
    cl = d.get("cluster")
    if cl:
        members = cl.get("members", [])
        lines.append(f"【所属庄家集团】{len(members)}个钱包 · 跨{cl.get('coins', '?')}币 · "
                     f"协同{cl.get('events', '?')}次")
        for m in members[:10]:
            tag = " ←本地址" if m == d.get("address") else ""
            lines.append(f"  {m[:12]}…{tag}")

    # ⑤ 近期成交轨迹
    traj = d.get("trajectory", [])
    if traj:
        lines.append(f"【近期成交轨迹 {len(traj)} 笔】")
        for t in traj[:10]:
            tm, coin, side, notional, px, taker = t
            tk = "主动" if taker else "被动"
            lines.append(
                f"  [{fmt_hms(int(tm))}] {coin} {side} ${to_float(notional):,.0f}"
                f" @{fmt_px(px)} {tk}")

    if not pos and not traj and not d.get("co_movers"):
        lines.append("（该地址近窗无永续持仓/成交/协同记录——可能纯现货、休眠或排行榜聚合账户）")

    return "\n".join(lines)
