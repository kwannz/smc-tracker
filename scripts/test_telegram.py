"""测试 Telegram 推送是否配好（配好 bot_token+chat_id 后运行）。

  ./.venv/bin/python scripts/test_telegram.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.config import Config  # noqa: E402
from smc_tracker.notify import build_notifier  # noqa: E402


async def main() -> int:
    cfg_path = ROOT / "config" / "config.yaml"
    cfg = Config.load(cfg_path) if cfg_path.exists() else Config()
    n = build_notifier(cfg)
    if not n.enabled:
        print("⚠ 未配置任何推送渠道。请在 config/config.yaml 填 telegram.bot_token + chat_id"
              "（@BotFather 建机器人；把机器人加为频道管理员；chat_id 用频道 @username）。")
        return 1
    msg = (f"✅ SMC 抓庄监控 推送测试\n时间 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
           f"渠道数={n.channels}。收到本消息说明推送已打通。")
    ok = await n.send(msg, int(time.time() * 1000))
    print("已推送 ✅" if ok else "推送失败 ❌（检查 bot_token/chat_id，机器人是否为频道管理员）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
