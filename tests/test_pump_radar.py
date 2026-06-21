"""暴涨暴跌实时预警单测（合成 K 线触发已验证规则，无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle
from smc_tracker.signals.pump_radar import PumpRadar, features


def _candles(closes, range_pct=0.04, vols=None):
    cs = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        v = vols[i] if vols else 1000.0
        cs.append(Candle(coin="X", interval="1h", open_time_ms=i * 3600000,
                         close_time_ms=i * 3600000 + 3599999,
                         o=o, h=max(o, c) * (1 + range_pct), l=min(o, c) * (1 - range_pct),
                         c=c, v=v, n=0))
    return cs


def test_pump_rsi_atr():
    closes = [100 * (1.03 ** i) for i in range(40)]      # 持续上涨 → RSI 高、宽幅 → ATR%高
    a = PumpRadar().evaluate("kPEPE", _candles(closes), 1000)
    assert a is not None and a.kind == "pump"


def test_dump_continuation():
    closes = [100 * (0.96 ** i) for i in range(40)]      # 持续下跌 → RSI<35 & ret24<-15%
    a = PumpRadar().evaluate("WIF", _candles(closes), 1000)
    assert a is not None and a.kind == "dump"


def test_blacklist_no_pump():
    closes = [100 * (1.03 ** i) for i in range(40)]
    a = PumpRadar().evaluate("PUMP", _candles(closes), 1000)   # 只跌型黑名单 → 不发暴涨
    assert a is None or a.kind != "pump"


def test_whitelist_lift_boost():
    closes = [100 * (1.03 ** i) for i in range(40)]
    a = PumpRadar().evaluate("MOODENG", _candles(closes), 1000)
    assert a is not None and a.lift >= 30        # 妖币 lift 翻倍(18×2=36)


def test_features_none_on_short():
    assert features(_candles([100, 101, 102])) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
