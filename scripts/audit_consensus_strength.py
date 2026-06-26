#!/usr/bin/env python3
"""审计第二基石算法:多庄**共识**(同币同向聚集)真比单庄信号更强吗?(续 #185-186)

CLAUDE.md §二:`address_correlation`/`WhaleConsensus` 与 smart_money_score 并列"可验证基石",
赌"多个聪明钱押同方向=放大信号"——但此假设从未实证(类'压缩→突破'/'加速度领先'之直觉,须自证)。

方法:庄 Open 入场按"同币同向 4h 窗内**不同庄数**"分共识度(1单庄 / ≥2共识)→比较扣币种漂移后前瞻 alpha。
  共识组 alpha 显著>单庄 ⇒ 放大假设成立、共识信号有据;持平 ⇒ 共识仅重复计数不加信息,须诚实标注。
纪律(#185-186):扣漂移基线分 alpha/beta;同向不同庄去重(同一庄多笔不算共识)。

用法:PYTHONPATH=src ./.venv/bin/python scripts/audit_consensus_strength.py
"""
from __future__ import annotations

import asyncio
import bisect
import sys
import time as _t
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.hyperliquid.info_client import HyperliquidInfo  # noqa: E402
from smc_tracker.monitor.whale_discovery import fetch_leaderboard_rows  # noqa: E402

_SAMPLE_ADDR = 80
_MIN_ACCT = 300_000.0
_LOOKBACK_D = 30
_BAR_MS = 900_000
_WINDOW_MS = 4 * 3_600_000      # 共识窗口 4h
_HZ = {"4h": 16, "24h": 96}     # 前瞻视野(15m bar)
_TOP_COINS = 20
_CONC = 3
_DELAY = 0.2


async def _fetch_fills(addrs):
    out = {}
    sem = asyncio.Semaphore(_CONC)
    async with HyperliquidInfo() as cli:
        async def one(a):
            async with sem:
                try:
                    await asyncio.sleep(_DELAY)
                    f = await cli.user_fills(a)
                    if f:
                        out[a] = f
                except Exception as e:  # noqa: BLE001
                    print(f"  跳过 {a[:10]}: {e}")
        await asyncio.gather(*(one(a) for a in addrs))
    return out


async def _fetch_candles(coins, start, end):
    px = {}
    async with HyperliquidInfo() as cli:
        for c in coins:
            try:
                cs = await cli.candle_snapshot(c, "15m", start, end)
                if len(cs) >= 100:
                    px[c] = (np.array([k.open_time_ms for k in cs]),
                             np.array([k.c for k in cs], dtype=float))
                await asyncio.sleep(_DELAY)
            except Exception as e:  # noqa: BLE001
                print(f"  K线跳过 {c}: {e}")
    return px


def _price_at(arr, t):
    ts, ps = arr
    i = bisect.bisect_right(ts, t) - 1
    return float(ps[i]) if 0 <= i < len(ps) else 0.0


def _drift(arr, h):
    _, ps = arr
    return float(np.mean(ps[h:] / ps[:-h] - 1.0)) if ps.size > h else 0.0


