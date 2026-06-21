"""按需生成 SQLite 摘要日报。

运行：./.venv/bin/python scripts/report.py [hours]   (默认近 24 小时)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.notify import build_report  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402

hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
now = int(time.time() * 1000)
store = Store(ROOT / "data" / "smc.db")
print(build_report(store, now - int(hours * 3600_000), now, title=f"SMC {hours:g}h 摘要"))
store.close()
