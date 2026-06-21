"""Telegram + 多渠道推送单测（不联网）。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import Config, TelegramCfg
from smc_tracker.notify import MultiNotifier, TelegramNotifier, build_notifier


def test_telegram_disabled_without_creds():
    assert not TelegramNotifier("", "").enabled
    assert not TelegramNotifier("TOKEN", "").enabled        # 缺 chat_id
    assert TelegramNotifier("TOKEN", "@chan").enabled
    assert asyncio.run(TelegramNotifier("", "").send("hi")) is False


def test_telegram_rate_limited():
    t = TelegramNotifier("TOKEN", "@chan", min_interval_ms=1500)
    t._last_sent_ms = 1000
    assert asyncio.run(t.send("hi", now_ms=1500)) is False   # 限流，不联网


class _Fake:
    def __init__(self, enabled, result):
        self.enabled = enabled
        self._r = result
        self.calls = 0

    async def send(self, text, now_ms=0):
        self.calls += 1
        return self._r


def test_multi_only_sends_enabled():
    a, b, c = _Fake(True, True), _Fake(False, True), _Fake(True, False)
    m = MultiNotifier([a, b, c])
    assert m.channels == 2 and m.enabled
    assert asyncio.run(m.send("hi", 1)) is True              # a 成功
    assert a.calls == 1 and b.calls == 0 and c.calls == 1    # 禁用的 b 不发


def test_build_notifier_from_config():
    cfg = Config()
    cfg.telegram = TelegramCfg(bot_token="T", chat_id="@c")
    n = build_notifier(cfg)
    assert n.channels == 1                                   # 仅 Telegram(webhook 空)
    cfg.output.webhook_url = "https://x/h"
    assert build_notifier(cfg).channels == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
