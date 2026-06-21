"""WS 客户端 handler 去重回归测试(防止 N 倍重复分发数据失真)。

历史 HIGH 缺陷：subscribe() 无条件 append handler，按币/址各订阅一次会把同一 bound method
注册 N 份 → 每条消息分发 N 次 → 净流向/成交累积器 N 倍失真。此测试锁定去重行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.bitget.ws_client import BitgetSub, BitgetWSClient
from smc_tracker.hyperliquid.ws_client import HyperliquidWSClient, Subscription


def test_hl_handler_dedup_across_coins():
    c = HyperliquidWSClient()
    calls = []

    def h(data, recv_ns):
        calls.append(data)

    for coin in ("BTC", "ETH", "SOL", "DOGE"):
        c.subscribe(Subscription(type="candle", coin=coin, interval="5m"), h)
    assert len(c._handlers["candle"]) == 1      # 同一 handler 仅 1 份
    assert len(c._subs) == 4                     # 4 个不同订阅仍保留


def test_hl_on_dedup():
    c = HyperliquidWSClient()

    def h(d, r):
        pass

    c.on("allMids", h)
    c.on("allMids", h)
    assert len(c._handlers["allMids"]) == 1


def test_bitget_handler_dedup_across_symbols():
    b = BitgetWSClient()

    def t(arg, data, recv_ns):
        pass

    for s in [f"C{i}USDT" for i in range(50)]:
        b.subscribe(BitgetSub(channel="ticker", inst_id=s), t)
    assert len(b._handlers["ticker"]) == 1       # 50 symbol 共用 1 份 handler
    assert len(b._subs) == 50


def test_hl_distinct_handlers_both_kept():
    c = HyperliquidWSClient()

    def h1(d, r):
        pass

    def h2(d, r):
        pass

    c.subscribe(Subscription(type="trades", coin="BTC"), h1)
    c.subscribe(Subscription(type="trades", coin="ETH"), h2)
    assert len(c._handlers["trades"]) == 2       # 不同 handler 各保留


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
