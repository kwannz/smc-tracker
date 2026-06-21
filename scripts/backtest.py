"""真实历史 K 线回测 SMC 结构信号，对比不同共振过滤的胜率/期望。

数据源：Hyperliquid candleSnapshot（真实历史）。
对每个 meme 回测三档：基线(全部结构突破) / 要求 OB-FVG 共振 / 要求流动性扫荡共振。

运行：./.venv/bin/python scripts/backtest.py [bars] [interval]
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.backtest import Backtester, BacktestResult  # noqa: E402
from smc_tracker.hyperliquid import HyperliquidInfo  # noqa: E402

BARS = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
INTERVAL = sys.argv[2] if len(sys.argv) > 2 else "5m"
_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def load_memes() -> list[str]:
    raw = yaml.safe_load((ROOT / "config" / "meme_markets.yaml").read_text("utf-8")) or {}
    return raw.get("meme_markets") or ["kPEPE", "DOGE", "WIF", "kBONK"]


def pooled(results: list[BacktestResult]) -> tuple[int, int, float, float, float]:
    wins = sum(r.wins for r in results)
    losses = sum(r.losses for r in results)
    n = wins + losses
    resolved = [t for r in results for t in r.resolved]
    avg_r = sum(t.r for t in resolved) / len(resolved) if resolved else 0.0
    gw = sum(t.r for t in resolved if t.r > 0)
    gl = -sum(t.r for t in resolved if t.r < 0)
    pf = gw / gl if gl else float("inf")
    wr = wins / n if n else 0.0
    return n, wins, wr, avg_r, pf


async def main() -> int:
    memes = load_memes()
    now = int(time.time() * 1000)
    span = _MS.get(INTERVAL, 300_000)
    start = now - BARS * span
    print(f"回测 {len(memes)} 个 meme，每个 {BARS} 根 {INTERVAL} K 线，目标 2R，止损≤8%")
    print("=" * 78)

    sem = asyncio.Semaphore(6)
    async with HyperliquidInfo() as info:
        async def fetch(coin):
            async with sem:
                try:
                    return coin, await info.candle_snapshot(coin, INTERVAL, start, now)
                except Exception as e:  # noqa: BLE001
                    print(f"  {coin} 拉取失败: {e}")
                    return coin, []
        data = await asyncio.gather(*(fetch(c) for c in memes))

    configs = {
        "追突破(break)": dict(entry_mode="break"),
        "追突破+扫荡": dict(entry_mode="break", require_sweep=True),
        "回撤OB(retrace)": dict(entry_mode="retrace"),
        "回撤OB+扫荡": dict(entry_mode="retrace", require_sweep=True),
    }
    groups: dict[str, list] = {k: [] for k in configs}
    for coin, candles in data:
        if len(candles) < 50:
            continue
        bt = Backtester(coin)
        for label, kw in configs.items():
            groups[label].append(bt.run(candles, lookback=2, **kw))
        print(groups["回撤OB(retrace)"][-1].summary())

    print("=" * 78)
    for label in configs:
        n, wins, wr, avg_r, pf = pooled(groups[label])
        pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"
        print(f"  {label:<18} 交易{n:>5} 胜率{wr*100:5.1f}% 期望{avg_r:+.3f}R 盈亏比{pf_s}")
    print("=" * 78)
    print("注：聪明钱流向/OI/链上为实时数据无历史，未计入；此为 SMC 骨架回测。")
    print("    回撤入场=正统 SMC（不追高，等价格回到 OB 限价入）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
