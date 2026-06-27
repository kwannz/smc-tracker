#!/usr/bin/env python3
"""验证 #203:真实数据回测谐波 edge,对比 SFG 共识确认(--require-sfg)是否真提升 edge。

CLAUDE.md §一-3(先实证)+ §四-2(真实数据)。#203 建了 SFG 入场确认,本脚本让数据裁决其增益:
对真实 Bitget K 线跑 harmonic_backtest(require_sfg=False vs True),对比胜率/期望/盈亏比/最大回撤。

用法:PYTHONPATH=src ./.venv/bin/python scripts/validate_harmonic_sfg.py [BTC ETH SOL ...]
默认 BTC/ETH/SOL/BNB/XRP;1H,各 1500 bar。
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
_BARS = 600


async def main() -> None:
    coins_in = sys.argv[1:] or ["BTC", "ETH", "SOL", "BNB", "XRP"]
    coins = {c.upper(): f"{c.upper()}USDT" for c in coins_in}
    db = Path(tempfile.mkdtemp()) / "hbt.db"
    store = Store(db)
    now = int(time.time() * 1000)
    store.add_monitored_coins([(c, s, now, "v") for c, s in coins.items()])
    print(f"采集真实 {_TF} K 线({_BARS} bar/币, {len(coins)} 币)...")
    cc = BitgetCandleCollector(coins, [_TF], _BARS, store)
    await cc.collect_symbols(list(coins.items()))

    def _agg(require_sfg: bool) -> BacktestResult:
        agg = BacktestResult("合计")
        for coin in coins:
            cs = store.get_candles(coin, _TF, limit=_BARS)
            if len(cs) < 200:
                continue
            r = harmonic_backtest(coin, _TF, cs, target_rr=2.0, require_sfg=require_sfg)
            agg.trades.extend(r.trades)
        return agg

    print("\n回测谐波 setup(no-repaint),对比 SFG 共识确认 on/off:")
    print("=" * 70)
    base = _agg(False)
    sfg = _agg(True)
    print(f"  {'SFG确认':<12}{'交易':>5}{'胜率':>8}{'期望R':>8}{'盈亏比':>8}{'总R':>8}{'回撤':>8}")
    for name, r in (("off(全收)", base), ("on(共识同向)", sfg)):
        n = r.wins + r.losses
        pf = r.profit_factor
        print(f"  {name:<12}{n:>5}{r.win_rate*100:>7.1f}%{r.expectancy:>+8.2f}"
              f"{(pf if pf != float('inf') else 99):>8.2f}{r.total_r:>+8.1f}{r.max_drawdown:>7.1f}R")
    print("-" * 70)
    nb, ns = base.wins + base.losses, sfg.wins + sfg.losses
    if nb > 5 and ns > 5:
        d_wr = (sfg.win_rate - base.win_rate) * 100
        d_exp = sfg.expectancy - base.expectancy
        if d_exp > 0.05 and d_wr > 0:
            print(f"结论:SFG 共识确认**提升** edge(胜率{d_wr:+.1f}pp、期望{d_exp:+.2f}R)⇒充分使用 SFG 有据,值得默认开。")
        elif d_exp < -0.05:
            print(f"结论:SFG 共识确认**降低** edge(期望{d_exp:+.2f}R)⇒SFG 反向过滤掉好 setup,作可选非默认。")
        else:
            print(f"结论:SFG 共识确认对 edge **无显著影响**(期望{d_exp:+.2f}R)——诚实:SFG 在此未兑现增益,保留可选。")
    else:
        print(f"样本不足(off={nb}/on={ns} 已平)——谐波形态稀疏,需更多币/更长历史定论。")
    print("=" * 70)
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
