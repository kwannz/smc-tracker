"""HL meme 成交监控实连冒烟：订阅全部 meme，跑约 20s，打印成交并落库。

运行：./.venv/bin/python -u scripts/hl_meme_smoke.py

- 从 config/meme_markets.yaml 读 meme 清单（HL 币名）。
- 用独立 db：data/smoke_hl_meme.db（不碰 data/smc.db）。
- 结束时 flush，打印各表行数 + 每个 coin 的 top takers。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.hyperliquid import HyperliquidWSClient, Subscription  # noqa: E402
from smc_tracker.monitor.meme_trade_monitor import MemeTradeMonitor  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402

RUN_SECONDS = 20.0
DB_PATH = "data/smoke_hl_meme.db"
CONFIG_PATH = "config/meme_markets.yaml"


def load_memes() -> list[str]:
    root = Path(__file__).resolve().parents[1]
    with open(root / CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return list(cfg.get("meme_markets") or [])


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    memes = load_memes()
    store = Store(root / DB_PATH)
    ws = HyperliquidWSClient()

    printed = 0

    def on_trade(rec: dict) -> None:
        nonlocal printed
        if printed < 30:
            dir_txt = "BUY " if rec["taker_side"] == "B" else "SELL"
            print(f"  [大单] {rec['coin']:>9} {dir_txt} notional=${rec['notional']:>10.0f} "
                  f"taker={rec['taker']} buyer={rec['buyer']} seller={rec['seller']}")
            printed += 1

    # 阈值压低到 $500，确保 20s 内能看到大单回调样例
    monitor = MemeTradeMonitor(
        memes, ws, store, large_notional_usd=500.0, on_trade=on_trade,
    )
    monitor.attach()

    # 同时打印前若干笔普通成交（含地址），证明双方地址确实拿到了
    sample_printed = 0
    orig_ingest = monitor._ingest

    def ingest_with_sample(rec: dict) -> None:
        nonlocal sample_printed
        if sample_printed < 15:
            print(f"  [成交] {rec['coin']:>9} side={rec['taker_side']} "
                  f"px={rec['px']:<12} sz={rec['sz']:<12} notional=${rec['notional']:.2f} "
                  f"taker={rec['taker']}")
            sample_printed += 1
        orig_ingest(rec)

    monitor._ingest = ingest_with_sample  # type: ignore[method-assign]

    run_task = asyncio.create_task(ws.run())
    print(f"连接 Hyperliquid 主网 WS，订阅 {len(memes)} 个 meme，跑 {RUN_SECONDS:.0f}s …")
    print(f"meme: {', '.join(memes)}")
    t0 = time.time()

    # 周期 flush，避免缓冲堆积
    async def periodic_flush() -> None:
        while True:
            await asyncio.sleep(2.0)
            monitor.flush()

    flush_task = asyncio.create_task(periodic_flush())
    try:
        await asyncio.sleep(RUN_SECONDS)
    finally:
        flush_task.cancel()
        await ws.stop()
        run_task.cancel()

    flushed = monitor.flush()  # 落最后残余
    elapsed = time.time() - t0

    print("\n=== 结果 ===")
    print(f"运行 {elapsed:.1f}s，收到 meme 成交 {monitor.trades_seen} 笔，"
          f"大单(≥$500) {monitor.large_trades_seen} 笔，最后 flush {flushed} 行")
    print(f"hl_meme_trades 行数 = {store.count('hl_meme_trades')}")

    # 各 coin top takers（仅打印有成交的 coin）
    print("\n=== 各 coin top takers（净主动流向，买正卖负）===")
    nets = monitor.all_coin_net()
    active = sorted(nets.keys(), key=lambda c: abs(nets[c]), reverse=True)
    if not active:
        print("  （本次未收到任何 meme 成交）")
    for coin in active[:10]:
        top = store.top_meme_takers(coin, since_ms=0, limit=3)
        print(f"  {coin:>9} 净流向=${nets[coin]:>12.0f}")
        for addr, net in top:
            print(f"             {addr}  净=${net:>12.0f}")

    rows_total = store.count("hl_meme_trades")
    ok = monitor.trades_seen > 0 and rows_total > 0
    store.close()
    print("\n✅ 冒烟通过：收到带地址的 meme 成交并落库" if ok
          else "❌ 冒烟未通过：未收到成交或未落库")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
