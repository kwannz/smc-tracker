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
from ..config import SmartScoreCfg
from ..signals.efficacy import wilson_interval

log = logging.getLogger("analyzer")


from ..util import to_float as _f  # 统一安全数值解析


def analyze_fills(fills: list[Any], now_ms: int) -> dict[str, Any]:
    """从 userFills 计算行为指标（纯函数，便于测试）。

    新增字段 win_rate_lower: Wilson score 95% CI 下界（样本守卫）。
    n_closed=0 时 wilson_interval(0, 0) 返回 (0.0, 1.0)，取下界 0.0，不除零。
    小样本下界大幅压缩（3 单 2 胜 ≈ 0.20），大样本逼近裸胜率（300 单 200 胜 ≈ 0.61）。
    """
    n = len(fills)
    if n == 0:
        # n_closed=0 → wilson(0, 0) = (0.0, 1.0) → 下界 0.0
        return {"n_trades": 0, "win_rate": 0.0, "win_rate_lower": 0.0,
                "n_closed": 0, "realized_pnl": 0.0, "volume_usd": 0.0,
                "taker_ratio": 0.0, "recent_24h": 0, "fav_coins": []}
    closed = [f for f in fills if f.closed_pnl != 0]
    wins = sum(1 for f in closed if f.closed_pnl > 0)
    n_closed = len(closed)
    # Wilson 下界：n_closed=0 → (0.0, 1.0)，n>0 → 标准公式（复用 signals.efficacy）
    win_rate_lo, _ = wilson_interval(wins, n_closed)
    coin_vol: Counter = Counter()
    for f in fills:
        coin_vol[f.coin] += f.notional
    return {
        "n_trades": n,
        "win_rate": wins / n_closed if n_closed else 0.0,
        "win_rate_lower": win_rate_lo,    # Wilson 95% CI 下界，用于 smart_money_score 样本守卫
        "n_closed": n_closed,
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


def smart_money_score(
    profile: dict[str, Any],
    cfg: SmartScoreCfg | None = None,
    *,
    return_caveats: bool = False,
) -> float | tuple[float, list[str]]:
    """0-100 综合评分：以盈利能力为主，胜率仅作辅助，并强化「真聪明钱」判别。

    聪明钱常低胜率高盈亏比，故胜率权重低、盈利占主导；此外加三个判别器：
    ① 跨窗一致性(周&月&全期同时为正)=持续 edge，过滤一次性运气；
    ② ROI/资本效率(近月 PnL / 账户净值)=单位本金的真实 alpha，区分「大资金碰运气」与「高手」；
    ③ 做市商/刷量判别(churn)：高成交额但单位成交方向盈亏≈0 → 非方向性 alpha → 整体打折。
    正分满分 100：全期 PnL 28 + 近月 PnL 18 + 一致性 16 + ROI 14 + 已实现盈利 8 + 账户规模 8 + 胜率 8。

    参数:
        cfg: SmartScoreCfg 权重配置；None=使用默认值（等价旧魔数，向后兼容）。
        return_caveats: True=返回 (score, caveats) 元组；False(默认)=仅返回 score。
    胜率项使用 Wilson 下界（profile["win_rate_lower"]，来自 analyze_fills）：
        小样本自动塌向 0（3 单 2 胜下界 ≈0.20），大样本逼近裸胜率（300 单 200 胜 ≈0.61）。
        若 profile 无 win_rate_lower 则回退 win_rate（向后兼容旧用例）。
    """
    if cfg is None:
        cfg = SmartScoreCfg()

    at = _f(profile.get("alltime_pnl", 0.0))
    mo = _f(profile.get("month_pnl", 0.0))
    wk = _f(profile.get("week_pnl", 0.0))
    av = _f(profile.get("account_value", 0.0))
    rp = _f(profile.get("realized_pnl", 0.0))
    vol = _f(profile.get("volume_usd", 0.0))
    # 胜率：优先用 Wilson 下界（样本守卫）；无此字段时回退裸胜率（向后兼容）
    wr_lb = _f(profile.get("win_rate_lower", profile.get("win_rate", 0.0)))
    n_closed: int = int(profile.get("n_closed", 0) or 0)

    caveats: list[str] = []

    s = 0.0
    s += min(max(at, 0) / cfg.cap_alltime, 1.0) * cfg.w_alltime       # 全期 PnL
    s += min(max(mo, 0) / cfg.cap_month, 1.0) * cfg.w_month            # 近月 PnL
    # ① 跨窗一致性：三窗皆正=持续 edge；仅月+全期正=过渡
    if at > 0 and mo > 0 and wk > 0:
        s += cfg.w_consistency_all
    elif at > 0 and mo > 0:
        s += cfg.w_consistency_part
    # ② ROI/资本效率：近月收益率(月化封顶)
    if av > 0:
        s += min(max(mo / av, 0) / cfg.cap_roi_monthly, 1.0) * cfg.w_roi
    if rp > 0:
        s += cfg.w_realized                                              # 近期已实现盈利
    s += min(av / cfg.cap_account, 1.0) * cfg.w_account                # 账户规模
    # 胜率项：用 Wilson 下界（样本守卫：小样本自动塌向 0）
    s += min(wr_lb, cfg.cap_winrate) / cfg.cap_winrate * cfg.w_winrate
    # ③ 做市商/刷量打折：成交额大但方向盈亏效率极低 → 非方向性 alpha → ×折扣
    # n_closed=0 守卫（P1修复）：建仓鲸鱼无任何平仓时 realized_pnl 自然为 0，
    # abs(0)/vol=0 本会误触发 churn 惩罚；n_closed=0 且 rp=0 时不应判刷量。
    # rp≠0 时（有已实现盈亏，无论正负）仍允许触发——此时 n_closed 必然>0。
    _is_pure_builder = n_closed == 0 and rp == 0.0
    if not _is_pure_builder and vol > cfg.churn_vol_floor and abs(rp) / vol < cfg.churn_eff_max:
        s *= cfg.churn_penalty

    score = round(min(max(s, 0.0), 100.0), 1)

    # 幸存者偏差显式标注（n_closed < min_trades_winrate 时诚实说明）
    if n_closed < cfg.min_trades_winrate and n_closed >= 0:
        caveats.append(f"⚠样本{n_closed}单(胜率下界估计)")

    if return_caveats:
        return score, caveats
    return score


class AddressAnalyzer:
    def __init__(self, store: Any | None = None,
                 cfg: SmartScoreCfg | None = None) -> None:
        self.store = store
        self._score_cfg = cfg or SmartScoreCfg()

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
        score, caveats = smart_money_score(profile, cfg=self._score_cfg, return_caveats=True)
        profile["score"] = score
        profile["score_caveats"] = caveats
        if self.store is not None and hasattr(self.store, "upsert_address_profile"):
            self.store.upsert_address_profile(profile)
        return profile

    @staticmethod
    def fmt(p: dict[str, Any], label: str = "") -> str:
        # 幸存者偏差标注：有 caveats 时追加 ⚠ 说明（CLAUDE.md 诚实标注）
        caveats: list[str] = p.get("score_caveats") or []
        caveat_str = (" " + " ".join(caveats)) if caveats else ""
        head = f"🔍 {label or p['address'][:10]} 评分={p['score']}/100{caveat_str}"
        return (f"{head}\n"
                f"   账户=${p['account_value']:,.0f} 持仓{p['n_positions']}个 "
                f"净敞口偏{p['net_bias']}(多${p['net_long_usd']:,.0f}/空${p['net_short_usd']:,.0f})\n"
                f"   全期PnL=${p['alltime_pnl']:,.0f} 近月=${p['month_pnl']:,.0f}\n"
                f"   近期{p['n_trades']}单 胜率{p['win_rate']*100:.0f}% 已实现=${p['realized_pnl']:,.0f} "
                f"吃单{p['taker_ratio']*100:.0f}% 24h={p['recent_24h']}单 偏好={','.join(p['fav_coins'])}")
