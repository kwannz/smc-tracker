"""Webhook 推送（无 API key —— webhook URL 自带鉴权，兼容 Discord/Slack/通用）。

POST JSON 同时带 content(Discord) 与 text(Slack/通用) 键，各服务读各自字段，互不干扰。
失败静默（不影响主流程），带轻量限流避免刷屏。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import orjson

from .chunk import split_message

log = logging.getLogger("notify")

_WH_LIMIT = 1900          # Discord content 上限 2000，留余量(Slack/通用更宽，取最严)
_WH_CHUNK_GAP_S = 0.4


class WebhookNotifier:
    def __init__(self, url: str = "", timeout_sec: float = 8.0,
                 min_interval_ms: int = 1500) -> None:
        self.url = url or ""
        self.timeout_sec = timeout_sec
        self.min_interval_ms = min_interval_ms
        self._last_sent_ms = 0
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def send(self, text: str, now_ms: int = 0) -> bool:
        """完整推送：长文按 1900 上限切成多条全部发出（不截断）。"""
        if not self.url:
            return False
        # 轻量限流：避免高频刷爆 webhook（仅限不同告警，同消息各分段顺序发）
        if now_ms and self._last_sent_ms and now_ms - self._last_sent_ms < self.min_interval_ms:
            return False
        chunks = split_message(text, _WH_LIMIT)
        if not chunks:
            return False
        n = len(chunks)
        ok_all = True
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout_sec)) as s:
                for i, chunk in enumerate(chunks):
                    if i:
                        await asyncio.sleep(_WH_CHUNK_GAP_S)
                    body = chunk if n == 1 else f"({i + 1}/{n})\n{chunk}"
                    payload = {"content": body, "text": body}
                    async with s.post(self.url, data=orjson.dumps(payload),
                                      headers={"Content-Type": "application/json"}) as resp:
                        if resp.status >= 300:
                            ok_all = False
                            log.warning("webhook 返回 %s (%d/%d)", resp.status, i + 1, n)
            self._last_sent_ms = now_ms
            if ok_all:
                self.sent += 1
            else:
                self.failed += 1
            return ok_all
        except Exception as e:  # noqa: BLE001 — 推送失败不影响主流程
            self.failed += 1
            log.warning("webhook 推送失败: %s", e)
            return False
