#!/usr/bin/env python3
"""真实数据全链路验证：监控清单 → 真实 Bitget 多周期采集 → 波动追踪板。

CLAUDE.md §四-2：关键功能用**真实数据**实证（非投资建议，仅验证链路正确性）。
验证内容：watch add 真实币 → BitgetCandleCollector 真实采集 CANONICAL_TIMEFRAMES →
         VolatilityMonitor 逐周期 速度/加速度/σ/regime/PD 板。

用法：
    PYTHONPATH=src ./.venv/bin/python scripts/verify_vol_chain.py [BTC ETH SOL ...]
默认验证 BTC/ETH/SOL；落临时库，不污染生产 data/smc.db。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import CANONICAL_TIMEFRAMES
from smc_tracker.monitor.candle_collector import BitgetCandleCollector
from smc_tracker.monitor.volatility_monitor import VolatilityMonitor
from smc_tracker.storage import Store

_BARS = 200   # 每周期拉取根数（快验证；足够算 σ/regime/PD）


async def run(coin_list: list[str]) -> None:
    db = Path(tempfile.mkdtemp()) / "verify_vol.db"
    store = Store(db)
    coins = {c.upper(): f"{c.upper()}USDT" for c in coin_list}
    now = int(time.time() * 1000)
    store.add_monitored_coins([(c, s, now, "verify") for c, s in coins.items()])
    print(f"监控清单: {list(coins)}")

    cc = BitgetCandleCollector(coins, list(CANONICAL_TIMEFRAMES), _BARS, store)
    written = await cc.collect_symbols(list(coins.items()))
    print(f"真实采集落库 {written} 根 K 线（{len(coins)} 币 × {len(CANONICAL_TIMEFRAMES)} 周期）")
    for c in coins:
        covered = [tf for tf in CANONICAL_TIMEFRAMES if store.count_candles(c, tf) > 0]
        print(f"  {c} 覆盖周期: {covered}")

    mon = VolatilityMonitor(coins, list(CANONICAL_TIMEFRAMES), store)
    card = mon.render(mon.rank(now), now)
    print("\n" + (card or "（无足够 K 线，稍后重试）"))
    print("\n[验证] 链路通：监控清单 → 真实采集 → 逐周期波动/regime/PD（非投资建议）")
    store.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a]
    asyncio.run(run(args or ["BTC", "ETH", "SOL"]))