async def main():
    print("取排行榜...")
    rows = await fetch_leaderboard_rows()
    cand = [r["ethAddress"] for r in rows
            if r.get("ethAddress") and float(r.get("accountValue", 0) or 0) >= _MIN_ACCT]
    sample = cand[::max(1, len(cand) // _SAMPLE_ADDR)][:_SAMPLE_ADDR]
    print(f"采样 {len(sample)} 庄取 fills...")
    fmap = await _fetch_fills(sample)

    now = int(_t.time() * 1000)
    cutoff = now - _LOOKBACK_D * 86_400_000
    # (coin,is_long) -> list[(time_ms, addr)]
    groups = defaultdict(list)
    ccount = Counter()
    for addr, fills in fmap.items():
        for f in fills:
            if f.dir.startswith("Open") and f.time_ms >= cutoff:
                groups[(f.coin, f.dir.endswith("Long"))].append((f.time_ms, addr))
                ccount[f.coin] += 1
    coins = [c for c, _ in ccount.most_common(_TOP_COINS)]
    print(f"庄入场分 {len(groups)} 组,取 top{len(coins)} 币 HL K线...")
    px = await _fetch_candles(coins, cutoff - _BAR_MS, now)

    # 每入场算共识度(同币同向 4h 窗内不同庄数)+ 前瞻 alpha(扣漂移)
    drift = {c: {h: _drift(px[c], n) for h, n in _HZ.items()} for c in px}
    # 不相交桶(避免重叠计数掩盖非单调性):=1 / =2 / ≥3
    buckets = {"单庄(=1)": {h: [] for h in _HZ}, "双庄(=2)": {h: [] for h in _HZ},
               "多庄(≥3)": {h: [] for h in _HZ}}
    percoin = defaultdict(lambda: {"solo": {h: [] for h in _HZ}, "cons": {h: [] for h in _HZ}})
    for (coin, is_long), evs in groups.items():
        if coin not in px:
            continue
        evs.sort()
        times = [t for t, _ in evs]
        for i, (tm, addr) in enumerate(evs):
            # 窗内 [tm-W, tm] 不同庄数
            lo = bisect.bisect_left(times, tm - _WINDOW_MS)
            distinct = {evs[j][1] for j in range(lo, i + 1)}
            deg = len(distinct)
            p0 = _price_at(px[coin], tm)
            if p0 <= 0:
                continue
            for hk, hb in _HZ.items():
                p1 = _price_at(px[coin], tm + hb * _BAR_MS)
                if p1 <= 0:
                    continue
                r = p1 / p0 - 1.0
                alpha = ((r if is_long else -r) -
                         (drift[coin][hk] if is_long else -drift[coin][hk])) * 100.0
                bk = "单庄(=1)" if deg == 1 else ("双庄(=2)" if deg == 2 else "多庄(≥3)")
                buckets[bk][hk].append(alpha)
                # 币内配对(消除币种选择混淆,#188教训):同币内 solo(=1) vs cons(≥2)
                percoin[coin]["solo" if deg == 1 else "cons"][hk].append(alpha)

    # 币内配对差:每币(cons_mean − solo_mean),跨币等权聚合→不被单币主导(#188核心修复)
    print("-" * 70)
    print("【币内配对(消除币种选择混淆,#188核心修复)=决定性估计量】每币 cons(≥2)−solo(=1) alpha 差,跨币等权")
    wc = {}
    for hk in _HZ:
        diffs = []
        for coin, d in percoin.items():
            so, co = d["solo"][hk], d["cons"][hk]
            if len(so) >= 8 and len(co) >= 5:           # 该币两组都够样本
                diffs.append(float(np.mean(co)) - float(np.mean(so)))
        if len(diffs) >= 4:
            da = np.array(diffs)
            pos = int((da > 0).sum())
            wc[hk] = (float(da.mean()), float(np.median(da)), pos, len(diffs))
            print(f"  {hk}: {len(diffs)}币  均差 {da.mean():+.3f}pp / 中位 {np.median(da):+.3f}pp / "
                  f"{pos}/{len(diffs)}币为正")
        else:
            print(f"  {hk}: 合格币不足({len(diffs)})——共识事件太稀疏,无法币内配对")
    # 决定性结论(币内配对优先于池化桶——后者被币种选择污染)
    w = wc.get("24h")
    if w:
        mean_d, med_d, pos, ncoin = w
        coinflip = abs(med_d) < 0.5 and 0.35 <= pos / ncoin <= 0.65
        if coinflip:
            print(f"  ★决定性:币内配对 24h 中位差 {med_d:+.2f}pp、{pos}/{ncoin}币为正(≈掷硬币)⇒"
                  "**共识放大≈0(了结#188悬案)**;每庄入场仍有#186领先性,但共识不额外放大,勿按庄数加权当更强。")
        elif med_d > 0.5 and pos / ncoin > 0.65:
            print(f"  ★决定性:币内配对 24h 中位差 {med_d:+.2f}pp、{pos}/{ncoin}币为正⇒共识确放大(币内稳健)。")
        else:
            print(f"  ★币内配对 24h 中位 {med_d:+.2f}pp、{pos}/{ncoin}币为正——方向弱/混合,仍需更多币。")

    rng = np.random.default_rng(7)

    def boot_ci(a, b, k=3000):
        """bootstrap 90% CI of mean(a)−mean(b)。CI 排除 0 = 统计可区分。"""
        a, b = np.array(a), np.array(b)
        d = [rng.choice(a, a.size).mean() - rng.choice(b, b.size).mean() for _ in range(k)]
        return float(np.percentile(d, 5)), float(np.percentile(d, 95))

    print("=" * 70)
    print("共识强度审计:前瞻 alpha(扣币种漂移,%)按共识度(同币同向 4h 窗内不同庄数,**不相交桶**)")
    print("  抗噪:均值(离群敏感) vs **中位数(稳健)** —— 中位也非单调才是真,只均值尖叫=离群幻觉")
    print(f"  {'共识度':<12}" + "".join(f"{h+'均/中位':>20}" for h in _HZ))
    md = {}
    for bk in ("单庄(=1)", "双庄(=2)", "多庄(≥3)"):
        cells = []
        for hk in _HZ:
            v = buckets[bk][hk]
            if len(v) > 15:
                md[(bk, hk)] = (float(np.mean(v)), float(np.median(v)), v)
                cells.append(f"{np.mean(v):+.2f}/{np.median(v):+.2f}(n{len(v)})")
            else:
                cells.append("—")
        print(f"  {bk:<12}" + "".join(f"{c:>20}" for c in cells))
    print("-" * 70)
    # 严格拷问 24h:双庄 vs 单庄的 bootstrap CI + 中位数是否也非单调
    g1 = md.get(("单庄(=1)", "24h")); g2 = md.get(("双庄(=2)", "24h")); g3 = md.get(("多庄(≥3)", "24h"))
    if g1 and g2:
        lo, hi = boot_ci(g2[2], g1[2])
        med_nonmono = g2[1] > g1[1] + 0.05 and (g3 is None or g3[1] < g2[1] - 0.05)
        excl0 = lo > 0 or hi < 0   # CI 排除 0(任一侧)
        side = "正向(双庄更强)" if lo > 0 else ("负向(双庄更弱)" if hi < 0 else "跨0")
        print(f"  双庄−单庄(24h) bootstrap 90%CI = [{lo:+.2f}, {hi:+.2f}]pp  ({side})")
        print(f"  中位数非单调(双庄>单庄且>≥3)? {'是' if med_nonmono else '否'}")
        # ⚠跨运行不稳定>单次CI:共识事件集中少数币,哪些币(candle)加载主导结果——#188 实测 +7.1%↔−6% 符号翻转
        if lo > 0 and med_nonmono:
            print("  结论:双庄>单庄(正向显著)且中位也非单调——但⚠须跨运行复核(共识alpha对币种选择极敏感,单次CI不足)。")
        else:
            print("  结论:**未确立共识放大**(正向不显著/中位不支持)。⚠核心限制:共识事件集中少数币,")
            print("        前瞻alpha由'哪些币candle加载'主导→跨运行极不稳定(#188:+7.1%↔−6%符号翻转),此数据/限流下无法定论。")
    else:
        print("  共识事件样本不足——诚实标注数据限制。")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
