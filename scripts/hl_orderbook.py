"""HL 挂单墙动态监控独立脚本（领先信号，无 API key）。

连 HL l2Book WS，attach HLOrderbookMonitor，跑 secs 秒，收集挂单墙 build/pull 信号打印摘要。

诚实定位（CLAUDE.md #1）：挂单墙=意图告警（可能 spoof），非确定方向。
  bid 墙=支撑/吸筹意图；ask 墙=压制/分销意图。先于成交，仅供前瞻参考。

用法：PYTHONPATH=src ./.venv/bin/python scripts/hl_orderbook.py --coins BTC,ETH --secs 30
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from smc_tracker.hyperliquid.ws_client import HyperliquidWSClient
from smc_tracker.monitor import HLOrderbookMonitor


async def run_orderbook_stream(coins: list[str], secs: float) -> list[dict]:
    """连 HL WS，attach HLOrderbookMonitor，跑 secs 秒，返回收集的墙信号列表。"""
    signals: list[dict] = []

    def _on_wall(evt: dict) -> None:
        signals.append(evt)
        side_cn = "买墙(支撑/吸筹意图)" if evt["side"] == "bid" else "卖墙(压制/分销意图)"
        kind_cn = "🟢出现" if evt["kind"] == "build" else "⚪抽单"
        print(f"{kind_cn} {evt['coin']} {side_cn} @ {evt['px']:g}  "
              f"≈${evt['notional']:,.0f}")

    ws = HyperliquidWSClient()
    mon = HLOrderbookMonitor(coins, ws, store=None, on_wall_signal=_on_wall)
    mon.attach()

    run_task = asyncio.create_task(ws.run())
    try:
        await asyncio.wait_for(ws.wait_connected(), timeout=15)
    except asyncio.TimeoutError:
        print("WS 连接超时")
        await ws.stop()
        run_task.cancel()
        return signals

    print(f"已连接 HL WS，监控 {len(coins)} 个币挂单墙 {secs:.0f}s：{', '.join(coins)}")
    await asyncio.sleep(secs)
    await ws.stop()
    run_task.cancel()
    try:
        await run_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    # 摘要
    kinds = Counter(s["kind"] for s in signals)
    print(f"\n{'='*60}")
    print(f"摘要：收到 {mon.frames_seen} 帧 l2Book，检出墙事件 {len(signals)} 条 "
          f"(build={kinds.get('build', 0)} / pull={kinds.get('pull', 0)})")
    for coin in coins:
        imb = mon.book_imbalance(coin)
        print(f"  {coin} 当前挂单失衡 {imb['imbalance']:+.3f} "
              f"(买${imb['bid_usd']:,.0f} / 卖${imb['ask_usd']:,.0f})")
    print(f"{'='*60}")
    return signals


def main() -> None:
    ap = argparse.ArgumentParser(description="Hyperliquid l2Book 挂单墙动态监控")
    ap.add_argument("--coins", type=str, default="BTC,ETH",
                    help="逗号分隔的币种（默认 BTC,ETH）")
    ap.add_argument("--secs", type=float, default=30.0, help="运行秒数（默认 30）")
    args = ap.parse_args()
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    asyncio.run(run_orderbook_stream(coins, args.secs))


if __name__ == "__main__":
    main()
