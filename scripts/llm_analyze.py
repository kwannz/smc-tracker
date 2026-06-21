"""按需触发一次 LLM(Codex GPT-5.4) 抓庄研判——独立验证研判层(需本机 codex login)。

运行：PYTHONPATH=src ./.venv/bin/python scripts/llm_analyze.py [hours] [--model gpt-5.4]
  hours    回看窗口(默认近 6 小时)，聚合为态势摘要喂给模型。
  --model  覆盖模型名(默认用 codex 配置档)。

注意：在受限沙箱里 codex 会挂起；请在已 `codex login` 的真实终端运行。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.llm import CodexClient, MarketAnalyst  # noqa: E402
from smc_tracker.notify import build_report  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402


async def _run(hours: float, model: str) -> None:
    now = int(time.time() * 1000)
    store = Store(ROOT / "data" / "smc.db")
    report = build_report(store, now - int(hours * 3600_000), now,
                          title=f"抓庄态势({hours:g}h)")
    store.close()
    print("=" * 60, "\n态势摘要(喂给模型)：\n", report, "\n", "=" * 60, sep="")
    client = CodexClient(model=model)
    analyst = MarketAnalyst(client.complete, enabled=True)
    print("\n⏳ 调用 Codex(GPT-5.4) 研判中…（首次可能数十秒）\n")
    verdict = await analyst.analyze(report)
    if verdict:
        print("🧠 LLM 抓庄研判：\n", verdict, sep="")
    else:
        print("⚠️ 研判失败/为空：确认已 `codex login`、模型名正确，或加大 timeout。")


def main() -> None:
    args = [a for a in sys.argv[1:]]
    model = ""
    if "--model" in args:
        i = args.index("--model")
        model = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    hours = float(args[0]) if args else 6.0
    asyncio.run(_run(hours, model))


if __name__ == "__main__":
    main()
