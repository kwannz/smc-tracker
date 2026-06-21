"""Bitget 系统快照：拉取 meme 永续的 OI + 资金费 + 链上合约地址，写入 SQLite。

交付需求：「Bitget USDT-M 永续合约 监控 oi 和相应 meme 的 blockchain 地址」。
数据源全部实时真实（见 bitget/rest.py 实证）。

运行：./.venv/bin/python scripts/bitget_snapshot.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.bitget import BitgetREST  # noqa: E402
from smc_tracker.memecoins import normalize  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402

NOW_MS = 0  # 由命令行注入真实时间，避免脚本内 Date.now（此处用 time）


def load_meme_markets() -> list[str]:
    p = ROOT / "config" / "meme_markets.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return raw.get("meme_markets") or []


async def main() -> int:
    import time
    now_ms = int(time.time() * 1000)
    hl_memes = load_meme_markets()
    if not hl_memes:
        print("❌ config/meme_markets.yaml 为空，先跑 scripts/build_meme_list.py")
        return 1
    canon = {normalize(c) for c in hl_memes}   # 规范基础符号集合
    print(f"meme 清单 {len(hl_memes)} 个：{hl_memes}")

    store = Store(ROOT / "data" / "smc.db")
    async with BitgetREST() as bg:
        # 1) Bitget 永续符号映射：canonical -> symbol
        base_map = await bg.perp_base_coins()             # symbol -> baseCoin
        canon_to_symbol: dict[str, str] = {}
        for symbol, base in base_map.items():
            n = normalize(base)
            if n in canon and n not in canon_to_symbol:
                canon_to_symbol[n] = symbol
        print(f"在 Bitget 永续匹配到 {len(canon_to_symbol)}/{len(canon)} 个 meme symbol")

        # 2) OI / 资金费 / 标记价（一次全市场 ticker）
        tickers = await bg.tickers()
        oi_rows = []
        print("\n=== Meme 永续 OI / 资金费 (Bitget) ===")
        for n, symbol in sorted(canon_to_symbol.items()):
            tk = tickers.get(symbol)
            if not tk:
                continue
            row = BitgetREST.parse_oi_row(symbol, n, tk, now_ms)
            oi_rows.append(row)
            _, _, oi_size, oi_usd, mark, funding, _ = row
            print(f"  {symbol:<16} OI={oi_size:>16,.0f}  OI≈${oi_usd:>14,.0f}  "
                  f"mark={mark:<12g} funding={funding*100:+.4f}%")
        store.insert_oi(oi_rows)
        print(f"已写入 {len(oi_rows)} 条 OI 到 SQLite")

        # 3) 链上合约地址（一次拉全量币种再本地匹配，避免并发限流）
        print("\n=== Meme 链上合约地址 (Bitget coins) ===")
        all_chains = await bg.all_coin_chains()
        n_addr = 0
        for n in sorted(canon):
            chains = all_chains.get(n.upper(), [])
            if not chains:
                print(f"  {n:<10} (无合约/原生链)")
                continue
            for chain, addr in chains:
                store.upsert_contract(n, chain, addr, now_ms)
                n_addr += 1
            shown = ", ".join(f"{c}:{a[:10]}…" for c, a in chains[:3])
            print(f"  {n:<10} {shown}{' …' if len(chains) > 3 else ''}")
        print(f"已写入 {n_addr} 条合约地址到 SQLite")

    print(f"\nSQLite 汇总: bitget_oi={store.count('bitget_oi')} 行, "
          f"meme_contracts={store.count('meme_contracts')} 行")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
