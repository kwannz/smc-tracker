"""庄 PnL 动量追踪 —— 谁现在最火 / 正在变热或变冷。

排行榜按「当前」PnL 选庄，但**正在快速盈利的庄**(策略正奏效)最该紧跟。
每轮快照各庄 PnL(日/周/月/全期)+ 账户净值，跨时间 diff：
- hot_now：按近24h(day_pnl)排「当前最火」。
- momentum：全期 PnL / 账户净值 自上次快照的变化 → 变热(加速盈利) / 变冷(回吐)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .whale_discovery import _window

log = logging.getLogger("momentum")


@dataclass(slots=True)
class MomentumEntry:
    address: str
    label: str
    pnl_change: float        # 全期 PnL 自上次快照变化
    acct_change: float       # 账户净值变化
    day_pnl: float
    hours: float             # 间隔小时

    @property
    def hot(self) -> bool:
        return self.pnl_change > 0

    def fmt(self) -> str:
        tag = "🔥变热" if self.pnl_change > 0 else "🧊变冷"
        return (f"{tag} {self.label or self.address[:10]} "
                f"{self.hours:.0f}h内 PnL{self.pnl_change:+,.0f} 账户{self.acct_change:+,.0f} "
                f"(近24h={self.day_pnl:+,.0f})")


def pnl_rows_from(leaderboard_rows: list[dict], top_n: int = 30,
                  min_account: float = 300_000.0) -> list[tuple]:
    """从已拉取的排行榜行解析 PnL（纯函数，无网络）→ [(addr,label,day,week,month,alltime,acct)]。

    抽出转换逻辑，供轮询单次拉取排行榜后与选庄排名共用，避免重复下载 16.8MB（低延迟）。
    """
    rows = []
    for r in leaderboard_rows:
        addr = r.get("ethAddress")
        if not addr:
            continue
        try:
            acct = float(r.get("accountValue", 0) or 0)
        except (TypeError, ValueError):
            continue
        if acct < min_account:
            continue
        at, _ = _window(r, "allTime")
        day, _ = _window(r, "day")
        week, _ = _window(r, "week")
        month, _ = _window(r, "month")
        rows.append((addr.lower(), "", day, week, month, at, acct))
    rows.sort(key=lambda x: x[5], reverse=True)
    return rows[:top_n]


class WhaleMomentum:
    def __init__(self, store: Any) -> None:
        self.store = store

    def snapshot(self, rows: list[tuple], now_ms: int) -> None:
        for addr, label, day, week, month, at, acct in rows:
            self.store.insert_whale_pnl((addr, label, day, week, month, at, acct, now_ms))

    def momentum(self, rows: list[tuple], now_ms: int, window_ms: int = 3_600_000,
                 min_change: float = 100_000.0) -> list[MomentumEntry]:
        """对本轮 rows，与 window 前的快照比对，返回变化超阈值的庄（按 |PnL变化| 降序）。"""
        out: list[MomentumEntry] = []
        for addr, label, day, week, month, at, acct in rows:
            prev = self.store.whale_pnl_before(addr, now_ms - window_ms)
            if not prev:
                continue
            _, prev_at, prev_acct, prev_ts = prev
            d_pnl = at - (prev_at or 0)
            d_acct = acct - (prev_acct or 0)
            if abs(d_pnl) < min_change and abs(d_acct) < min_change:
                continue
            out.append(MomentumEntry(addr, label, d_pnl, d_acct, day,
                                     (now_ms - prev_ts) / 3_600_000))
        out.sort(key=lambda e: abs(e.pnl_change), reverse=True)
        return out

    @staticmethod
    def hot_now(rows: list[tuple], limit: int = 10) -> list[tuple]:
        """按近24h PnL(day) 排当前最火的庄。"""
        return sorted(rows, key=lambda x: x[2], reverse=True)[:limit]
