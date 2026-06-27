#!/usr/bin/env python3
"""跑谐波交易策略回测(真实数据,大样本)——看策略整体表现 + 置信分层。

#206 优化后回测高效,本脚本对多币×2000bar 真实 K 线跑 harmonic_backtest(无SFG,#205已验SFG不帮忙),
报告:① 各币 + 合计 freqtrade 式绩效;② min_conf 分层(0/0.5/0.75)——验证 #165 置信校准(高置信是否高胜率)。

用法:PYTHONPATH=src ./.venv/bin/python scripts/run_harmonic_strategy.py [coins...]
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.backtest import BacktestResult, harmonic_backtest  # noqa: E402
from smc_tracker.monitor.candle_collector import BitgetCandleCollector  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402

_TF = "1H"
_BARS = 2000
_DEFAULT = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
            "LINK", "TRX", "DOT", "LTC", "BCH", "NEAR", "APT"]


def _line(tag: str, r: BacktestResult) -> str:
    n = r.wins + r.losses
    pf = r.profit_factor
    return (f"  {tag:<14}{n:>5}{r.win_rate*100:>7.1f}%{r.expectancy:>+8.2f}R"
            f"{(pf if pf != float('inf') else 99):>7.2f}{r.total_r:>+8.1f}{r.max_drawdown:>7.1f}R")


async def main() -> None:
    coins_in = sys.argv[1:] or _DEFAULT
    coins = {c.upper(): f"{c.upper()}USDT" for c in coins_in}
    store = Store(Path(tempfile.mkdtemp()) / "strat.db")
    now = int(time.time() * 1000)
    store.add_monitored_coins([(c, s, now, "v") for c, s in coins.items()])
    print(f"采集真实 {_TF} K 线({_BARS} bar/币, {len(coins)} 币)...")
    await BitgetCandleCollector(coins, [_TF], _BARS, store).collect_symbols(list(coins.items()))

    cand = {c: store.get_candles(c, _TF, limit=_BARS) for c in coins}
    cand = {c: cs for c, cs in cand.items() if len(cs) >= 300}
    print(f"有效 {len(cand)} 币(各≥300bar)\n")

    t0 = time.perf_counter()
    print("=" * 70)
    print("【① 各币谐波策略绩效(min_conf=0,无SFG)】")
    print(f"  {'币':<14}{'交易':>5}{'胜率':>8}{'期望':>9}{'盈亏比':>7}{'总R':>8}{'回撤':>8}")
    agg = BacktestResult("合计")
    for c, cs in cand.items():
        r = harmonic_backtest(c, _TF, cs, target_rr=2.0)
        if r.wins + r.losses > 0:
            print(_line(c, r))
            agg.trades.extend(r.trades)
    print("  " + "─" * 60)
    print(_line("合计", agg))
    print("-" * 70)

    print("【② 多因子汇合对比(谐波 × Fib已含 × SFG × S/R × 置信门控)——数据裁决哪个真提升】")
    print(f"  {'配置':<14}{'交易':>5}{'胜率':>8}{'期望':>9}{'盈亏比':>7}{'总R':>8}{'回撤':>8}")
    configs = [
        ("谐波(基线)", dict()),
        ("+S/R确认", dict(require_sr=True)),
        ("+SFG共识", dict(require_sfg=True)),
        ("+置信≥0.75", dict(min_conf=0.75)),
        ("+S/R+置信", dict(require_sr=True, min_conf=0.75)),
        ("全汇合", dict(require_sr=True, require_sfg=True, min_conf=0.75)),
    ]
    for name, kw in configs:
        agg2 = BacktestResult(name)
        for c, cs in cand.items():
            r = harmonic_backtest(c, _TF, cs, target_rr=2.0, **kw)
            agg2.trades.extend(r.trades)
        print(_line(name, agg2))
    print("-" * 70)
    print(f"回测耗时 {time.perf_counter()-t0:.1f}s（{len(cand)}币×{_BARS}bar×4配置）")
    n = agg.wins + agg.losses
    if n > 20:
        verdict = ("正期望策略,可行" if agg.expectancy > 0.1 and agg.profit_factor > 1.2
                   else "边际/不稳,需更多样本或参数调优")
        print(f"结论:谐波策略合计 {n}笔 胜率{agg.win_rate*100:.0f}% 期望{agg.expectancy:+.2f}R ⇒ {verdict}。")
    else:
        print(f"样本偏小(n={n}),谐波形态稀疏——结论谨慎。")
    print("=" * 70)
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
