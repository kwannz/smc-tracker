"""飞书(Lark)自定义机器人 webhook 推送（支持「签名校验」）。

飞书机器人开启签名校验时，每条消息须带 timestamp + sign（HMAC-SHA256）：
  string_to_sign = f"{timestamp}\n{secret}"
  sign = base64( HMAC_SHA256(key=string_to_sign, msg=b"") )   # key 是 string_to_sign，消息体为空
POST JSON: {"timestamp","sign","msg_type":"text","content":{"text":...}}。
失败静默（不影响主流程）；长文按上限分段全部发出；带轻量限流避免刷屏。
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

_FS_LIMIT = 4000          # 飞书 text 上限较宽，保守分段控可读性
_FS_CHUNK_GAP_S = 0.4


def feishu_sign(timestamp: int, secret: str) -> str:
    """飞书签名：base64( HMAC-SHA256(key=f"{ts}\n{secret}", msg=b"") )。纯函数，可测。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuNotifier:
    """飞书自定义机器人推送器。webhook_url 为空则禁用；有 secret 则带签名。"""

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

    def _payload(self, text: str) -> dict[str, Any]:
        """构造一条飞书 text 消息（有 secret 则带 timestamp+sign，时间戳取当前秒）。"""
        body: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        if self.secret:
            ts = int(time.time())   # 签名时间戳须为当前时间（飞书要求 1 小时内）
            body["timestamp"] = str(ts)
            body["sign"] = feishu_sign(ts, self.secret)
        return body

    async def send(self, text: str, now_ms: int = 0) -> bool:
        """完整推送：长文按 _FS_LIMIT 切成多条全部发出（不截断）。"""
        if not self.url:
            return False
        # 轻量限流：避免高频刷爆（仅限不同告警，同消息各分段顺序发）
        if now_ms and self._last_sent_ms and now_ms - self._last_sent_ms < self.min_interval_ms:
            return False
        chunks = split_message(text, _FS_LIMIT)
        if not chunks:
            return False
        n = len(chunks)
        ok_all = True
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout_sec)) as s:
                for i, chunk in enumerate(chunks):
                    if i:
                        await asyncio.sleep(_FS_CHUNK_GAP_S)
                    body_text = chunk if n == 1 else f"({i + 1}/{n})\n{chunk}"
                    payload = self._payload(body_text)
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
