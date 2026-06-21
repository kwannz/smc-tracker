"""聪明钱地址深度分析 CLI。

对给定地址综合排行榜表现 + 当前持仓 + 近期成交，产出聪明钱画像并落库。

运行：./.venv/bin/python scripts/analyze_address.py <address>
"""
from __future__ import annotations

import asyncio
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import orjson  # noqa: E402

from smc_tracker.hyperliquid import HyperliquidInfo  # noqa: E402
from smc_tracker.monitor.address_analyzer import AddressAnalyzer  # noqa: E402
from smc_tracker.monitor.whale_discovery import LEADERBOARD  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _fetch_leaderboard_row(address: str) -> dict[str, Any] | None:
    """从排行榜 JSON 找该地址的 row（含 windowPerformances）。失败/未命中返回 None。"""
    addr = address.lower()
    try:
        req = urllib.request.Request(LEADERBOARD, headers={"User-Agent": "smc-tracker"})
        data = orjson.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 拉取排行榜失败（继续分析，无榜单维度）：{e}")
        return None
    for r in data.get("leaderboardRows") or []:
        if str(r.get("ethAddress", "")).lower() == addr:
            return r
    print("ℹ️ 该地址不在排行榜中（缺全期/月 PnL 维度）")
    return None


async def main(address: str) -> None:
    now_ms = int(time.time() * 1000)
    lb_row = _fetch_leaderboard_row(address)
    store = Store(ROOT / "data" / "smc.db")
    try:
        async with HyperliquidInfo() as info:
            profile = await AddressAnalyzer(store).analyze(address, info, now_ms, lb_row)
        print(AddressAnalyzer.fmt(profile))
    finally:
        store.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python scripts/analyze_address.py <address>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
