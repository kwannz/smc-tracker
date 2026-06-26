#!/usr/bin/env python3
"""审计系统**根本命题**:smart_money_score 识别的"聪明钱"是技巧持续,还是运气?

CLAUDE.md §一-3(先实证)+§二:smart_money_score/address_correlation 被称"系统主体与可验证基石",
但可信度地图从无其前瞻性数字——everything 抓庄都建在"过去盈利=未来仍盈利(技巧持续)"这一未验证假设上。
若像 KNN≈随机(过去 PnL 不预测未来),则"发现庄→跟庄"是空中楼阁,须诚实标注。

方法:**业绩持续性(split-half,基金持续性研究标准)**——每地址 fills 按时间切**非重叠**两半,
算 **size-independent 技巧度量**(胜率 / 每笔 notional 收益率),跨地址 corr(早半技巧, 晚半技巧)。
  skill 持续 ⇒ 早预测晚 ⇒ 筛选有效;corr≈null(打乱配对)⇒ 运气,过去不预测未来。
关键纪律(#149/#177):
  ① 必须用胜率/效率(非**原始 PnL**——后者大账户两半都大=被账户规模机械相关污染,假性"持续");
  ② null 对照(打乱早晚配对)证伪机械相关;③ 同时报原始 PnL corr 作"陷阱对照"暴露污染。

用法:PYTHONPATH=src ./.venv/bin/python scripts/audit_smart_money_persistence.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.hyperliquid.info_client import HyperliquidInfo  # noqa: E402
from smc_tracker.monitor.whale_discovery import fetch_leaderboard_rows  # noqa: E402

_MIN_CLOSED = 30        # 每地址最少平仓笔数(够切两半各≥15)
_SAMPLE = 80            # 采样地址数(跨排名分布,非只 top)
_MIN_ACCT = 100_000.0   # 账户净值下限(活跃交易者)
_CONC = 3               # 并发取 fills(HL info 限流敏感,低并发+延时更可复现)
_DELAY = 0.2            # 每笔取 fills 前小延时(避 429)


def _rank(a: np.ndarray) -> np.ndarray:
    """秩(平均秩处理并列简化为 argsort 秩;Spearman 用)。"""
    order = a.argsort()
    r = np.empty_like(order, dtype=float)
    r[order] = np.arange(len(a))
    return r


def _corr(x: list, y: list) -> float:
    if len(x) < 5:
        return float("nan")
    return float(np.corrcoef(np.array(x), np.array(y))[0, 1])


def _split_metrics(fills: list) -> tuple | None:
    """按时间切非重叠两半,各算 (胜率, 每笔notional收益率, 原始PnL)。不足 _MIN_CLOSED → None。"""
    closed = sorted((f for f in fills if f.closed_pnl != 0), key=lambda f: f.time_ms)
    if len(closed) < _MIN_CLOSED:
        return None
    mid = len(closed) // 2
    early, late = closed[:mid], closed[mid:]

    def metr(half):
        wins = sum(1 for f in half if f.closed_pnl > 0)
        wr = wins / len(half)
        effs = [f.closed_pnl / f.notional for f in half if f.notional > 0]
        eff = float(np.mean(effs)) if effs else 0.0
        pnl = sum(f.closed_pnl for f in half)
        return wr, eff, pnl

    return metr(early), metr(late)


async def _fetch_all(addrs: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    sem = asyncio.Semaphore(_CONC)
    async with HyperliquidInfo() as cli:
        async def one(a: str):
            async with sem:
                try:
                    await asyncio.sleep(_DELAY)
                    fills = await cli.user_fills(a)
                    if fills:
                        out[a] = fills
                except Exception as e:  # noqa: BLE001
                    print(f"  跳过 {a[:10]}: {e}")
        await asyncio.gather(*(one(a) for a in addrs))
    return out


async def main() -> None:
    print("取 Hyperliquid 排行榜...")
    rows = await fetch_leaderboard_rows()
    cand = []
    for r in rows:
        a = r.get("ethAddress")
        try:
            av = float(r.get("accountValue", 0) or 0)
        except (TypeError, ValueError):
            continue
        if a and av >= _MIN_ACCT:
            cand.append(a)
    # 跨排名分布均匀采样(非只 top,避免量程限制)
    step = max(1, len(cand) // _SAMPLE)
    sample = cand[::step][:_SAMPLE]
    print(f"排行榜 {len(rows)} 行 → 活跃候选 {len(cand)} → 采样 {len(sample)} 地址取 fills...")

    fills_map = await _fetch_all(sample)
    print(f"成功取 {len(fills_map)} 地址 fills\n")

    e_wr, l_wr, e_eff, l_eff, e_pnl, l_pnl = [], [], [], [], [], []
    for a, fills in fills_map.items():
        m = _split_metrics(fills)
        if m is None:
            continue
        (ewr, eeff, epnl), (lwr, leff, lpnl) = m
        e_wr.append(ewr); l_wr.append(lwr)
        e_eff.append(eeff); l_eff.append(leff)
        e_pnl.append(epnl); l_pnl.append(lpnl)

    n = len(e_wr)
    print("=" * 66)
    print(f"业绩持续性审计:{n} 地址(各≥{_MIN_CLOSED}平仓笔,fills 按时间切非重叠两半)")
    if n < 5:
        print("有效地址不足(<5),无法统计。可能 fills 深度不够或数据受限。")
        return
    # null:打乱晚半配对(销毁真实持续、保留各自分布)
    rng = np.random.default_rng(42)

    def null_corr(x, y, k=200):
        ya = np.array(y)
        return float(np.mean([np.corrcoef(x, rng.permutation(ya))[0, 1] for _ in range(k)]))

    wr_c, eff_c, pnl_c = _corr(e_wr, l_wr), _corr(e_eff, l_eff), _corr(e_pnl, l_pnl)
    wr_s = _corr(list(_rank(np.array(e_wr))), list(_rank(np.array(l_wr))))   # Spearman
    print("  度量(早半→晚半)        Pearson   null(打乱)   真实增益")
    print(f"  胜率(技巧,size无关)    {wr_c:+.3f}    {null_corr(e_wr, l_wr):+.3f}      "
          f"{wr_c - null_corr(e_wr, l_wr):+.3f}")
    print(f"  每笔效率(size无关)     {eff_c:+.3f}    {null_corr(e_eff, l_eff):+.3f}      "
          f"{eff_c - null_corr(e_eff, l_eff):+.3f}")
    print(f"  胜率 Spearman(秩)      {wr_s:+.3f}")
    print(f"  原始PnL(⚠陷阱对照,被账户规模污染) {pnl_c:+.3f}    {null_corr(e_pnl, l_pnl):+.3f}")
    print("-" * 66)
    real = max(wr_c - null_corr(e_wr, l_wr), eff_c - null_corr(e_eff, l_eff))
    if real < 0.12:
        print("结论:技巧度量(胜率/效率)早→晚持续性≈null ⇒ **过去盈利不预测未来盈利**,")
        print("      smart_money_score 的'技巧持续'前提存疑(类 KNN≈随机);'发现庄→跟庄'须诚实标注此局限。")
    else:
        print("结论:技巧度量早→晚持续性显著超 null ⇒ **盈利能力有真实持续(技巧非纯运气)**,")
        print(f"      smart_money_score 筛选前提成立(胜率持续 corr≈{wr_c:.2f});系统根本命题获实证支持。")
    print("=" * 66)


if __name__ == "__main__":
    asyncio.run(main())
