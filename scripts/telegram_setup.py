"""Telegram 一键配置：发现/设置频道 chat_id → 发测试消息 → 写回 config.yaml。

前提：已在 config.yaml 填好 telegram.bot_token，并把该 bot 加为目标频道的管理员。

用法：
  ./.venv/bin/python scripts/telegram_setup.py            # 自动从 getUpdates 发现频道
  ./.venv/bin/python scripts/telegram_setup.py @我的频道   # 指定频道 @username 或数字 id
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.config import Config  # noqa: E402

CFG_PATH = ROOT / "config" / "config.yaml"


def api(token: str, method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return json.loads(urllib.request.urlopen(url, timeout=15).read())


def discover_chat(token: str) -> tuple[str, str] | None:
    """从 getUpdates 找一个可推送的 chat（优先频道）。返回 (chat_id, 描述)。"""
    up = api(token, "getUpdates")
    found: dict[str, tuple[str, str]] = {}
    for u in up.get("result", []):
        for key in ("channel_post", "message", "my_chat_member"):
            if key in u:
                c = u[key].get("chat", {})
                cid = str(c.get("id"))
                found[cid] = (c.get("type", ""),
                              c.get("title") or c.get("username") or c.get("first_name") or "")
    if not found:
        return None
    # 优先 channel
    for cid, (ctype, name) in found.items():
        if ctype == "channel":
            return cid, f"{ctype}:{name}"
    cid, (ctype, name) = next(iter(found.items()))
    return cid, f"{ctype}:{name}"


def save_chat_id(chat_id: str) -> None:
    text = CFG_PATH.read_text(encoding="utf-8")
    import re
    new = re.sub(r'(\n\s*chat_id:\s*)".*?"', rf'\1"{chat_id}"', text, count=1)
    CFG_PATH.write_text(new, encoding="utf-8")


def main() -> int:
    cfg = Config.load(CFG_PATH) if CFG_PATH.exists() else Config()
    token = cfg.telegram.bot_token
    if not token:
        print("⚠ config.yaml 未填 telegram.bot_token")
        return 1

    if len(sys.argv) > 1:
        chat_id = sys.argv[1]
        desc = "命令行指定"
    else:
        got = discover_chat(token)
        if not got:
            print("⚠ getUpdates 没发现任何 chat。请先：\n"
                  "  1) 把 @Chiukwan49Bot 加为你频道的管理员(允许发消息)；\n"
                  "  2) 在频道发一条任意消息(或私聊 bot 发 /start)；\n"
                  "  3) 再次运行本脚本。\n"
                  "  或直接指定：scripts/telegram_setup.py @你的频道username")
            return 1
        chat_id, desc = got

    # 发测试消息
    msg = f"✅ SMC 抓庄监控 已连接\n时间 {time.strftime('%Y-%m-%d %H:%M:%S')}\n目标 {chat_id} ({desc})"
    r = api(token, "sendMessage", {"chat_id": chat_id, "text": msg,
                                   "disable_web_page_preview": "true"})
    if not r.get("ok"):
        print(f"❌ 发送失败: {r.get('description')}（确认 bot 是该频道管理员、chat_id 正确）")
        return 1
    save_chat_id(chat_id)
    print(f"✅ 已发送测试消息到 {chat_id} ({desc}) 并写入 config.yaml。")
    print("   之后流式 app / 轮询监控的所有信号都会推到这里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
