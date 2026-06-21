"""多渠道推送聚合：同一条消息推到所有已配置的渠道（webhook + Telegram）。"""
from __future__ import annotations

import asyncio
from typing import Any


class MultiNotifier:
    def __init__(self, senders: list[Any]) -> None:
        # 仅保留已启用的渠道
        self.senders = [s for s in senders if getattr(s, "enabled", False)]

    @property
    def enabled(self) -> bool:
        return bool(self.senders)

    @property
    def channels(self) -> int:
        return len(self.senders)

    async def send(self, text: str, now_ms: int = 0) -> bool:
        if not self.senders:
            return False
        res = await asyncio.gather(*(s.send(text, now_ms) for s in self.senders),
                                   return_exceptions=True)
        return any(r is True for r in res)
