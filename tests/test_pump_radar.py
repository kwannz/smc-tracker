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


def test_whitelist_lift_boost_separated():
    """修审计P2:lift 存实测基值,妖币×2 走独立 boost 字段(不再把×2折进 lift 冒充历史 lift)。"""
    closes = [100 * (1.03 ** i) for i in range(40)]
    a = PumpRadar().evaluate("MOODENG", _candles(closes), 1000)
    assert a is not None
    assert a.lift < 30 and a.boost == 2.0        # lift=实测基值(18)未翻倍, boost 单独=2.0
    assert "妖币×2" in a.fmt()                    # fmt 诚实标注加权(先验非实测)


def test_fmt_relabels_span_and_caveats_offcalib_tf():
    """修审计P1:喂 5m K 线(非 1h 标定)→ fmt 真实跨度=2h(非24h)+ hr/lift 标注「本TF未验证」。"""
    closes = [100 * (1.03 ** i) for i in range(40)]
    cs = _candles(closes)
    for i, c in enumerate(cs):                    # 改成 5m 周期(open_time_ms 间隔 300000)
        c.open_time_ms = i * 300_000
    a = PumpRadar().evaluate("kPEPE", cs, 1000)
    assert a is not None
    txt = a.fmt()
    assert "近24根(2h)" in txt                     # 24根×5m=2h,非硬编码 24h
    assert "未验证" in txt and "24h=" not in txt    # 命中率/lift 标注未验证;不再谎称 24h


def test_features_none_on_short():
    assert features(_candles([100, 101, 102])) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
