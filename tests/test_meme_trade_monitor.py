"""MemeTradeMonitor 单元测试（合成 trade，临时库，无网络）。

校验点：
  - buyer/seller/taker 归属正确（side='B' taker=买方；side='A' taker=卖方）；
  - notional = px*sz 正确；
  - flush 后 SQLite 行数正确，top_meme_takers 聚合正确（买正卖负）；
  - 内存净流向（per-coin / per-taker）累计正确。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.meme_trade_monitor import MemeTradeMonitor
from smc_tracker.storage import Store


class _FakeWS:
    """假 WS：只记录订阅，不联网。"""

    def __init__(self) -> None:
        self.subs: list = []

    def subscribe(self, sub, handler) -> None:
        self.subs.append((sub, handler))


def _store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


def _trade(coin, side, px, sz, buyer, seller, tid):
    return {
        "coin": coin, "side": side, "px": px, "sz": sz,
        "time": tid, "hash": f"h{tid}", "tid": tid,
        "users": [buyer, seller],
    }


def test_attach_subscribes_all_memes():
    ws = _FakeWS()
    s = _store()
    m = MemeTradeMonitor(["kPEPE", "DOGE", "WIF"], ws, s)
    m.attach()
    assert len(ws.subs) == 3
    coins = {sub.coin for sub, _ in ws.subs}
    assert coins == {"kPEPE", "DOGE", "WIF"}
    assert all(sub.type == "trades" for sub, _ in ws.subs)
    s.close()


def test_buyer_side_B_taker_is_buyer():
    """side='B'：taker 主动买，taker = users[0] = 买方。"""
    ws = _FakeWS()
    s = _store()
    m = MemeTradeMonitor(["kPEPE"], ws, s)
    # px=0.001, sz=100000 → notional=100
    m._on_trades([_trade("kPEPE", "B", 0.001, 100_000, "0xBUYER", "0xSELLER", 1)], 0)
    assert m.trades_seen == 1
    row = m._buffer[0]
    # row 顺序 = (coin,px,sz,notional,taker_side,buyer,seller,taker,hash,tid,time_ms)
    assert row[0] == "kPEPE"
    assert abs(row[3] - 100.0) < 1e-9          # notional = px*sz
    assert row[4] == "B"                        # taker_side
    assert row[5] == "0xBUYER"                  # buyer = users[0]
    assert row[6] == "0xSELLER"                 # seller = users[1]
    assert row[7] == "0xBUYER"                  # taker = 买方
    # 净流向：taker 主动买 → coin +100
    assert abs(m.coin_net("kPEPE") - 100.0) < 1e-9
    s.close()


def test_seller_side_A_taker_is_seller():
    """side='A'：taker 主动卖，taker = users[1] = 卖方。"""
    ws = _FakeWS()
    s = _store()
    m = MemeTradeMonitor(["DOGE"], ws, s)
    # px=0.1, sz=2000 → notional=200
    m._on_trades([_trade("DOGE", "A", 0.1, 2000, "0xBUYER", "0xSELLER", 2)], 0)
    row = m._buffer[0]
    assert abs(row[3] - 200.0) < 1e-9
    assert row[4] == "A"
    assert row[5] == "0xBUYER"                  # buyer 仍是 users[0]
    assert row[6] == "0xSELLER"
    assert row[7] == "0xSELLER"                 # taker = 卖方
    # 净流向：taker 主动卖 → coin -200
    assert abs(m.coin_net("DOGE") - (-200.0)) < 1e-9
    s.close()


def test_flush_persists_and_store_aggregate():
    ws = _FakeWS()
    s = _store()
    m = MemeTradeMonitor(["kPEPE"], ws, s)
    # 0xA 主动买 100，再主动买 50；0xB 主动卖 200
    trades = [
        _trade("kPEPE", "B", 0.001, 100_000, "0xA", "0xM", 1),  # notional 100, taker 0xA
        _trade("kPEPE", "B", 0.001, 50_000, "0xA", "0xM", 2),   # notional 50,  taker 0xA
        _trade("kPEPE", "A", 0.001, 200_000, "0xM", "0xB", 3),  # notional 200, taker 0xB
    ]
    m._on_trades(trades, 0)
    assert len(m._buffer) == 3
    n = m.flush()
    assert n == 3
    assert len(m._buffer) == 0
    assert s.count("hl_meme_trades") == 3

    # store 权威 per-(coin,addr) 聚合：0xA 净 +150，0xB 净 -200（验证 taker/side 解析→落库）
    d = dict(s.top_meme_takers("kPEPE", since_ms=0, limit=10))
    assert abs(d["0xA"] - 150.0) < 1e-9
    assert abs(d["0xB"] - (-200.0)) < 1e-9

    # 内存 per-coin 净流向
    assert abs(m.coin_net("kPEPE") - (-50.0)) < 1e-9   # +100+50-200
    s.close()


def test_maybe_flush_threshold():
    ws = _FakeWS()
    s = _store()
    m = MemeTradeMonitor(["WIF"], ws, s, flush_threshold=3)
    # 喂 2 条 → 未达阈值，不落库
    m._on_trades([
        _trade("WIF", "B", 1.0, 10, "0xA", "0xM", 1),
        _trade("WIF", "B", 1.0, 10, "0xA", "0xM", 2),
    ], 0)
    assert s.count("hl_meme_trades") == 0
    assert len(m._buffer) == 2
    # 第 3 条 → 累积到达阈值（3 条在缓冲），热路径不再自动 flush（由周期 _periodic_flush 驱动）
    m._on_trades([_trade("WIF", "A", 1.0, 10, "0xM", "0xB", 3)], 0)
    assert len(m._buffer) == 3          # 3 条仍在缓冲
    # 显式 flush 后落库
    n = m.flush()
    assert n == 3
    assert s.count("hl_meme_trades") == 3
    assert len(m._buffer) == 0
    s.close()


def test_large_trade_callback():
    ws = _FakeWS()
    s = _store()
    captured: list[dict] = []
    m = MemeTradeMonitor(
        ["kPEPE"], ws, s, large_notional_usd=10_000.0,
        on_trade=lambda rec: captured.append(rec),
    )
    # 小单（notional=100）不回调，大单（notional=20000）回调
    m._on_trades([
        _trade("kPEPE", "B", 0.001, 100_000, "0xA", "0xM", 1),       # 100
        _trade("kPEPE", "B", 0.2, 100_000, "0xWHALE", "0xM", 2),     # 20000
    ], 0)
    assert m.large_trades_seen == 1
    assert len(captured) == 1
    assert captured[0]["taker"] == "0xWHALE"
    assert abs(captured[0]["notional"] - 20_000.0) < 1e-9
    s.close()


def test_skips_invalid_trades():
    ws = _FakeWS()
    s = _store()
    m = MemeTradeMonitor(["kPEPE"], ws, s)
    # px=0 / sz=0 / 无 coin → 全部跳过
    m._on_trades([
        {"coin": "kPEPE", "side": "B", "px": 0, "sz": 100, "time": 1, "users": ["0xA", "0xM"]},
        {"coin": "kPEPE", "side": "B", "px": 0.1, "sz": 0, "time": 2, "users": ["0xA", "0xM"]},
        {"coin": "", "side": "B", "px": 0.1, "sz": 100, "time": 3, "users": ["0xA", "0xM"]},
    ], 0)
    assert m.trades_seen == 0
    assert len(m._buffer) == 0
    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
