"""HLOrderbookMonitor 挂单墙检测与动态、db roundtrip 单测（合成数据，无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor import HLOrderbookMonitor, detect_walls
from smc_tracker.monitor.orderbook_monitor import Subscription
from smc_tracker.storage.db import Store


def _lv(px: float, sz: float, n: int = 1) -> dict:
    """构造一档 l2Book 档位（px/sz 为字符串，仿真实推送）。"""
    return {"px": str(px), "sz": str(sz), "n": n}


class _FakeWS:
    """记录订阅的假 WS（不联网）。"""

    def __init__(self) -> None:
        self.subs: list[tuple] = []

    def subscribe(self, sub, handler) -> None:
        self.subs.append((sub, handler))


# ---- detect_walls 纯函数 ----
def test_detect_walls_big_wall_detected():
    # 9 档均匀小档(notional≈100*10=1000) + 1 档大墙(100*200=20000)
    levels = [_lv(100.0, 10.0) for _ in range(9)]
    levels.append(_lv(99.0, 200.0, n=5))   # 大墙：notional=19800
    walls = detect_walls(levels, mult=3.0)
    pxs = [w[0] for w in walls]
    assert 99.0 in pxs, walls
    # 验算：均值 = (9*1000 + 19800)/10 = 2880；阈值 3×=8640；19800≥8640 命中
    # 小档 notional=1000 < 8640 不命中
    assert all(p == 99.0 for p in pxs), walls
    # 返回元组 (px, notional, n)
    px, ntl, n = walls[0]
    assert px == 99.0 and abs(ntl - 19800.0) < 1e-6 and n == 5


def test_detect_walls_uniform_none():
    # 全部均匀 → 无任何档超 3× 均值 → []
    levels = [_lv(100.0, 10.0) for _ in range(20)]
    assert detect_walls(levels, mult=3.0) == []


def test_detect_walls_empty_and_zero_safe():
    assert detect_walls([]) == []
    assert detect_walls([_lv(0.0, 0.0), _lv(0.0, 0.0)]) == []


def test_detect_walls_sorted_desc():
    levels = [_lv(100.0, 1.0) for _ in range(8)]
    levels.append(_lv(99.0, 100.0))    # notional 9900
    levels.append(_lv(98.0, 200.0))    # notional 19600（更大）
    walls = detect_walls(levels, mult=3.0)
    # 按 notional 降序：98 在 99 之前
    assert [w[0] for w in walls] == [98.0, 99.0], walls


# ---- _on_l2book 动态 build/pull ----
def _frame(coin: str, bids: list[dict], asks: list[dict], t: int) -> dict:
    return {"coin": coin, "time": t, "levels": [bids, asks]}


def test_on_l2book_build_then_pull():
    events: list[dict] = []
    ws = _FakeWS()
    mon = HLOrderbookMonitor(["BTC"], ws, store=None,
                             on_wall_signal=events.append, min_wall_usd=200_000.0)
    mon.attach()
    # attach 注册了一个 l2Book 订阅
    assert len(ws.subs) == 1
    assert isinstance(ws.subs[0][0], Subscription)
    assert ws.subs[0][0].type == "l2Book" and ws.subs[0][0].coin == "BTC"

    # 第一帧：bid 侧有一个大墙（px=60000, sz=10 → notional=600k ≥ 均值3× 且 ≥ min_wall_usd）
    small_bids = [_lv(60000.0 - i, 0.001) for i in range(1, 19)]   # 18 档极小档
    big_bid = _lv(60000.0, 10.0, n=7)                              # 大墙 600k
    bids1 = [big_bid] + small_bids
    asks1 = [_lv(60100.0 + i, 0.001) for i in range(19)]
    mon._on_l2book(_frame("BTC", bids1, asks1, 1000), 0)
    builds = [e for e in events if e["kind"] == "build"]
    assert any(e["px"] == 60000.0 and e["side"] == "bid" for e in builds), events
    b = next(e for e in builds if e["px"] == 60000.0)
    assert abs(b["notional"] - 600_000.0) < 1e-3 and b["ts"] == 1000

    # 第二帧：该大墙消失（抽单）→ pull
    events.clear()
    bids2 = [_lv(60000.0, 0.001)] + small_bids   # 原墙价位变成极小档 = 墙抽走
    mon._on_l2book(_frame("BTC", bids2, asks1, 2000), 0)
    pulls = [e for e in events if e["kind"] == "pull"]
    assert any(e["px"] == 60000.0 and e["side"] == "bid" for e in pulls), events


def test_on_l2book_no_callback_still_updates_state():
    ws = _FakeWS()
    mon = HLOrderbookMonitor(["ETH"], ws, store=None,
                             on_wall_signal=None, min_wall_usd=100_000.0)
    small = [_lv(3000.0 - i, 0.01) for i in range(1, 19)]
    big = _lv(3000.0, 50.0)            # 150k 墙
    mon._on_l2book(_frame("ETH", [big] + small, [_lv(3001.0, 0.01)], 1), 0)
    # 无回调也更新状态
    walls = mon.all_walls()
    assert 3000.0 in walls["ETH"]["bid"], walls
    # 失衡也维护了
    imb = mon.book_imbalance("ETH")
    assert imb["bid_usd"] > imb["ask_usd"]


def test_on_l2book_malformed_safe():
    ws = _FakeWS()
    mon = HLOrderbookMonitor(["BTC"], ws)
    mon._on_l2book({"coin": "BTC", "time": 1, "levels": [[]]}, 0)   # levels 不足两侧
    mon._on_l2book({"time": 1, "levels": [[], []]}, 0)              # 无 coin
    assert mon.all_walls().get("BTC", {"bid": {}, "ask": {}})["bid"] == {}


# ---- db roundtrip ----
def test_db_orderbook_walls_roundtrip():
    store = Store(Path(tempfile.mkdtemp()) / "ob.db")
    try:
        rows = [
            (1000, "BTC", "bid", "build", 60000.0, 600_000.0),
            (1500, "BTC", "bid", "pull", 60000.0, 600_000.0),
            (2000, "ETH", "ask", "build", 3100.0, 250_000.0),
        ]
        store.insert_orderbook_walls(rows)
        got = store.recent_orderbook_walls(1200)   # 仅 ts>=1200 的两条
        assert len(got) == 2
        # 按 ts ASC
        assert got[0][0] == 1500 and got[0][3] == "pull"
        assert got[1][0] == 2000 and got[1][1] == "ETH" and got[1][2] == "ask"
        # 全量
        assert len(store.recent_orderbook_walls(0)) == 3
        assert store.count("hl_orderbook_walls") == 3
    finally:
        store.close()


def test_flush_with_store():
    store = Store(Path(tempfile.mkdtemp()) / "ob2.db")
    try:
        ws = _FakeWS()
        mon = HLOrderbookMonitor(["BTC"], ws, store=store,
                                 on_wall_signal=None, min_wall_usd=200_000.0)
        small = [_lv(60000.0 - i, 0.001) for i in range(1, 19)]
        big = _lv(60000.0, 10.0)   # 600k
        mon._on_l2book(_frame("BTC", [big] + small, [_lv(60100.0, 0.001)], 1000), 0)
        n = mon.flush()
        assert n == 1
        assert store.count("hl_orderbook_walls") == 1
        # 缓冲已清空
        assert mon.flush() == 0
    finally:
        store.close()


# ---- confirming_wall 新查询方法 ----

def _make_mon_with_walls(walls_data: dict) -> HLOrderbookMonitor:
    """构造带预填 _walls 的 monitor（无 WS 连接）。"""
    ws = _FakeWS()
    mon = HLOrderbookMonitor(["BTC", "ETH"], ws, store=None)
    for coin, sides in walls_data.items():
        for side, pxmap in sides.items():
            mon._walls[coin][side] = pxmap
    return mon


def test_confirming_wall_hit():
    """BTC bid px=100，查询 price=100.5，tol=1.5% → 命中，dist_pct 正确。"""
    mon = _make_mon_with_walls({"BTC": {"bid": {100.0: (2_000_000.0, 5)}, "ask": {}}})
    result = mon.confirming_wall("BTC", price=100.5, side="bid", tol_pct=0.015)
    assert result is not None, "应命中 px=100 的墙"
    assert result["px"] == 100.0
    assert abs(result["notional"] - 2_000_000.0) < 1e-6
    assert result["n"] == 5
    # dist_pct = |100 - 100.5| / 100.5 ≈ 0.004975
    expected_dist = abs(100.0 - 100.5) / 100.5
    assert abs(result["dist_pct"] - expected_dist) < 1e-8


def test_confirming_wall_too_far_returns_none():
    """墙距 price 超过 tol_pct → 返回 None。"""
    mon = _make_mon_with_walls({"BTC": {"bid": {80.0: (2_000_000.0, 3)}, "ask": {}}})
    # |80 - 100| / 100 = 20%，远超 1.5%
    result = mon.confirming_wall("BTC", price=100.0, side="bid", tol_pct=0.015)
    assert result is None


def test_confirming_wall_wrong_side_returns_none():
    """墙在 bid 侧，查询 ask 侧 → 返回 None。"""
    mon = _make_mon_with_walls({"BTC": {"bid": {100.0: (2_000_000.0, 3)}, "ask": {}}})
    result = mon.confirming_wall("BTC", price=100.0, side="ask", tol_pct=0.015)
    assert result is None


def test_confirming_wall_invalid_price_returns_none():
    """price <= 0 → 返回 None（防止零/负价格导致除零）。"""
    mon = _make_mon_with_walls({"BTC": {"bid": {100.0: (2_000_000.0, 3)}, "ask": {}}})
    assert mon.confirming_wall("BTC", price=0.0, side="bid") is None
    assert mon.confirming_wall("BTC", price=-5.0, side="bid") is None


def test_confirming_wall_unknown_coin_returns_none():
    """coin 未订阅/无墙数据 → 返回 None。"""
    mon = _make_mon_with_walls({})
    result = mon.confirming_wall("UNKNOWN", price=100.0, side="bid")
    assert result is None


def test_confirming_wall_picks_largest_notional():
    """多个 px 均在 tol 范围内，返回 notional 最大的那个。"""
    # price=100, tol=5%: px=97(dist=3%)、px=103(dist=3%) 均在范围内
    mon = _make_mon_with_walls({
        "ETH": {
            "ask": {
                103.0: (500_000.0, 2),   # 小墙
                97.0: (3_000_000.0, 8),  # 大墙（notional 更大）
            },
            "bid": {},
        }
    })
    result = mon.confirming_wall("ETH", price=100.0, side="ask", tol_pct=0.05)
    assert result is not None
    assert result["px"] == 97.0   # notional 最大的是 97.0
    assert result["notional"] == 3_000_000.0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
