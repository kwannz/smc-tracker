"""Bitget meme OI 实时流 smoke：实连公共 WS ticker，订阅全部 meme 永续，

跑约 20 秒，实时打印 OI 与变化（异动），结束打印最新 OI 与落库行数。
全程无需 API key（公开频道）。独立 db：data/smoke_bitget_oi.db。

运行：timeout 35 ./.venv/bin/python -u scripts/bitget_oi_stream.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.bitget import BitgetREST, BitgetWSClient  # noqa: E402
from smc_tracker.memecoins import normalize  # noqa: E402
from smc_tracker.monitor.bitget_oi_monitor import BitgetOIMonitor  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402

RUN_SECONDS = 20.0
DB_PATH = ROOT / "data" / "smoke_bitget_oi.db"


def load_meme_markets() -> list[str]:
    p = ROOT / "config" / "meme_markets.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return raw.get("meme_markets") or []


def on_surge(e: dict) -> None:
    dir_txt = "增" if e["change"] > 0 else "减"
    print(f"  🚨 OI 异动 {e['symbol']:<14}({e['coin']}) {dir_txt}{e['change']*100:+.2f}%  "
          f"OI {e['prev_oi']:,.0f}→{e['oi_size']:,.0f}  OI≈${e['oi_usd']:,.0f}  "
          f"funding={e['funding']*100:+.4f}%")


async def main() -> int:
    hl_memes = load_meme_markets()
    if not hl_memes:
        print("❌ config/meme_markets.yaml 为空，先跑 scripts/build_meme_list.py")
        return 1
    canon = {normalize(c) for c in hl_memes}
    print(f"meme 清单 {len(hl_memes)} 个：{hl_memes}")

    # 1) Bitget 永续符号映射：canonical -> symbol（一次 REST，无需 key）
    async with BitgetREST() as bg:
        base_map = await bg.perp_base_coins()             # symbol -> baseCoin
    canon_to_symbol: dict[str, str] = {}
    for symbol, base in base_map.items():
        n = normalize(base)
        if n in canon and n not in canon_to_symbol:
            canon_to_symbol[n] = symbol
    symbols = sorted(canon_to_symbol.values())
    symbol_to_coin = {sym: n for n, sym in canon_to_symbol.items()}
    print(f"在 Bitget 永续匹配到 {len(symbols)} 个 meme symbol：{symbols}")
    if not symbols:
        print("❌ 未匹配到任何 meme symbol")
        return 1

    # 2) 挂载 OI 监控（异动阈值用 0.1% 让 smoke 期间更易看到变化）
    store = Store(DB_PATH)
    ws = BitgetWSClient()
    mon = BitgetOIMonitor(symbols, symbol_to_coin, ws, store,
                          surge_pct=0.001, on_surge=on_surge, flush_threshold=50)
    mon.attach()

    ws_task = asyncio.create_task(ws.run())
    print(f"\n=== 实时订阅 ticker，运行约 {RUN_SECONDS:.0f}s ===")
    t0 = time.monotonic()
    last_report = t0
    while time.monotonic() - t0 < RUN_SECONDS:
        await asyncio.sleep(1.0)
        now = time.monotonic()
        if now - last_report >= 5.0:
            last_report = now
            n_sym = len(mon.all_latest())
            print(f"  [{now - t0:4.0f}s] ticks={mon.ticks_seen} 异动={mon.surges_seen} "
                  f"已收到 {n_sym}/{len(symbols)} 个 symbol 的 OI，缓冲 {len(mon._buffer)} 条待落库")

    # 3) 收尾：停 WS，flush 剩余缓冲
    await ws.stop()
    ws_task.cancel()
    flushed = mon.flush()
    print(f"\nflush 落库 {flushed} 条（剩余缓冲）")

    # 4) 打印最新 OI 快照
    print("\n=== 最新 OI 快照（内存）===")
    latest = mon.all_latest()
    for sym in sorted(latest):
        snap = latest[sym]
        print(f"  {sym:<16} OI={snap['oi_size']:>16,.0f}  OI≈${snap['oi_usd']:>14,.0f}  "
              f"mark={snap['mark_px']:<12g} funding={snap['funding']*100:+.4f}%")

    n_rows = store.count("bitget_oi")
    print(f"\n汇总: ticks={mon.ticks_seen} 异动={mon.surges_seen} "
          f"收到 {len(latest)}/{len(symbols)} 个 symbol  SQLite bitget_oi={n_rows} 行")
    store.close()
    if mon.ticks_seen == 0:
        print("❌ 未收到任何 ticker")
        return 1
    print("✅ 实时收到 OI 并落库")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
