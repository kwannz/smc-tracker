"""查询地址轨迹 / 列出可疑地址。

运行：
  ./.venv/bin/python scripts/trajectory.py            # 列出已标记的可疑地址
  ./.venv/bin/python scripts/trajectory.py <address>  # 打印某地址的成交轨迹
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.storage import Store  # noqa: E402


def _hms(ms: int) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ms / 1000)) if ms else "--"


store = Store(ROOT / "data" / "smc.db")

if len(sys.argv) > 1:
    addr = sys.argv[1]
    traj = store.address_trajectory(addr, limit=50)
    print(f"📍 地址轨迹 {addr}（最近 {len(traj)} 笔 meme 成交）")
    running = 0.0
    for ts, coin, side, notional, px, is_taker in reversed(traj):
        running += notional if side == "BUY" else -notional
        tk = "主动" if is_taker else "被动"
        print(f"  [{_hms(ts)}] {coin:<8} {side}({tk}) ${notional:,.0f} @ {px:g}  "
              f"净累计 ${running:,.0f}")
else:
    rows = store.flagged_addresses(limit=50)
    print(f"🚨 已标记可疑地址 {len(rows)} 个：")
    for addr, coin, reason, net, promoted, first, last in rows:
        p = "✓已升级" if promoted else ""
        print(f"  {addr} | {reason} ${net:,.0f} | 首现 {_hms(first)} 最近 {_hms(last)} {p}")

store.close()
