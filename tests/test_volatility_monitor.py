"""实时波动追踪 VolatilityMonitor + vol_metrics 单测（合成数据，确定性）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.volatility_monitor import vol_metrics, VolatilityMonitor


# ---- vol_metrics 纯函数 ----

def _ohlc(closes):
    """由收盘价构造 (o,h,l,c)：o=前收，h/l=±0（聚焦收盘动力学）。"""
    o = [closes[0]] + closes[:-1]
    h = [max(a, b) for a, b in zip(o, closes)]
    l = [min(a, b) for a, b in zip(o, closes)]
    return o, h, l, closes


def test_flat_prices_near_zero_vol():
    o, h, l, c = _ohlc([100.0] * 30)
    m = vol_metrics(o, h, l, c)
    assert abs(m["rv"]) < 1e-6
    assert abs(m["velocity"]) < 1e-6
    assert abs(m["accel"]) < 1e-6


def test_uptrend_positive_velocity():
    o, h, l, c = _ohlc([100.0 + i for i in range(30)])  # 线性上行
    m = vol_metrics(o, h, l, c)
    assert m["velocity"] > 0


def test_acceleration_positive_on_convex_up():
    o, h, l, c = _ohlc([100.0 + i * i * 0.1 for i in range(30)])  # 凸加速上行
    m = vol_metrics(o, h, l, c)
    assert m["accel"] > 0


def test_too_few_bars_returns_empty():
    assert vol_metrics([1.0], [1.0], [1.0], [1.0]) == {}


# ---- VolatilityMonitor ----

class _FakeCandle:
    __slots__ = ("o", "h", "l", "c")
    def __init__(self, o, h, l, c):
        self.o, self.h, self.l, self.c = o, h, l, c


class _FakeStore:
    def __init__(self, series):
        self._series = series  # {coin: [(o,h,l,c), ...]}

    def get_candles(self, coin, tf, limit=1000):
        return [_FakeCandle(*x) for x in self._series.get(coin, [])]


def test_rank_orders_by_movement():
    # MOVER 强加速上行，CALM 横盘 → MOVER 排前
    mover = [(c, c, c, c) for c in [100.0 + i * i * 0.2 for i in range(30)]]
    calm = [(100.0, 100.0, 100.0, 100.0)] * 30
    mon = VolatilityMonitor({"MOVER": "MOVERUSDT", "CALM": "CALMUSDT"},
                            ["15m"], _FakeStore({"MOVER": mover, "CALM": calm}))
    rows = mon.rank(0)
    assert rows[0]["coin"] == "MOVER"
    assert rows[0]["score"] > rows[-1]["score"]


def test_render_nonempty_card():
    mover = [(c, c, c, c) for c in [100.0 + i for i in range(30)]]
    mon = VolatilityMonitor({"MOVER": "MOVERUSDT"}, ["15m"],
                            _FakeStore({"MOVER": mover}))
    card = mon.render(mon.rank(0), 0)
    assert "MOVER" in card


def test_rank_skips_insufficient_data():
    mon = VolatilityMonitor({"X": "XUSDT"}, ["15m"], _FakeStore({"X": [(1.0, 1.0, 1.0, 1.0)]}))
    assert mon.rank(0) == []
