"""HyperliquidInfo._post 429 限流退避重试单测（#189，注入 fake session，无网络）。

生产 discover/address/监控全走 HL info API,遇 429 须退避重试而非静默丢数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.hyperliquid.info_client import HyperliquidInfo


class _FakeResp:
    def __init__(self, status: int, body: bytes = b"{}", headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeSession:
    """按序返回预设响应；记录调用次数。post() 返回 async CM。"""
    def __init__(self, responses: list):
        self._responses = responses
        self.calls = 0

    def post(self, *a, **k):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_s):
        return None
    monkeypatch.setattr("smc_tracker.hyperliquid.info_client.asyncio.sleep", _instant)


@pytest.mark.asyncio
async def test_post_retries_on_429_then_succeeds():
    """429 → 退避重试 → 200 成功(不抛、不丢数据)。"""
    cli = HyperliquidInfo()
    cli._session = _FakeSession([_FakeResp(429), _FakeResp(429),
                                 _FakeResp(200, b'{"ok":1}')])
    out = await cli._post({"type": "meta"})
    assert out == {"ok": 1}
    assert cli._session.calls == 3            # 重试 2 次后成功


@pytest.mark.asyncio
async def test_post_succeeds_first_try_no_retry():
    cli = HyperliquidInfo()
    cli._session = _FakeSession([_FakeResp(200, b'{"v":2}')])
    out = await cli._post({"type": "meta"})
    assert out == {"v": 2} and cli._session.calls == 1


@pytest.mark.asyncio
async def test_post_raises_after_exhausting_retries():
    """持续 429 → 重试用尽后抛(不静默成功)。"""
    cli = HyperliquidInfo()
    cli._session = _FakeSession([_FakeResp(429)] * 10)
    with pytest.raises(Exception):
        await cli._post({"type": "meta"})
    assert cli._session.calls >= 3            # 至少重试到上限
