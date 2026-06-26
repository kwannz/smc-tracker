#!/usr/bin/env python3
"""审计跟庄价值主张:带**真实延迟**地跟随庄入场,能否捕获有利前瞻价格?(#185 续:技巧持续≠跟单即盈利)

CLAUDE.md §一-3(先实证)+§二:系统产品=检测庄开仓→告警→用户跟。#185 证地址技巧持续(庄聪明),
但"庄聪明"≠"跟庄能赚"——隔两道坎:① 庄的**入场**(非整个 round-trip)是否领先价格?② edge 扛得住用户的检测+执行**延迟**吗?
本脚本验这两道坎,把"庄聪明"翻译成"跟庄产品能不能用"。

方法:取庄 Open 入场 fill(coin,time,方向)→HL 15m K线测**方向调整前瞻收益**(多:+收益好/空:−收益好),
  在 entry+**延迟**(0/15m/1h)起算多视野(1h/4h/24h),**扣除币种同向漂移基线**(=alpha 非 beta)。
关键纪律(#149/#177/#185):减去币种无条件漂移(否则牛市里所有多头入场都"领先"=beta 冒充 alpha);看 edge 随延迟衰减曲线。

用法:PYTHONPATH=src ./.venv/bin/python scripts/audit_copytrade_latency.py
"""
from __future__ import annotations

import asyncio
import bisect
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.hyperliquid.info_client import HyperliquidInfo  # noqa: E402
from smc_tracker.monitor.whale_discovery import fetch_leaderboard_rows  # noqa: E402

_SAMPLE_ADDR = 40
_MIN_ACCT = 300_000.0
_LOOKBACK_D = 30
_BAR_MS = 900_000              # 15m
_HZ = {"1h": 4, "4h": 16, "24h": 96}      # 视野(15m bar 数)
_LAT = {"0延迟": 0, "15m": 1, "1h": 4}     # 跟单延迟(15m bar 数)
_TOP_COINS = 20
_CONC = 3
_DELAY = 0.2


async def _fetch_fills(addrs: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    sem = asyncio.Semaphore(_CONC)
    async with HyperliquidInfo() as cli:
        async def one(a: str):
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


async def _fetch_candles(coins: list[str], start: int, end: int) -> dict[str, tuple]:
    px: dict[str, tuple] = {}
    async with HyperliquidInfo() as cli:
        for c in coins:
            try:
                cs = await cli.candle_snapshot(c, "15m", start, end)
                if len(cs) >= 100:
                    t = np.array([k.open_time_ms for k in cs])
                    p = np.array([k.c for k in cs], dtype=float)
                    px[c] = (t, p)
                await asyncio.sleep(_DELAY)
            except Exception as e:  # noqa: BLE001
                print(f"  K线跳过 {c}: {e}")
    return px


def _price_at(arr: tuple, t: int) -> float:
    ts, ps = arr
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or i >= len(ps):
        return 0.0
    return float(ps[i])


def _coin_drift(arr: tuple, h: int) -> float:
    """币种无条件 h-bar 平均收益(漂移基线=beta)。"""
    _, ps = arr
    if ps.size <= h:
        return 0.0
    r = ps[h:] / ps[:-h] - 1.0
    return float(np.mean(r))


async def main() -> None:
    print("取排行榜...")
    rows = await fetch_leaderboard_rows()
    cand = [r["ethAddress"] for r in rows
            if r.get("ethAddress") and float(r.get("accountValue", 0) or 0) >= _MIN_ACCT]
    step = max(1, len(cand) // _SAMPLE_ADDR)
    sample = cand[::step][:_SAMPLE_ADDR]
    print(f"采样 {len(sample)} 庄取 fills...")
    fmap = await _fetch_fills(sample)

    import time as _t
    now = int(_t.time() * 1000)
    cutoff = now - _LOOKBACK_D * 86_400_000
    entries = []   # (coin, time_ms, is_long)
    ccount: Counter = Counter()
    for fills in fmap.values():
        for f in fills:
            if f.dir.startswith("Open") and f.time_ms >= cutoff:
                entries.append((f.coin, f.time_ms, f.dir.endswith("Long")))
                ccount[f.coin] += 1
    coins = [c for c, _ in ccount.most_common(_TOP_COINS)]
    print(f"庄入场 {len(entries)} 笔,取 top{len(coins)} 币 HL K线...")
    px = await _fetch_candles(coins, cutoff - _BAR_MS, now)
    drift = {c: {h: _coin_drift(px[c], n) for h, n in _HZ.items()} for c in px}

    # 聚合:edge[lat][hz] = list of (方向调整前瞻收益 − 同向漂移基线)
    agg: dict = {lk: {hk: [] for hk in _HZ} for lk in _LAT}
    for coin, tm, is_long in entries:
        if coin not in px:
            continue
        for lk, lb in _LAT.items():
            t0 = tm + lb * _BAR_MS
            p0 = _price_at(px[coin], t0)
            if p0 <= 0:
                continue
            for hk, hb in _HZ.items():
                p1 = _price_at(px[coin], t0 + hb * _BAR_MS)
                if p1 <= 0:
                    continue
                r = p1 / p0 - 1.0
                adj = r if is_long else -r
                base = drift[coin][hk] if is_long else -drift[coin][hk]
                agg[lk][hk].append((adj - base) * 100.0)   # alpha %,扣漂移

    print("=" * 70)
    print(f"跟庄延迟 alpha(方向调整前瞻收益 − 币种同向漂移基线,%;n 笔入场)")
    print(f"  {'延迟':<8}" + "".join(f"{hk:>12}" for hk in _HZ))
    for lk in _LAT:
        cells = []
        for hk in _HZ:
            v = agg[lk][hk]
            cells.append(f"{np.mean(v):+.3f}%(n{len(v)})" if len(v) > 20 else "  —  ")
        print(f"  {lk:<8}" + "".join(f"{c:>12}" for c in cells))
    print("-" * 70)
    def mean(lk, hk):
        v = agg[lk][hk]
        return np.mean(v) if len(v) > 20 else float("nan")
    e0_4, e1_4 = mean("0延迟", "4h"), mean("1h", "4h")        # 短视野 0/1h 延迟
    e0_24, e1_24 = mean("0延迟", "24h"), mean("1h", "24h")    # 长视野 0/1h 延迟
    print(f"庄入场领先价格(alpha,扣漂移):0延迟 4h {e0_4:+.3f}% / 24h {e0_24:+.3f}%。")
    if e0_4 > 0.05 and e1_4 < e0_4 * 0.4 and e1_24 > 0.1:
        print(f"视野分化:**短视野(≤4h)edge 对延迟敏感**(1h延迟 4h→{e1_4:+.3f}% 大幅衰减)需低延迟检测(WS实时);"
              f"**长视野(24h)扛延迟**(1h延迟仍 {e1_24:+.3f}%)⇒慢跟宜长持。")
    elif e0_4 > 0.05:
        print(f"庄入场领先且较扛延迟(1h延迟 4h {e1_4:+.3f}%)⇒跟庄产品可用。")
    else:
        print(f"庄入场本身不明显领先(0延迟4h {e0_4:+.3f}%)⇒靠出场/仓位盈利,照入场跟单存疑。")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
