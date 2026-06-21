"""按 Bitget 永续合约币种构建 meme 清单，落盘 config/meme_markets.yaml。

数据源（均为实时真实数据）：
  - Bitget USDT-M 永续合约： GET /api/v2/mix/market/contracts?productType=USDT-FUTURES
  - Hyperliquid 永续宇宙：    POST /info {"type":"meta"}
逻辑：MEME_BASES ∩ Bitget永续 ∩ Hyperliquid永续（归一化后），输出 Hyperliquid 币名。

运行：./.venv/bin/python scripts/build_meme_list.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.hyperliquid import HyperliquidInfo  # noqa: E402
from smc_tracker.memecoins import build_meme_markets, normalize, MEME_BASES  # noqa: E402

BITGET = "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES"


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "smc-meme-builder"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


async def main() -> int:
    # 1) Bitget 永续基础币
    bg = fetch_json(BITGET)
    rows = bg.get("data", [])
    if not rows:
        print("❌ Bitget 永续合约拉取为空")
        return 1
    bitget_bases = {r.get("baseCoin", "") for r in rows if r.get("baseCoin")}
    print(f"Bitget USDT-M 永续合约: {len(rows)} 个 (基础币 {len(bitget_bases)} 种)")

    # 2) Hyperliquid 永续币名
    async with HyperliquidInfo() as info:
        meta = await info.meta()
    hl_coins = [u["name"] for u in meta.get("universe", []) if not u.get("isDelisted")]
    print(f"Hyperliquid 永续宇宙: {len(hl_coins)} 个币")

    # 3) 求交集
    memes = build_meme_markets(hl_coins, bitget_bases)
    print(f"\n✅ Meme 永续清单 (Bitget∩Hyperliquid∩MEME): {len(memes)} 个")
    # 展示归一化映射，便于人工核对
    for c in memes:
        print(f"   {c:<12} → 规范 {normalize(c)}")

    # 诊断：MEME_BASES 中在 Bitget 有、但 Hyperliquid 没有的（无法监控）
    bitget_norm = {normalize(b) for b in bitget_bases}
    hl_norm = {normalize(c) for c in hl_coins}
    only_bitget = sorted((MEME_BASES & bitget_norm) - hl_norm)
    if only_bitget:
        print(f"\n⚠ 在 Bitget 永续有但 Hyperliquid 无（暂无法监控）: {only_bitget}")

    # 4) 落盘
    out_path = ROOT / "config" / "meme_markets.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 自动生成：scripts/build_meme_list.py",
             "# meme 永续清单 = Bitget USDT-M 永续 ∩ Hyperliquid 永续 ∩ 内置 meme 集",
             "# 数据源实时拉取，需定期重跑刷新（新 meme 上线）",
             "meme_markets:"]
    lines += [f"  - {c}" for c in memes]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
