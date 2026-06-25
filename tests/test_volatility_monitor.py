"""实时波动追踪 VolatilityMonitor + vol_metrics + pdarray 单测（合成数据，确定性）。

设计：每个周期独立展示指标（不做跨周期共振合并）；PDArray=ICT 溢价/折价数组。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.volatility_monitor import (
    vol_metrics, pdarray, VolatilityMonitor,
)


def _ohlc(closes):
    """由收盘价构造 (o,h,l,c)：o=前收，h/l=±0（聚焦收盘动力学）。"""
    o = [closes[0]] + closes[:-1]
    h = [max(a, b) for a, b in zip(o, closes)]
    l = [min(a, b) for a, b in zip(o, closes)]
    return o, h, l, closes


# ---- vol_metrics 纯函数 ----

def test_flat_prices_near_zero_vol():
    o, h, l, c = _ohlc([100.0] * 30)
    m = vol_metrics(o, h, l, c)
    assert abs(m["rv"]) < 1e-6
    assert abs(m["velocity"]) < 1e-6
    assert abs(m["accel"]) < 1e-6


def test_uptrend_positive_velocity():
    o, h, l, c = _ohlc([100.0 + i for i in range(30)])
    m = vol_metrics(o, h, l, c)
    assert m["velocity"] > 0


def test_acceleration_positive_on_convex_up():
    o, h, l, c = _ohlc([100.0 + i * i * 0.1 for i in range(30)])
    m = vol_metrics(o, h, l, c)
    assert m["accel"] > 0


def test_too_few_bars_returns_empty():
    assert vol_metrics([1.0], [1.0], [1.0], [1.0]) == {}


# ---- pdarray 纯函数（ICT 溢价/折价）----

def test_pdarray_premium_near_high():
    # 价在区间顶部 → 溢价区，pd_pct≈1
    h = [100.0 + i for i in range(30)]
    l = [99.0 + i for i in range(30)]
    c = [100.0 + i for i in range(30)]  # 末值≈区间高
    pd = pdarray(h, l, c)
    assert pd["pd_zone"] == "溢价"
    assert pd["pd_pct"] > 0.8


def test_pdarray_discount_near_low():
    h = [100.0 - i * 0 + 1 for i in range(30)]  # 高位平
    l = [1.0] * 30
    c = [2.0] * 29 + [1.0]  # 末值在区间底
    pd = pdarray(h, l, c)
    assert pd["pd_zone"] == "折价"
    assert pd["pd_pct"] < 0.2


def test_pdarray_equilibrium_zero_range():
    pd = pdarray([5.0] * 10, [5.0] * 10, [5.0] * 10)
    assert pd["pd_zone"] == "均衡"


# ---- VolatilityMonitor（逐周期）----

class _FakeCandle:
    __slots__ = ("o", "h", "l", "c")
    def __init__(self, o, h, l, c):
        self.o, self.h, self.l, self.c = o, h, l, c


class _FakeStore:
    def __init__(self, series):
        self._series = series  # {(coin,tf): [(o,h,l,c), ...]} 或 {coin: [...]}（所有 tf 共用）

    def get_candles(self, coin, tf, limit=1000):
        data = self._series.get((coin, tf), self._series.get(coin, []))
        return [_FakeCandle(*x) for x in data]


def test_rank_orders_by_movement():
    mover = [(c, c, c, c) for c in [100.0 + i * i * 0.2 for i in range(30)]]
    calm = [(100.0, 100.0, 100.0, 100.0)] * 30
    mon = VolatilityMonitor({"MOVER": "MOVERUSDT", "CALM": "CALMUSDT"},
                            ["15m"], _FakeStore({"MOVER": mover, "CALM": calm}))
    rows = mon.rank(0)
    assert rows[0]["coin"] == "MOVER"
    assert rows[0]["score"] > rows[-1]["score"]


def test_per_tf_block_shows_each_tf():
    """每个周期独立展示：render 含所有配置周期标签 + PD 指标。"""
    up = [(c, c, c, c) for c in [100.0 + i for i in range(30)]]
    store = _FakeStore({("BTC", "15m"): up, ("BTC", "1H"): up})
    mon = VolatilityMonitor({"BTC": "BTCUSDT"}, ["15m", "1H"], store)
    rows = mon.rank(0)
    assert set(rows[0]["by_tf"].keys()) == {"15m", "1H"}
    card = mon.render(rows, 0)
    assert "15m" in card and "1H" in card and "PD" in card


def test_rank_skips_insufficient_data():
    mon = VolatilityMonitor({"X": "XUSDT"}, ["15m"], _FakeStore({"X": [(1.0, 1.0, 1.0, 1.0)]}))
    assert mon.rank(0) == []
