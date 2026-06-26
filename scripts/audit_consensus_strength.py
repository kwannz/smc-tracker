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

_SAMPLE_ADDR = 50
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

    print("=" * 64)
    print(f"共识强度审计:前瞻 alpha(扣币种漂移,%)按共识度(同币同向 4h 窗内不同庄数,**不相交桶**)")
    print(f"  {'共识度':<12}" + "".join(f"{h:>14}" for h in _HZ))
    means = {}
    for bk in ("单庄(=1)", "双庄(=2)", "多庄(≥3)"):
        cells = []
        for hk in _HZ:
            v = buckets[bk][hk]
            m = np.mean(v) if len(v) > 15 else float("nan")
            means[(bk, hk)] = (m, len(v))
            cells.append(f"{m:+.3f}%(n{len(v)})" if len(v) > 15 else "—")
        print(f"  {bk:<12}" + "".join(f"{c:>14}" for c in cells))
    print("-" * 64)
    s, two, three = (means.get(("单庄(=1)", "24h"), (float('nan'), 0))[0],
                     means.get(("双庄(=2)", "24h"), (float('nan'), 0))[0],
                     means.get(("多庄(≥3)", "24h"), (float('nan'), 0))[0])
    if not np.isnan(s) and not np.isnan(two):
        if two > s + 0.1 and (np.isnan(three) or three < two - 0.1):
            print(f"结论:**非单调**——双庄 {two:+.2f}% > 单庄 {s:+.2f}%(共识有信息),但 多庄≥3 {three:+.2f}% 反塌回单庄水平")
            print("      ⇒'庄越多越强'**证伪**(拥挤反转或小样本);共识(≥2)有据但**勿把 ≥3 强共识当最强加码**。")
        elif two > s + 0.1:
            print(f"结论:双庄 {two:+.2f}% > 单庄 {s:+.2f}% 且 ≥3 {three:+.2f}% 续高 ⇒ 共识单调放大,假设成立。")
        else:
            print(f"结论:双庄 {two:+.2f}% ≈ 单庄 {s:+.2f}% ⇒ 共识不显著放大,存疑。")
    else:
        print("共识事件样本不足——诚实标注数据限制。")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
