"""WS 实连冒烟测试：订阅 BTC trades + allMids，收满 N 条消息后退出并报告延迟。

运行：./.venv/bin/python scripts/smoke_ws.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.hyperliquid import HyperliquidWSClient, Subscription  # noqa: E402
from smc_tracker.models import Side  # noqa: E402

TARGET_TRADES = 5


async def main() -> int:
    client = HyperliquidWSClient()
    got_mids = asyncio.Event()
    trades_seen = 0
    done = asyncio.Event()
    latencies_ms: list[float] = []

    def on_trades(data, recv_ns):
        nonlocal trades_seen
        now_ms = time.time() * 1000
        for t in data:
            lat = now_ms - int(t["time"])
            latencies_ms.append(lat)
            side = Side.from_hl(t["side"]).name
            print(f"  [trade] {t['coin']:>5} {side:<4} px={t['px']:>10} sz={t['sz']:>8}  "
                  f"延迟≈{lat:6.0f}ms")
            trades_seen += 1
            if trades_seen >= TARGET_TRADES:
                done.set()

    def on_mids(data, recv_ns):
        if not got_mids.is_set():
            mids = data.get("mids", {})
            print(f"  [allMids] 收到 {len(mids)} 个 coin 中间价, BTC={mids.get('BTC')}")
            got_mids.set()

    client.subscribe(Subscription(type="trades", coin="BTC"), on_trades)
    client.subscribe(Subscription(type="allMids"), on_mids)

    run_task = asyncio.create_task(client.run())
    print("连接 Hyperliquid 主网 WS …")
    try:
        await asyncio.wait_for(asyncio.gather(done.wait(), got_mids.wait()), timeout=30)
        ok = True
    except asyncio.TimeoutError:
        ok = False
        print("⛔ 30s 内未收满目标消息")
    finally:
        await client.stop()
        run_task.cancel()

    if latencies_ms:
        latencies_ms.sort()
        p50 = latencies_ms[len(latencies_ms) // 2]
        print(f"\n成交延迟统计: n={len(latencies_ms)} min={latencies_ms[0]:.0f}ms "
              f"p50={p50:.0f}ms max={latencies_ms[-1]:.0f}ms")
    print("✅ 冒烟测试通过" if ok and got_mids.is_set() else "❌ 冒烟测试未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
