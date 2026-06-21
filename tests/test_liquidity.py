"""SMC 流动性引擎单测（扫荡/等高等低，合成数据无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle
from smc_tracker.smc.liquidity import LiquidityEngine


def _c(i, o, h, l, c):
    return Candle(coin="X", interval="1m", open_time_ms=i * 60000,
                  close_time_ms=i * 60000 + 59999, o=o, h=h, l=l, c=c, v=1, n=1)


def _feed(eng, bars):
    out = []
    for i, (o, h, l, c) in enumerate(bars):
        out.append(eng.update(_c(i, o, h, l, c)))
    return out


def test_bearish_sweep_of_bsl():
    eng = LiquidityEngine(lookback=2)
    bars = [
        (10, 11, 9, 10), (10, 12, 10, 11),
        (11, 15, 11, 14),    # idx2 摆动高 → BSL@15
        (14, 14, 12, 13), (13, 13.5, 11, 12),  # idx4 确认 SH@2
        (12, 16, 11.5, 13),  # idx5 刺破 16>15 但收 13<15 → 看跌扫荡
    ]
    out = _feed(eng, bars)
    sweeps = out[5]
    assert len(sweeps) == 1 and sweeps[0].direction == "bearish"
    assert sweeps[0].price == 15
    assert eng.bsl[0].swept


def test_bullish_sweep_of_ssl():
    eng = LiquidityEngine(lookback=2)
    bars = [
        (10, 11, 9, 10), (10, 10.5, 8, 9),
        (9, 10, 5, 6),       # idx2 摆动低 → SSL@5
        (6, 8, 6, 7), (7, 9, 6.5, 8),          # idx4 确认 SL@2
        (8, 8.5, 4, 7),      # idx5 刺破 4<5 但收 7>5 → 看涨扫荡
    ]
    out = _feed(eng, bars)
    sweeps = out[5]
    assert len(sweeps) == 1 and sweeps[0].direction == "bullish"
    assert sweeps[0].price == 5
    assert eng.ssl[0].swept


def test_breakout_is_not_sweep():
    """收在流动性外侧 = 突破(接受)，不算扫荡。"""
    eng = LiquidityEngine(lookback=2)
    bars = [
        (10, 11, 9, 10), (10, 12, 10, 11),
        (11, 15, 11, 14), (14, 14, 12, 13), (13, 13.5, 11, 12),  # SH@2=15
        (14, 16, 13.5, 15.5),  # 刺破 16>15 且收 15.5>15 → 突破，不是扫荡
    ]
    out = _feed(eng, bars)
    assert out[5] == []
    assert not eng.bsl[0].swept


def test_equal_highs_cluster():
    eng = LiquidityEngine(lookback=2, eq_tol_pct=0.005)
    bars = [
        (10, 11, 9, 10), (10, 12, 10, 11),
        (11, 15, 11, 14), (14, 14, 12, 13), (13, 13.5, 11, 12),   # SH@2=15
        (12, 14, 11, 13),
        (14, 15.02, 14, 15.0),   # idx6 第二个高 15.02，收 15.0(不触发扫荡)
        (14, 14.5, 13, 13.5), (13.5, 13.8, 12, 12.5),             # idx8 确认 SH@6
    ]
    _feed(eng, bars)
    # 两个 ~15 的摆动高合并为一个等高流动性
    assert len(eng.bsl) == 1
    assert eng.bsl[0].equal is True and not eng.bsl[0].swept


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
