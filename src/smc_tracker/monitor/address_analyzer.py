"""聪明钱地址深度分析 / 画像。

对任一 Hyperliquid 地址，综合：
- 排行榜表现(全期/月/周 PnL、ROI、账户净值)；
- 当前持仓(clearinghouseState：净敞口、杠杆、多空)；
- 近期成交(userFills：胜率、已实现盈亏、交易频率、偏好币、吃单比、近24h活跃)。
产出画像 dict + 聪明钱评分(0-100)，落库 address_profiles。
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from ..hyperliquid import HyperliquidInfo
from ..models import Side

log = logging.getLogger("analyzer")


from ..util import to_float as _f  # 统一安全数值解析


def analyze_fills(fills: list[Any], now_ms: int) -> dict[str, Any]:
    """从 userFills 计算行为指标（纯函数，便于测试）。"""
    n = len(fills)
    if n == 0:
        return {"n_trades": 0, "win_rate": 0.0, "realized_pnl": 0.0, "volume_usd": 0.0,
                "taker_ratio": 0.0, "recent_24h": 0, "fav_coins": []}
    closed = [f for f in fills if f.closed_pnl != 0]
    wins = sum(1 for f in closed if f.closed_pnl > 0)
    coin_vol: Counter = Counter()
    for f in fills:
        coin_vol[f.coin] += f.notional
    return {
        "n_trades": n,
        "win_rate": wins / len(closed) if closed else 0.0,
        "n_closed": len(closed),
        "realized_pnl": sum(f.closed_pnl for f in fills),
        "volume_usd": sum(f.notional for f in fills),
        "taker_ratio": sum(1 for f in fills if f.crossed) / n,
        "recent_24h": sum(1 for f in fills if f.time_ms >= now_ms - 86_400_000),
        "fav_coins": [c for c, _ in coin_vol.most_common(3)],
    }


def is_perp_active(n_positions: int, n_trades: int) -> bool:
    """地址是否有「可追踪的永续活动」（当前持仓或近期成交）。

    排行榜按 spot+perp 聚合 PnL 排名(见 PLAN 关键经验)，纯现货/休眠巨鲸会被选为「庄」，
    但按地址查永续 userFills/clearinghouseState 全空(0持仓0成交) → 无法做地址级永续追踪。
    据此诚实区分「无永续可追(疑纯现货)」与「已分析但低质(评分0)」，避免画像误导。
    """
    return n_positions > 0 or n_trades > 0


def smart_money_score(profile: dict[str, Any]) -> float:
    """0-100 综合评分：以盈利能力为主，胜率仅作辅助，并强化「真聪明钱」判别。

    聪明钱常低胜率高盈亏比，故胜率权重低、盈利占主导；此外加三个判别器：
    ① 跨窗一致性(周&月&全期同时为正)=持续 edge，过滤一次性运气；
    ② ROI/资本效率(近月 PnL / 账户净值)=单位本金的真实 alpha，区分「大资金碰运气」与「高手」；
    ③ 做市商/刷量判别(churn)：高成交额但单位成交方向盈亏≈0 → 非方向性 alpha → 整体打折。
    正分满分 100：全期 PnL 28 + 近月 PnL 18 + 一致性 16 + ROI 14 + 已实现盈利 8 + 账户规模 8 + 胜率 8。
    """
    at = profile.get("alltime_pnl", 0.0)
    mo = profile.get("month_pnl", 0.0)
    wk = profile.get("week_pnl", 0.0)
    av = profile.get("account_value", 0.0)
    rp = profile.get("realized_pnl", 0.0)
    vol = profile.get("volume_usd", 0.0)
    wr = profile.get("win_rate", 0.0)

    s = 0.0
    s += min(max(at, 0) / 50_000_000, 1.0) * 28       # 全期 PnL(5000万封顶) 28
    s += min(max(mo, 0) / 10_000_000, 1.0) * 18       # 近月 PnL(1000万封顶) 18
    # ① 跨窗一致性：三窗皆正=持续 edge 16；仅月+全期正=过渡 7
    if at > 0 and mo > 0 and wk > 0:
        s += 16
    elif at > 0 and mo > 0:
        s += 7
    # ② ROI/资本效率：近月收益率(月化 50% 封顶) 14
    if av > 0:
        s += min(max(mo / av, 0) / 0.5, 1.0) * 14
    if rp > 0:
        s += 8                                        # ③前置：近期已实现盈利 8
    s += min(av / 10_000_000, 1.0) * 8                # 账户规模(1000万封顶) 8
    s += min(wr, 0.7) / 0.7 * 8                       # 胜率(<=70%封顶) 辅助 8
    # ③ 做市商/刷量打折：成交额大但方向盈亏效率极低(<0.1%) → 非 alpha → ×0.85
    if vol > 1_000_000 and abs(rp) / vol < 0.001:
        s *= 0.85
    return round(min(max(s, 0.0), 100.0), 1)


class AddressAnalyzer:
    def __init__(self, store: Any | None = None) -> None:
        self.store = store

    async def analyze(self, address: str, info: HyperliquidInfo, now_ms: int,
                      lb_row: dict[str, Any] | None = None,
                      fills: list[Any] | None = None) -> dict[str, Any]:
        # 1) 持仓 + 账户
        state = await info.clearinghouse_state(address)
        ms = state.get("marginSummary", {})
        positions = await info.positions(address)
        net_long = sum(p.position_value for p in positions if p.is_long)
        net_short = sum(p.position_value for p in positions if not p.is_long)
        # 2) 成交行为（fills 可由调用方预取传入，避免重复拉取大 payload）
        if fills is None:
            fills = await info.user_fills(address)
        beh = analyze_fills(fills, now_ms)
        # 3) 排行榜表现
        lb = lb_row or {}
        def win(name: str) -> float:
            for w in lb.get("windowPerformances", []):
                if w and len(w) >= 2 and w[0] == name and isinstance(w[1], dict):
                    return _f(w[1].get("pnl"))
            return 0.0

        profile = {
            "address": address.lower(),
            "account_value": _f(ms.get("accountValue")),
            "total_notional": _f(ms.get("totalNtlPos")),
            "n_positions": len(positions),
            "net_long_usd": net_long, "net_short_usd": net_short,
            "net_bias": "多" if net_long > net_short else "空",
            "alltime_pnl": win("allTime"), "month_pnl": win("month"), "week_pnl": win("week"),
            **beh,
            # 永续可追踪性：纯现货/休眠巨鲸 0 持仓 0 成交 → False（诚实标注，非真低质）
            "perp_active": is_perp_active(len(positions), beh.get("n_trades", 0)),
            "ts": now_ms,
        }
        profile["score"] = smart_money_score(profile)
        if self.store is not None and hasattr(self.store, "upsert_address_profile"):
            self.store.upsert_address_profile(profile)
        return profile

    @staticmethod
    def fmt(p: dict[str, Any], label: str = "") -> str:
        head = f"🔍 {label or p['address'][:10]} 评分={p['score']}/100"
        return (f"{head}\n"
                f"   账户=${p['account_value']:,.0f} 持仓{p['n_positions']}个 "
                f"净敞口偏{p['net_bias']}(多${p['net_long_usd']:,.0f}/空${p['net_short_usd']:,.0f})\n"
                f"   全期PnL=${p['alltime_pnl']:,.0f} 近月=${p['month_pnl']:,.0f}\n"
                f"   近期{p['n_trades']}单 胜率{p['win_rate']*100:.0f}% 已实现=${p['realized_pnl']:,.0f} "
                f"吃单{p['taker_ratio']*100:.0f}% 24h={p['recent_24h']}单 偏好={','.join(p['fav_coins'])}")
