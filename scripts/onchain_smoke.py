"""链上 meme 转账监控 smoke：纯公开 RPC，无 key，实查真实链上 Transfer。

做什么：
  1. 只读 data/smc.db 拿 meme 合约（EVM 链：ERC20→ETH / BEP20→BSC / BASE→BASE）。
  2. 对 PEPE/SHIB 等 EVM meme 查最近若干区块的 Transfer，打印最大几笔。
  3. 落库写到独立 data/smoke_onchain.db（绝不写 data/smc.db）。

运行：timeout 40 ./.venv/bin/python -u scripts/onchain_smoke.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.onchain import OnchainMemeMonitor  # noqa: E402
from smc_tracker.onchain.evm import CHAIN_BY_TOKEN_STANDARD  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402

# 公开 EVM RPC（零鉴权，已实证可用）。
CHAIN_RPC = {
    "ETH": "https://ethereum-rpc.publicnode.com",
    "BSC": "https://bsc-rpc.publicnode.com",
    "BASE": "https://base-rpc.publicnode.com",
}

SRC_DB = ROOT / "data" / "smc.db"
SMOKE_DB = ROOT / "data" / "smoke_onchain.db"

# smoke 优先看的几个高活跃 EVM meme（其它也会一并扫）。
PREFERRED = {"PEPE", "SHIB", "FLOKI", "TURBO", "BRETT", "AIXBT", "MEME"}


def read_evm_contracts() -> list[tuple[str, str, str]]:
    """只读 data/smc.db，返回 EVM meme 合约 (coin, token_standard, contract)。"""
    if not SRC_DB.exists():
        print(f"❌ 找不到 {SRC_DB}，先跑 ./.venv/bin/python scripts/bitget_snapshot.py")
        return []
    # 只读连接，绝不写源库。
    con = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT coin, chain, contract FROM meme_contracts ORDER BY coin, chain"
        ).fetchall()
    finally:
        con.close()
    return [r for r in rows if r[1] in CHAIN_BY_TOKEN_STANDARD]


async def main() -> int:
    evm = read_evm_contracts()
    if not evm:
        return 1

    pref = [r for r in evm if r[0] in PREFERRED] or evm
    print(f"EVM meme 合约 {len(evm)} 个，优先扫描 {len(pref)} 个：")
    for coin, std, contract in pref:
        print(f"  {coin:8s} {std:6s} {contract}")

    # 落库到独立 smoke 库（Store 会建标准表；onchain_transfers 由 monitor 自建）。
    if SMOKE_DB.exists():
        SMOKE_DB.unlink()
    store = Store(SMOKE_DB)
    # 把需要监控的合约灌进 smoke 库的 meme_contracts（不碰 data/smc.db）。
    import time
    now_ms = int(time.time() * 1000)
    for coin, std, contract in pref:
        store.upsert_contract(coin, std, contract, now_ms)

    # min_amount_usd 这里不设 USD 阈值（无价源），保留所有转账后按 token 量排序展示。
    mon = OnchainMemeMonitor(store, CHAIN_RPC, min_amount_usd=0.0, max_block_span=5)

    print("\n=== 抓取最近 5 个区块的链上 Transfer（公开 RPC，无 key）===")
    captured = await mon.poll_once(lookback=4)
    print(f"\n本轮共捕获 {len(captured)} 笔 Transfer，落库 {store.count('onchain_transfers')} 行 → {SMOKE_DB.name}")

    if captured:
        captured.sort(key=lambda t: t.amount, reverse=True)
        print("\n=== 金额最大的若干笔（真实链上数据）===")
        for t in captured[:10]:
            print(
                f"  [{t.coin:6s} {t.chain:4s}] block {t.block}\n"
                f"      from {t.from_addr}\n"
                f"      to   {t.to_addr}\n"
                f"      amount {t.amount:,.4f} {t.coin}  tx {t.tx_hash}"
            )
    else:
        print("\n（这 5 个区块内该批合约暂无 Transfer——meme 转账有间歇性，可重跑）")

    # Solana 跳过说明。
    sol = mon.skipped_contracts()
    if sol:
        print(f"\n⏭️  跳过 {len(sol)} 个非 EVM 合约（Solana，本期不支持，TODO 公开 SOL RPC）：")
        print("   " + ", ".join(sorted({c[0] for c in sol})))

    store.close()
    return 0 if captured else 0  # 即便偶发无转账也算脚本成功（不当作错误退出）


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
