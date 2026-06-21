"""异步并行收集 Bitget meme 历史 K 线（默认 1H，回溯 N 年）到外置盘 data/history/。

第一性原理实证：history-candles 每页 200 根，data 升序([0]最早)，用 endTime=本页最早ts 回溯。
字段：[ts, open, high, low, close, baseVol, quoteVol]。

运行：./.venv/bin/python scripts/fetch_bitget_history.py [granularity=1H] [years=3] [concurrency=6]
"""
from __future__ import annotations

import asyncio
import csv
import sys
import time
from pathlib import Path

import aiohttp
import orjson
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.bitget import BitgetREST  # noqa: E402
from smc_tracker.memecoins import normalize  # noqa: E402

GRAN = sys.argv[1] if len(sys.argv) > 1 else "1H"
YEARS = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
CONC = int(sys.argv[3]) if len(sys.argv) > 3 else 6
OUT = ROOT / "data" / "history"
BASE = "https://api.bitget.com/api/v2/mix/market/history-candles"


def load_memes() -> list[str]:
    raw = yaml.safe_load((ROOT / "config" / "meme_markets.yaml").read_text("utf-8")) or {}
    return raw.get("meme_markets") or []


async def fetch_symbol(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                       coin: str, symbol: str, start_ms: int) -> int:
    """回溯拉取一个 symbol 的全部历史到 start_ms，存 CSV。返回根数。"""
    rows: list[list] = []
    end = int(time.time() * 1000)
    seen_oldest = None
    while True:
        url = (f"{BASE}?symbol={symbol}&productType=USDT-FUTURES"
               f"&granularity={GRAN}&limit=200&endTime={end}")
        async with sem:
            try:
                async with session.get(url, headers={"User-Agent": "smc"}) as resp:
                    body = orjson.loads(await resp.read())
            except Exception as e:  # noqa: BLE001
                print(f"  {coin} 拉取异常: {e}")
                break
        data = body.get("data") or []
        if not data:
            break
        oldest = int(data[0][0])
        rows = data + rows                        # 升序前置
        if seen_oldest is not None and oldest >= seen_oldest:
            break                                 # 无进展，停止
        seen_oldest = oldest
        if oldest <= start_ms:
            break
        end = oldest                              # 继续往更早翻
        await asyncio.sleep(0.12)                 # 轻微限速
    rows = [r for r in rows if int(r[0]) >= start_ms]
    if not rows:
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{coin}_{GRAN}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "base_vol", "quote_vol"])
        w.writerows(rows)
    span_days = (int(rows[-1][0]) - int(rows[0][0])) / 86_400_000
    print(f"  ✅ {coin:<10} {len(rows):>6} 根 {GRAN}  覆盖 {span_days:.0f} 天 "
          f"({time.strftime('%Y-%m-%d', time.gmtime(int(rows[0][0])/1000))} → 今)")
    return len(rows)


async def main() -> int:
    memes = load_memes()
    start_ms = int((time.time() - YEARS * 365 * 86400) * 1000)
    print(f"收集 {len(memes)} 个 meme 的 {GRAN} 历史，回溯 {YEARS:g} 年，并发 {CONC}")
    print(f"输出目录: {OUT}")
    async with BitgetREST() as bg:
        base_map = await bg.perp_base_coins()
    canon_to_symbol = {}
    for sym, base in base_map.items():
        canon_to_symbol.setdefault(normalize(base), sym)

    sem = asyncio.Semaphore(CONC)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        tasks = []
        for hl_coin in memes:
            sym = canon_to_symbol.get(normalize(hl_coin))
            if sym:
                tasks.append(fetch_symbol(session, sem, hl_coin, sym, start_ms))
        results = await asyncio.gather(*tasks)
    print(f"\n完成：{sum(1 for r in results if r)} 个币有数据，共 {sum(results):,} 根 K 线，存于 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
