"""可疑地址检测 + 标记 + 轨迹 单测（合成数据，无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import Config
from smc_tracker.monitor.meme_trade_monitor import MemeTradeMonitor
from smc_tracker.storage import Store


class _FakeWS:
    def subscribe(self, *a, **k):
        pass


def _store():
    return Store(Path(tempfile.mkdtemp()) / "s.db")


def _trade(side, px, sz, buyer, seller, t):
    return {"coin": "kPEPE", "side": side, "px": px, "sz": sz, "time": t,
            "users": [buyer, seller], "hash": "h", "tid": t}


def test_suspicious_detection_threshold():
    flagged = []
    mon = MemeTradeMonitor(["kPEPE"], _FakeWS(), _store(),
                           on_suspicious=lambda i: flagged.append(i),
                           suspicious_notional=50_000)
    # taker=0xAAA 净买 30k（未达阈值）
    mon._on_trades([_trade("B", 1.0, 30_000, "0xAAA", "0xM", 1)], 0)
    assert flagged == []
    # 再净买 30k → 累计 60k ≥ 50k → 上报
    mon._on_trades([_trade("B", 1.0, 30_000, "0xAAA", "0xM", 2)], 0)
    assert len(flagged) == 1
    assert flagged[0]["address"] == "0xAAA" and flagged[0]["direction"] == "buy"


def test_suspicious_net_cancels_out():
    """同地址先买后卖对冲 → 净额不足，不上报。"""
    flagged = []
    mon = MemeTradeMonitor(["kPEPE"], _FakeWS(), _store(),
                           on_suspicious=lambda i: flagged.append(i),
                           suspicious_notional=50_000)
    mon._on_trades([_trade("B", 1.0, 40_000, "0xBBB", "0xM", 1)], 0)   # +40k
    mon._on_trades([_trade("A", 1.0, 40_000, "0xM", "0xBBB", 2)], 0)   # 0xBBB 卖, -40k
    assert flagged == []


def test_flag_and_trajectory():
    s = _store()
    s.insert_hl_meme_trades([
        ("kPEPE", 1.0, 30_000, 30_000, "B", "0xAAA", "0xM", "0xAAA", "h1", 1, 1000),
        ("WIF", 2.0, 5_000, 10_000, "A", "0xM", "0xAAA", "0xM", "h2", 2, 2000),
    ])
    s.flag_address("0xAAA", 1000, "kPEPE", "激进净买", 30_000, promoted=1)
    assert s.is_flagged("0xAAA") and not s.is_flagged("0xZZZ")
    traj = s.address_trajectory("0xAAA")
    assert len(traj) == 2
    # 最近在前：t=2000 WIF SELL（被动），t=1000 kPEPE BUY（主动）
    assert traj[0][1] == "WIF" and traj[0][2] == "SELL" and traj[0][5] == 0
    assert traj[1][2] == "BUY" and traj[1][5] == 1
    s.close()


def test_app_promotes_suspicious():
    from smc_tracker.app import TradingSystem
    s = _store()
    app = TradingSystem(Config(), [], s, Path("."))
    app._on_suspicious({"address": "0xSUS", "coin": "kPEPE", "net_usd": 80_000,
                        "direction": "buy", "px": 1.0, "time_ms": 1000})
    assert s.is_flagged("0xSUS")
    assert any(w.address == "0xSUS" for w in app.cfg.watchlist)   # 已升级进 watchlist
    # 复现不重复升级（仍只有一个）
    n = len(app.cfg.watchlist)
    app._on_suspicious({"address": "0xSUS", "coin": "kPEPE", "net_usd": 90_000,
                        "direction": "buy", "px": 1.0, "time_ms": 2000})
    assert len(app.cfg.watchlist) == n
    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
