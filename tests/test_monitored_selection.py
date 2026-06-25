"""监控清单选币决策纯函数单测（闭合审计 P2-6：enabled 分支行为）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import (
    UniverseCfg,
    select_base_universe,
    harmonic_extra_coins,
    collect_timeframes,
)

_BASE = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
_TK = {"BTCUSDT": {"quoteVolume": "900"}, "ETHUSDT": {"quoteVolume": "500"},
       "SOLUSDT": {"quoteVolume": "100"}}


def test_enabled_uses_only_watchlist():
    """enabled=True：基集只含清单内币（不是全市场）。"""
    out = select_base_universe(True, {"BTC": "BTCUSDT"}, _BASE, _TK, UniverseCfg(mode="all"))
    assert out == {"BTC": "BTCUSDT"}


def test_disabled_uses_universe_cfg():
    """enabled=False：走 universe_cfg（mode=all → 全部）。"""
    out = select_base_universe(False, {}, _BASE, _TK, UniverseCfg(mode="all"))
    assert set(out) == {"BTC", "ETH", "SOL"}


def test_harmonic_extra_empty_when_enabled():
    """enabled=True：谐波不再并入 harmonic_collected（清单已是基集）。"""
    assert harmonic_extra_coins(True, {"OLD": "OLDUSDT"}, {"NEW": "NEWUSDT"}) == {}


def test_harmonic_extra_union_when_disabled():
    """enabled=False：并入 harmonic_collected ∪ monitored（修 discover 真相源回归）。"""
    out = harmonic_extra_coins(False, {"OLD": "OLDUSDT"}, {"NEW": "NEWUSDT"})
    assert out == {"OLD": "OLDUSDT", "NEW": "NEWUSDT"}


def test_collect_tfs_union_when_enabled():
    """enabled=True：采集周期取 monitored∪bb∪harm 并集去重（谐波 30m + 用户 6H 都在）。"""
    out = collect_timeframes(True, ["15m", "6H"], ["15m", "1H"], ["15m", "30m"])
    assert out == ["15m", "6H", "1H", "30m"]
    assert "30m" in out and "6H" in out


def test_collect_tfs_excludes_monitored_when_disabled():
    """enabled=False：只 bb∪harm，不含 monitored timeframes。"""
    out = collect_timeframes(False, ["6H"], ["15m"], ["1H"])
    assert out == ["15m", "1H"]
    assert "6H" not in out
