"""Telegram 推送（Bot API，HTTP，无需额外库）。

向 Telegram 频道/群推送监控告警（类似 @BWE_OI_Price_monitor）。
设置：①@BotFather 建机器人→bot_token；②建频道，把机器人加为管理员；
③chat_id 用频道 @username 或数字 id(-100…)。bot_token+chat_id 即可推送（无需 api_id/api_hash）。
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
import orjson

from .chunk import split_message

log = logging.getLogger("notify.tg")

_TG_LIMIT = 4000          # Telegram 单条上限 4096，留余量
_TG_CHUNK_GAP_S = 0.4     # 同聊天分段间隔，避免 429 限流


class TelegramNotifier:
    def __init__(self, bot_token: str = "", chat_id: str = "",
                 timeout_sec: float = 8.0, min_interval_ms: int = 1200) -> None:
        self.bot_token = bot_token or ""
        self.chat_id = str(chat_id or "")
        self.timeout_sec = timeout_sec
        self.min_interval_ms = min_interval_ms
        self._last_sent_ms = 0
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, text: str, now_ms: int = 0) -> bool:
        """完整推送：长文按 4000 上限切成多条全部发出（不截断，用户要求 TG 完整）。

        min_interval_ms 仅限制「不同告警」频率；同一条消息的各分段顺序发出(间隔
        _TG_CHUNK_GAP_S 防 429)。所有分段都成功才记 sent，否则记 failed。
        """
        if not self.enabled:
            return False
        if now_ms and self._last_sent_ms and now_ms - self._last_sent_ms < self.min_interval_ms:
            return False
        chunks = split_message(text, _TG_LIMIT)
        if not chunks:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        n = len(chunks)
        ok_all = True
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout_sec)) as s:
                for i, chunk in enumerate(chunks):
                    if i:
                        await asyncio.sleep(_TG_CHUNK_GAP_S)
                    # 多段时加 (i/n) 页码，便于阅读
                    body_txt = chunk if n == 1 else f"({i + 1}/{n})\n{chunk}"
                    payload = {"chat_id": self.chat_id, "text": body_txt,
                               "disable_web_page_preview": True}
                    async with s.post(url, data=orjson.dumps(payload),
                                      headers={"Content-Type": "application/json"}) as resp:
                        body = orjson.loads(await resp.read())
                    if not body.get("ok"):
                        ok_all = False
                        log.warning("Telegram 返回(%d/%d): %s", i + 1, n,
                                    body.get("description"))
            if ok_all:
                # 只在全部分段成功时才更新时间戳，避免部分失败也触发限速；
                # now_ms=0 表示调用方未传时间，不写入（否则永久锁死限速）
                if now_ms:
                    self._last_sent_ms = now_ms
                self.sent += 1
            else:
                self.failed += 1
            return ok_all
        except Exception as e:  # noqa: BLE001 — 推送失败不影响主流程
            self.failed += 1
            log.warning("Telegram 推送失败: %s", e)
            return False
