"""诊断脚本(#98 一次性)：用生产历史数据验证背离/暴涨等信号是否反向 / 是否精确。

按 systematic-debugging：区分「真信号反向」vs「市场 beta 污染(看涨信号撞上普跌)」。
输出每类信号的方向分布、各向命中、币种集中度、同期市场漂移、市场中性(去 beta)命中率。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.review import market_neutral_stats
from smc_tracker.storage import Store


def main(db: str = "data/smc.db") -> None:
    s = Store(db)
    c = s.conn
    for kind in ("背离", "暴涨", "跟庄", "SMC", "超级", "前瞻", "OKX"):
        rows = c.execute(
            "SELECT direction,COUNT(*),SUM(correct),AVG(realized_ret) "
            "FROM predictions WHERE kind=? AND evaluated=1 GROUP BY direction", (kind,)
        ).fetchall()
        if not rows:
            continue
        print(f"\n===== {kind} =====")
        for d, n, hit, avgret in rows:
            hit = hit or 0
            print(f"   {d:<6} n={n} 命中={hit}({100*hit//n if n else 0}%) 平均ret={avgret:+.4f}")
        coins = c.execute(
            "SELECT coin,COUNT(*) FROM predictions WHERE kind=? AND evaluated=1 "
            "GROUP BY coin ORDER BY 2 DESC", (kind,)
        ).fetchall()
        print("   币种:", ", ".join(f"{co}x{n}" for co, n in coins))
        recs = [(int(ts), d, rret) for ts, d, rret in c.execute(
            "SELECT ts,direction,realized_ret FROM predictions WHERE kind=? AND evaluated=1", (kind,))]
        mn = market_neutral_stats(recs)
        print(f"   市场中性(去beta): n={mn['n']} 中性命中率={mn['hit_rate']:.2f} "
              f"edge={mn['edge']:+.2f} 平均超额={mn['avg_excess']:+.4f}")

    mk = c.execute("SELECT AVG(realized_ret),COUNT(*) FROM predictions WHERE evaluated=1").fetchone()
    print(f"\n=== 全样本同期市场漂移(方向无关)={mk[0]:+.4f} n={mk[1]} "
          f"(负=评估期普跌→看涨信号被 beta 拖累) ===")
    s.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/smc.db")
