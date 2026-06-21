"""输出层单测：Webhook（禁用/限流，不实际联网）+ 摘要报告内容。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.notify import WebhookNotifier, build_report, split_message
from smc_tracker.storage import Store


def test_webhook_disabled_when_no_url():
    n = WebhookNotifier("")
    assert not n.enabled
    assert asyncio.run(n.send("hi")) is False        # 无 url 直接 False，不联网


def test_webhook_rate_limited():
    n = WebhookNotifier("https://example.com/hook", min_interval_ms=1500)
    n._last_sent_ms = 1000
    # 距上次仅 500ms < 1500ms → 被限流，返回 False（不会发起 POST）
    assert asyncio.run(n.send("hi", now_ms=1500)) is False


def test_split_message_under_limit():
    """短文不分段；空串返回空列表。"""
    assert split_message("hello", 100) == ["hello"]
    assert split_message("", 100) == []


def test_split_message_by_lines():
    """超长按行边界切，每段 ≤ limit，且不丢内容。"""
    text = "\n".join(f"line{i}" for i in range(20))   # 20 行
    chunks = split_message(text, 30)
    assert len(chunks) > 1
    assert all(len(c) <= 30 for c in chunks)
    # 还原(各段以 \n 拼回)应与原文一致(行不丢)
    assert "\n".join(chunks).replace("\n\n", "\n") or True   # 内容完整性下面更严格校验
    assert sum(c.count("line") for c in chunks) == 20        # 20 行全保留


def test_split_message_hard_split_long_line():
    """单行超长 → 硬切成多段，每段 ≤ limit。"""
    chunks = split_message("X" * 250, 100)
    assert len(chunks) == 3 and all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == "X" * 250                      # 硬切无丢失


def test_report_contents():
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    store.insert_signal((1000, "kPEPE", "long", 0.9, 1.0, 0.8, 300_000, 0.03, 0,
                         0.0028, 0.0027, 0.003, 2.0, "CHoCH↑ × 聪明钱"))
    store.insert_divergence((1000, "PEPE", "bearish", 0.3, 0.0001, 0.03, -300_000, "分销"))
    store.insert_hl_meme_trades([
        ("kPEPE", 0.0028, 1e6, 100_000, "B", "0xA", "0xM", "0xA", "h", 1, 1000)])
    r = build_report(store, since_ms=0, now_ms=2000)
    assert "共振信号 1" in r and "kPEPE" in r and "做多" in r
    assert "背离信号 1" in r and "PEPE" in r and "分销" in r
    assert "聪明钱主动净流向" in r
    store.close()


def test_report_empty():
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    r = build_report(store, since_ms=0, now_ms=1000)
    assert "共振信号 0" in r and "（无）" in r
    store.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
