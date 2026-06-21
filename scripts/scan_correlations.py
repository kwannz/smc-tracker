"""地址关联报告 CLI —— 扫描近 24h 协同行为。

用 AddressCorrelation 读 data/smc.db，打印：
- co_movers：同币同向同窗反复一起主动成交的协同地址对；
- clusters：并查集聚合出的地址群（庄家集团候选）；
- counterparties：频繁互为对手方的地址对（疑似关联钱包/自成交）。

库可能为空 —— 优雅处理，不报错。

运行：./.venv/bin/python scripts/scan_correlations.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.address_correlation import AddressCorrelation  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "smc.db"
WINDOW_MS = 24 * 60 * 60 * 1000  # 近 24h


def _short(addr: str) -> str:
    """地址缩写显示，过短则原样返回。"""
    return f"{addr[:8]}…{addr[-4:]}" if len(addr) > 14 else addr


def main() -> None:
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - WINDOW_MS

    if not DB_PATH.exists():
        print(f"⚠️ 库不存在：{DB_PATH}（先采集 hl_meme_trades 再扫描）")
        return

    print(f"📊 地址关联报告（近 24h，库={DB_PATH.name}）")
    print(f"   时间窗 [{since_ms} ~ {now_ms}]\n")

    with Store(DB_PATH) as store:
        corr = AddressCorrelation(store)

        # 1) 协同地址对
        movers = corr.co_movers(since_ms)
        print(f"— 协同地址对 co_movers（{len(movers)} 对）—")
        if movers:
            for a, b, c in movers:
                print(f"   {_short(a)}  ↔  {_short(b)}   共同行动 {c} 次")
        else:
            print("   （无：近 24h 无满足阈值的协同对，或库为空）")
        print()

        # 2) 地址群
        groups = corr.clusters(since_ms)
        print(f"— 地址群 clusters（{len(groups)} 群）—")
        if groups:
            for i, g in enumerate(groups, 1):
                members = "  ".join(_short(x) for x in g)
                print(f"   群#{i}（{len(g)} 个）：{members}")
        else:
            print("   （无：未聚合出 ≥2 地址的群）")
        print()

        # 3) 高频对手方
        cps = corr.counterparties(since_ms)
        print(f"— 高频对手方 counterparties（{len(cps)} 对）—")
        if cps:
            for buyer, seller, c in cps:
                print(f"   买 {_short(buyer)}  ←→  卖 {_short(seller)}   {c} 次")
        else:
            print("   （无：无频繁互为对手方的地址对，或库为空）")
        print()

    print("✅ 扫描完成。")


if __name__ == "__main__":
    main()
