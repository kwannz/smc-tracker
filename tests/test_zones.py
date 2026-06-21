"""SMC 区域引擎单测（FVG/OB/回补/溢价折价，合成数据无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle
from smc_tracker.smc.zones import ZoneEngine, premium_discount, in_ote


def _c(i, o, h, l, c):
    return Candle(coin="X", interval="1m", open_time_ms=i * 60000,
                  close_time_ms=i * 60000 + 59999, o=o, h=h, l=l, c=c, v=1, n=1)


def _feed(eng, bars):
    """bars: list of (o,h,l,c)。返回每根 update 的新区列表。"""
    out = []
    for i, (o, h, l, c) in enumerate(bars):
        out.append(eng.update(_c(i, o, h, l, c)))
    return out


def test_bullish_fvg_and_ob():
    eng = ZoneEngine()
    bars = [
        (12, 12.5, 10, 10.5),    # idx0 阴线 → 看涨 OB
        (10.5, 11, 10, 10.8),    # idx1 = a，high=11
        (10.8, 14, 10.8, 13.5),  # idx2 = b 位移阳线
        (13.5, 15, 12, 14),      # idx3 = c，low=12 > high[a]=11 → 看涨 FVG
    ]
    new = _feed(eng, bars)
    fvgs = [z for z in new[3] if z.kind == "FVG"]
    obs = [z for z in new[3] if z.kind == "OB"]
    assert len(fvgs) == 1 and fvgs[0].direction == "bull"
    assert fvgs[0].bottom == 11 and fvgs[0].top == 12
    assert len(obs) == 1 and obs[0].direction == "bull"
    assert obs[0].index == 0 and obs[0].bottom == 10 and obs[0].top == 12.5


def test_bearish_fvg_and_ob():
    eng = ZoneEngine()
    bars = [
        (10, 12, 9.5, 11.5),     # idx0 阳线 → 看跌 OB
        (11, 11.5, 11, 11),      # idx1 = a (doji)，low=11
        (11, 11, 7, 7.5),        # idx2 = b 位移阴线
        (7.5, 9, 6.5, 7),        # idx3 = c，high=9 < low[a]=11 → 看跌 FVG
    ]
    new = _feed(eng, bars)
    fvgs = [z for z in new[3] if z.kind == "FVG"]
    obs = [z for z in new[3] if z.kind == "OB"]
    assert len(fvgs) == 1 and fvgs[0].direction == "bear"
    assert fvgs[0].bottom == 9 and fvgs[0].top == 11
    assert len(obs) == 1 and obs[0].direction == "bear" and obs[0].index == 0


def test_fvg_mitigation():
    eng = ZoneEngine()
    bars = [
        (12, 12.5, 10, 10.5), (10.5, 11, 10, 10.8),
        (10.8, 14, 10.8, 13.5), (13.5, 15, 12, 14),   # 看涨 FVG [11,12] @idx3
        (14, 14.5, 11.5, 13),                          # idx4 回落 low=11.5 ≤ top=12 → 回补
    ]
    _feed(eng, bars)
    bull_fvg = next(z for z in eng.fvgs if z.direction == "bull")
    assert bull_fvg.mitigated and bull_fvg.mitigated_at == 4
    # OB 顶 12.5，也被 11.5 触及 → 回补
    assert all(z.mitigated for z in eng.obs)
    # 未回补区应为空
    assert eng.active_zones() == []


def test_no_fvg_when_overlap():
    eng = ZoneEngine()
    bars = [(10, 11, 9, 10.5), (10.5, 11.2, 9.8, 10.2), (10.2, 11.5, 9.5, 11)]
    new = _feed(eng, bars)
    assert all(z.kind != "FVG" for row in new for z in row)


def test_min_gap_filter():
    eng = ZoneEngine(min_gap_pct=0.01)   # 缺口需 ≥1%
    bars = [(12, 12.5, 10, 10.5), (10.5, 11, 10, 10.8),
            (10.8, 14, 10.8, 13.5), (13.5, 15, 11.05, 14)]  # 缺口 11→11.05 仅 0.45%
    new = _feed(eng, bars)
    assert all(z.kind != "FVG" for row in new for z in row)   # 被过滤


def test_premium_discount_and_ote():
    assert premium_discount(80, 100, 0) == "premium"
    assert premium_discount(20, 100, 0) == "discount"
    assert premium_discount(50, 100, 0) == "equilibrium"
    assert in_ote(30, 100, 0, "bull") is True       # OTE 区 [21,38]
    assert in_ote(50, 100, 0, "bull") is False
    assert in_ote(70, 100, 0, "bear") is True       # OTE 区 [62,79]


def test_zone_at_lookup():
    eng = ZoneEngine()
    bars = [(12, 12.5, 10, 10.5), (10.5, 11, 10, 10.8),
            (10.8, 14, 10.8, 13.5), (13.5, 15, 12, 14)]
    _feed(eng, bars)
    z = eng.zone_at(11.5, "bull")    # 落在 FVG[11,12] 或 OB[10,12.5]
    assert z is not None and z.direction == "bull"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
