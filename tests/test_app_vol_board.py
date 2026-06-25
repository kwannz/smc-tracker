"""_periodic_volatility_board 接线单测：opt-out 早返回 + gather 注册存在（不起网络）。"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.app import TradingSystem


def _fake(enabled: bool, sec: float):
    mc = types.SimpleNamespace(enabled=enabled, vol_board_sec=sec,
                               timeframes=["15m"])
    return types.SimpleNamespace(cfg=types.SimpleNamespace(monitored_coins=mc))


def test_optout_returns_immediately():
    """enabled=False 或 vol_board_sec<=0 → 立即返回（不进 while 循环）。"""
    asyncio.run(TradingSystem._periodic_volatility_board(_fake(False, 0.0)))
    asyncio.run(TradingSystem._periodic_volatility_board(_fake(True, 0.0)))  # 间隔 0 也关


def test_method_is_coroutine_and_registered():
    assert inspect.iscoroutinefunction(TradingSystem._periodic_volatility_board)
    src = inspect.getsource(TradingSystem)
    assert 'periodic_volatility_board"' in src  # gather 注册存在
