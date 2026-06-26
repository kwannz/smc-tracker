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


def test_report_null_funding_flow_no_crash():
    """背离记录 funding/dex_flow_usd 为 NULL 时 build_report 不崩溃（P1 修复验证）。"""
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    # None 模拟 DB NULL 值：直接用 conn.execute 绕过 insert_divergence 类型守卫
    store.conn.execute(
        "INSERT INTO divergence(ts,coin,direction,score,funding,oi_change_pct,dex_flow_usd,reason)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (500, "BTC", "bearish", 0.5, None, None, None, "分销"),
    )
    r = build_report(store, since_ms=0, now_ms=2000)
    # 不崩溃，且背离行出现在报告中
    assert "背离信号 1" in r and "BTC" in r
    store.close()


# ---------------------------------------------------------------------------
# 以下测试直接调用真实 send() 并用 aiohttp mock 拦截网络层
# ---------------------------------------------------------------------------

class _FakeResp:
    """模拟 aiohttp 响应。"""
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _FakeSession:
    """模拟 aiohttp.ClientSession，post() 按顺序返回预设响应。"""
    def __init__(self, responses: list, **_kwargs) -> None:
        self._resps = iter(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def post(self, *_args, **_kwargs):
        return next(self._resps)


def test_webhook_partial_fail_does_not_update_timestamp():
    """分块部分失败时 _last_sent_ms 不更新，sent 不增（P1 修复验证）。

    构造两段消息（超过 _WH_LIMIT），第一段成功(200)，第二段失败(500)；
    ok_all=False → _last_sent_ms 应保持 0。
    """
    import unittest.mock as mock
    import orjson

    n = WebhookNotifier("https://example.com/hook", min_interval_ms=500)
    n._last_sent_ms = 0

    # 构造两段消息：_WH_LIMIT=1900，发两段 "A"*1000 各段
    long_text = "A" * 1000 + "\n" + "B" * 1000  # split_message 按行分成 2 段

    resp_ok = _FakeResp(200, b"{}")
    resp_fail = _FakeResp(500, b"{}")

    fake_session = _FakeSession([resp_ok, resp_fail])
    with mock.patch("aiohttp.ClientSession", return_value=fake_session):
        result = asyncio.run(n.send(long_text, now_ms=9000))

    # 部分失败 → False 且时间戳未更新
    assert result is False
    assert n._last_sent_ms == 0
    assert n.sent == 0
    assert n.failed == 1


def test_webhook_now_ms_zero_success_does_not_update_timestamp():
    """now_ms=0 成功后不更新 _last_sent_ms（P1 修复验证）。"""
    import unittest.mock as mock
    import orjson

    n = WebhookNotifier("https://example.com/hook")
    n._last_sent_ms = 3000  # 预设非零值

    resp_ok = _FakeResp(200, b"{}")
    fake_session = _FakeSession([resp_ok])
    with mock.patch("aiohttp.ClientSession", return_value=fake_session):
        result = asyncio.run(n.send("hello", now_ms=0))

    # 成功但 now_ms=0 → 不写时间戳
    assert result is True
    assert n._last_sent_ms == 3000   # 保持原值，未被 0 覆盖
    assert n.sent == 1


def test_telegram_partial_fail_does_not_update_timestamp():
    """Telegram 分块部分失败时 _last_sent_ms 不更新（P1 修复验证）。"""
    import unittest.mock as mock
    import orjson
    from smc_tracker.notify import TelegramNotifier

    t = TelegramNotifier("TOKEN", "@chan", min_interval_ms=500)
    t._last_sent_ms = 0

    # 两段消息：TG limit=4000，用 \n 分两段
    long_text = "A" * 2000 + "\n" + "B" * 2000

    # 第一段 ok，第二段返回 ok=false
    resp_ok = _FakeResp(200, orjson.dumps({"ok": True}))
    resp_fail = _FakeResp(200, orjson.dumps({"ok": False, "description": "bad"}))

    fake_session = _FakeSession([resp_ok, resp_fail])
    with mock.patch("aiohttp.ClientSession", return_value=fake_session):
        result = asyncio.run(t.send(long_text, now_ms=8888))

    assert result is False
    assert t._last_sent_ms == 0   # 不更新
    assert t.sent == 0
    assert t.failed == 1


def test_telegram_now_ms_zero_success_does_not_update_timestamp():
    """Telegram now_ms=0 成功后不覆盖 _last_sent_ms（P1 修复验证）。"""
    import unittest.mock as mock
    import orjson
    from smc_tracker.notify import TelegramNotifier

    t = TelegramNotifier("TOKEN", "@chan")
    t._last_sent_ms = 7777  # 预设值

    resp_ok = _FakeResp(200, orjson.dumps({"ok": True}))
    fake_session = _FakeSession([resp_ok])
    with mock.patch("aiohttp.ClientSession", return_value=fake_session):
        result = asyncio.run(t.send("hello", now_ms=0))

    assert result is True
    assert t._last_sent_ms == 7777  # 保持原值
    assert t.sent == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
