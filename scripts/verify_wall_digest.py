"""用**生产真实挂单墙数据**验证 HLDigest 按币聚合渲染（无模拟数据）。

读 hl_orderbook_walls 表近 N 小时的真实 build 墙事件，喂入 HLDigest.add_wall，
渲染「整体分析 + 单一币种总结」——证明聚合逻辑在真实数据上正确。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.notify.digest import HLDigest
from smc_tracker.storage import Store


def main(db: str = "data/smc.db", hours: float = 12.0, min_ntl: float = 100_000.0) -> None:
    s = Store(db)
    now = int(time.time() * 1000)
    since = now - int(hours * 3_600_000)
    d = HLDigest()
    n = 0
    for ts, coin, side, kind, px, notional in s.recent_orderbook_walls(since):
        if kind != "build":               # 仅「墙出现」(领先意图)，与生产 _on_wall_signal 一致
            continue
        if float(notional) < min_ntl:      # 仅显著大墙
            continue
        d.add_wall(coin, side, float(notional), float(px))
        n += 1
    print(f"真实墙事件(build, 近 {hours}h, ≥${min_ntl:,.0f})= {n} 条")
    out = d.render(now)
    print(out if out else "(窗口内无符合阈值的真实墙)")
    s.close()


if __name__ == "__main__":
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    main(hours=hours)
