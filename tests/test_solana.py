"""Solana 供应量监控单测（detect_change 纯逻辑 + 监控 diff，假 RPC 无网络）。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.onchain.solana import SolanaSupplyMonitor, detect_change
from smc_tracker.storage import Store


def test_detect_change():
    assert detect_change(None, 100, 0.005) is None        # 无历史
    assert detect_change(0, 100, 0.005) is None            # 无效历史
    assert detect_change(100, 100.2, 0.005) is None        # 0.2% < 阈值
    pct, kind = detect_change(100, 110, 0.005)             # +10% 增发
    assert kind == "mint" and abs(pct - 0.10) < 1e-9
    pct, kind = detect_change(100, 90, 0.005)              # -10% 销毁
    assert kind == "burn" and abs(pct + 0.10) < 1e-9


class _FakeRPC:
    """按调用次数返回不同供应量，模拟 mint/burn。"""
    def __init__(self, supplies):
        self._s = list(supplies)
        self._i = 0

    async def token_supply(self, session, mint):
        v = self._s[min(self._i, len(self._s) - 1)]
        self._i += 1
        return (v, 6)


def test_supply_monitor_detects_mint():
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    store.upsert_contract("BONK", "SOL", "MINT123", 1)
    store.upsert_contract("DOGE", "ETH", "0xabc", 1)   # 非 SOL，应被忽略
    mon = SolanaSupplyMonitor(store, rpc=_FakeRPC([1000.0, 1100.0]), min_change_pct=0.01)
    assert [c for (c, m) in mon.sol_mints()] == ["BONK"]   # 只取 SOL

    # 第一次：建基线，无变化事件
    ch1 = asyncio.run(mon.poll_once(now_ms=1000, session=object()))
    assert ch1 == []
    assert store.count("sol_supply") == 1

    # 第二次：1000→1100 = +10% 增发 → mint 事件
    ch2 = asyncio.run(mon.poll_once(now_ms=2000, session=object()))
    assert len(ch2) == 1 and ch2[0].kind == "mint"
    assert abs(ch2[0].pct - 0.10) < 1e-9
    assert store.count("sol_supply") == 2
    store.close()


if __name__ == "__main__":
    test_detect_change()
    test_supply_monitor_detects_mint()
    print("✅ 全部通过")
