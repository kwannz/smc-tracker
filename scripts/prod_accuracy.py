"""生产准确率复盘(#98)：在真实累积 DB 上跑 accuracy_report，验证 k 币单位错配修复 + 离群剔除后命中率干净。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.review import PredictionReview
from smc_tracker.storage import Store


def main(db: str = "data/smc.db") -> None:
    s = Store(db)
    rev = PredictionReview(s)
    now = int(time.time() * 1000)
    rep = rev.accuracy_report(now - 7 * 86_400_000, now)
    print(f"总样本={rep['total_n']} 命中率={rep['hit_rate']:.2f} "
          f"剔除离群={rep.get('outlier_count')} 价差告警={rep['gap_warn_count']}")
    print("各信号源(离群剔除后):")
    for k, v in rep["by_kind"].items():
        print(f"  {k:<6} n={v['n']:<3} 命中率={v['hit_rate']:.2f} avg_ret={v['avg_ret']:+.4f}")
    mn = rep["market_neutral"]
    print(f"市场中性(去beta): n={mn['n']} 命中率={mn['hit_rate']:.2f} edge={mn['edge']:+.2f}")
    s.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/smc.db")
