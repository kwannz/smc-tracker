"""AddressMonitor 分类与净流向单元测试（合成数据，无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import WatchAddress
from smc_tracker.monitor import AddressMonitor, EventType
from smc_tracker.monitor.address_monitor import _classify


ADDR = "0xABC0000000000000000000000000000000000001"


def _fill(side: str, sz: float, px: float, start: float, t: int = 0):
    return {"coin": "BTC", "side": side, "sz": str(sz), "px": str(px),
            "startPosition": str(start), "dir": "", "closedPnl": "0",
            "hash": "0x", "oid": t, "crossed": True, "time": t}


def _feed(mon: AddressMonitor, fills: list[dict], snapshot=False):
    mon._on_fills({"user": ADDR, "isSnapshot": snapshot, "fills": fills}, 0)


def test_classify_pure():
    assert _classify(0, 1) is EventType.OPEN
    assert _classify(1, 2) is EventType.ADD
    assert _classify(2, 1) is EventType.REDUCE
    assert _classify(1, 0) is EventType.CLOSE
    assert _classify(1, -1) is EventType.FLIP
    assert _classify(-2, -3) is EventType.ADD     # 加空
    assert _classify(-1, 0) is EventType.CLOSE    # 平空


def test_lifecycle_events():
    events = []
    mon = AddressMonitor([WatchAddress(ADDR, "whale")], ws=_FakeWS(),
                         on_event=events.append)
    # 建多 0->1
    _feed(mon, [_fill("B", 1.0, 100.0, 0.0)])
    # 加多 1->1.5
    _feed(mon, [_fill("B", 0.5, 110.0, 1.0)])
    # 减多 1.5->0.5
    _feed(mon, [_fill("A", 1.0, 120.0, 1.5)])
    # 平多 0.5->0
    _feed(mon, [_fill("A", 0.5, 130.0, 0.5)])
    # 反手做空 0 -> -1（一笔卖 1）
    _feed(mon, [_fill("A", 1.0, 125.0, 0.0)])     # 从 0 开空
    types = [e.type for e in events]
    assert types == [EventType.OPEN, EventType.ADD, EventType.REDUCE,
                     EventType.CLOSE, EventType.OPEN], types
    assert events[0].label == "whale"
    # 仓位追踪正确
    assert mon.position(ADDR, "BTC") == -1.0


def test_flip_event():
    events = []
    mon = AddressMonitor([WatchAddress(ADDR, "w")], ws=_FakeWS(), on_event=events.append)
    _feed(mon, [_fill("B", 1.0, 100.0, 0.0)])     # 多 1
    _feed(mon, [_fill("A", 3.0, 100.0, 1.0)])     # 卖 3 -> -2，跨零反手
    assert events[-1].type is EventType.FLIP
    assert mon.position(ADDR, "BTC") == -2.0


def test_net_flow():
    mon = AddressMonitor([WatchAddress(ADDR, "w")], ws=_FakeWS(), on_event=lambda e: None)
    _feed(mon, [_fill("B", 1.0, 100.0, 0.0)])     # +100
    _feed(mon, [_fill("A", 0.5, 200.0, 1.0)])     # -100
    assert abs(mon.net_flow("BTC") - 0.0) < 1e-9
    _feed(mon, [_fill("B", 2.0, 100.0, 0.5)])     # +200
    assert abs(mon.net_flow("BTC") - 200.0) < 1e-9


def test_snapshot_seeds_no_alert():
    events = []
    mon = AddressMonitor([WatchAddress(ADDR, "w")], ws=_FakeWS(), on_event=events.append)
    _feed(mon, [_fill("B", 2.0, 100.0, 0.0)], snapshot=True)   # 历史回放
    assert events == []                            # snapshot 不告警
    assert mon.position(ADDR, "BTC") == 2.0        # 但已播种仓位
    _feed(mon, [_fill("B", 1.0, 100.0, 2.0)])      # 实时加仓 2->3
    assert events[-1].type is EventType.ADD
    assert mon.position(ADDR, "BTC") == 3.0


def test_spot_fills_ignored():
    """现货成交 coin='@107' 不应进入永续分类（审计 E 项发现）。"""
    events = []
    mon = AddressMonitor([WatchAddress(ADDR, "w")], ws=_FakeWS(), on_event=events.append)
    mon._on_fills({"user": ADDR, "isSnapshot": False, "fills": [
        {"coin": "@107", "side": "B", "sz": "1", "px": "100", "startPosition": "0",
         "dir": "Buy", "closedPnl": "0", "hash": "0x", "oid": 1, "crossed": True, "time": 1}
    ]}, 0)
    assert events == []
    assert mon.position(ADDR, "@107") == 0.0
    assert mon.net_flow("@107") == 0.0
    # 永续成交仍正常处理
    _feed(mon, [_fill("B", 1.0, 100.0, 0.0)])
    assert events[-1].type is EventType.OPEN


class _FakeWS:
    def subscribe(self, *a, **k):
        pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
