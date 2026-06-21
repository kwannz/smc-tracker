"""轮询监控：拉庄持仓/流向 → 换仓/共识/背离 → 告警/推送。

两种用法：
  单次(cron 友好)：./.venv/bin/python scripts/poll_monitor.py
  持续动态监控   ：./.venv/bin/python scripts/poll_monitor.py --loop --interval 3600

每轮都把摘要推送到 webhook(需在 config/config.yaml 配 output.webhook_url，
支持 Discord/Slack/通用，无需 API key)。逐事件实时推送请用流式 app：
  PYTHONPATH=src ./.venv/bin/python -m smc_tracker.app
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.config import Config  # noqa: E402
from smc_tracker.monitor.poll_monitor import PollMonitor  # noqa: E402
from smc_tracker.notify import build_notifier  # noqa: E402
from smc_tracker.storage import Store  # noqa: E402


async def _cycle(mon: PollMonitor, notifier) -> None:
    now = int(time.time() * 1000)
    digest = await mon.run_once(now)
    print(digest, flush=True)
    if notifier.enabled:
        # 完整推送：notifier 内部按各渠道上限分段全发(不截断,#59)
        ok = await notifier.send(digest, now)
        print(f"\n[{time.strftime('%H:%M:%S')}] [webhook] {'已推送' if ok else '失败'}", flush=True)
    else:
        print("\n[webhook] 未配置 output.webhook_url（不推送）", flush=True)


async def main() -> int:
    ap = argparse.ArgumentParser(description="SMC 轮询监控")
    ap.add_argument("--loop", action="store_true", help="持续运行(动态监控)")
    ap.add_argument("--interval", type=float, default=3600.0, help="周期秒数(默认3600)")
    args = ap.parse_args()

    cfg_path = ROOT / "config" / "config.yaml"
    cfg = Config.load(cfg_path) if cfg_path.exists() else Config()
    store = Store(ROOT / "data" / "smc.db")
    mon = PollMonitor(cfg, store)
    notifier = build_notifier(cfg)

    if args.loop:
        print(f"动态监控启动：每 {args.interval:g}s 一轮，推送渠道={notifier.channels}",
              flush=True)
        while True:
            try:
                await _cycle(mon, notifier)
            except Exception as e:  # noqa: BLE001 — 单轮失败不退出
                # 带类型：TimeoutError 等 str(e) 为空，只打消息会丢信息(#58,与 cli 一致)
                print(f"[轮询出错] {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(args.interval)
    else:
        await _cycle(mon, notifier)
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
