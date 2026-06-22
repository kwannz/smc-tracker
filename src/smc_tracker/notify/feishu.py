"""飞书(Lark)自定义机器人 webhook 推送 —— 交互卡片(interactive card)+ 签名校验。

推送为飞书**交互卡片**(msg_type=interactive)：彩色头部(按内容类型配色)+ lark_md
完整正文 + 落款 note，信息完整充分、可读性强。机器人开「签名校验」时每条带 timestamp+sign：
  string_to_sign = f"{timestamp}\n{secret}"
  sign = base64( HMAC_SHA256(key=string_to_sign, msg=b"") )
失败静默(不影响主流程)；长文按上限分段成多张卡片全部发出；带轻量限流避免刷屏。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from typing import Any

import aiohttp
import orjson

from .chunk import split_message

log = logging.getLogger("notify")

_FS_LIMIT = 8000          # 飞书卡片正文较宽；多数推送一张卡片，超长才分段
_FS_CHUNK_GAP_S = 0.4
_FOOTER = "SMC 抓庄系统 · 实时监控"


def feishu_sign(timestamp: int, secret: str) -> str:
    """飞书签名：base64( HMAC-SHA256(key=f"{ts}\n{secret}", msg=b"") )。纯函数，可测。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def card_title(text: str) -> str:
    """取首个非空行作卡片标题(截断 120)。"""
    for ln in text.splitlines():
        if ln.strip():
            return ln.strip()[:120]
    return "SMC 监控"


def card_color(text: str) -> str:
    """按内容类型(首部 emoji/关键词)选飞书卡片头部色(template)：告警红/信号橙/摘要蓝等。"""
    head = text[:60]
    if any(k in head for k in ("💥", "强平", "🚨", "可疑", "反手", "平仓")):
        return "red"
    if any(k in head for k in ("🌟", "超级")):
        return "orange"
    if any(k in head for k in ("🔀", "背离")):
        return "orange"
    if any(k in head for k in ("🧱", "挂单墙")):
        return "violet"
    if any(k in head for k in ("🕸", "集团")):
        return "purple"
    if any(k in head for k in ("🐋", "净流", "跟庄", "共识")):
        return "turquoise"
    if any(k in head for k in ("🏦", "资金流", "持仓")):
        return "blue"
    if "🧠" in head:
        return "indigo"
    if any(k in head for k in ("📊", "回顾", "行情", "准确率")):
        return "wathet"
    return "blue"


class FeishuNotifier:
    """飞书自定义机器人推送器(交互卡片)。webhook_url 为空则禁用；有 secret 则带签名。"""

    def __init__(self, webhook_url: str = "", secret: str = "",
                 timeout_sec: float = 8.0, min_interval_ms: int = 1500) -> None:
        self.url = webhook_url or ""
        self.secret = secret or ""
        self.timeout_sec = timeout_sec
        self.min_interval_ms = min_interval_ms
        self._last_sent_ms = 0
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _sign_fields(self, body: dict[str, Any]) -> None:
        """有 secret 则原地注入 timestamp+sign(时间戳取当前秒，飞书要求 1 小时内)。"""
        if self.secret:
            ts = int(time.time())
            body["timestamp"] = str(ts)
            body["sign"] = feishu_sign(ts, self.secret)

    def _payload(self, body_text: str, title: str, color: str,
                 footer: str = _FOOTER) -> dict[str, Any]:
        """构造一张飞书交互卡片：彩色头部(title/color) + lark_md 正文 + 落款 note。"""
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body_text}},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "lark_md", "content": footer}]},
            ],
        }
        body: dict[str, Any] = {"msg_type": "interactive", "card": card}
        self._sign_fields(body)
        return body

    async def send(self, text: str, now_ms: int = 0) -> bool:
        """完整推送：整理成交互卡片；长文按 _FS_LIMIT 切成多张卡片全部发出(不截断)。"""
        if not self.url:
            return False
        # 轻量限流：避免高频刷爆(仅限不同告警，同消息各分段顺序发)
        if now_ms and self._last_sent_ms and now_ms - self._last_sent_ms < self.min_interval_ms:
            return False
        chunks = split_message(text, _FS_LIMIT)
        if not chunks:
            return False
        title = card_title(text)      # 标题/配色按完整内容一次性决定，分段沿用
        color = card_color(text)
        n = len(chunks)
        ok_all = True
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout_sec)) as s:
                for i, chunk in enumerate(chunks):
                    if i:
                        await asyncio.sleep(_FS_CHUNK_GAP_S)
                    footer = _FOOTER if n == 1 else f"{_FOOTER} · 第 {i + 1}/{n} 段"
                    payload = self._payload(chunk, title, color, footer)
                    async with s.post(self.url, data=orjson.dumps(payload),
                                      headers={"Content-Type": "application/json"}) as resp:
                        ok = False
                        if resp.status < 300:
                            try:
                                rj = orjson.loads(await resp.read())
                                ok = int(rj.get("code", rj.get("StatusCode", -1))) == 0
                            except Exception:  # noqa: BLE001 — 非 JSON 但 2xx 视为成功
                                ok = True
                        if not ok:
                            ok_all = False
                            log.warning("飞书返回非成功 (%d/%d) status=%s", i + 1, n, resp.status)
            self._last_sent_ms = now_ms
            if ok_all:
                self.sent += 1
            else:
                self.failed += 1
            return ok_all
        except Exception as e:  # noqa: BLE001 — 推送失败不影响主流程
            self.failed += 1
            log.warning("飞书推送失败: %s", e)
            return False
