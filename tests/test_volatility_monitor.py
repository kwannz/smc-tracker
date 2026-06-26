"""实时波动追踪 VolatilityMonitor + vol_metrics + pdarray 单测（合成数据，确定性）。

设计：每个周期独立展示指标（不做跨周期共振合并）；PDArray=ICT 溢价/折价数组。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.volatility_monitor import (
    vol_metrics, pdarray, VolatilityMonitor, volatility_highlights, market_regime,
    mtf_alignment,
)


def _ohlc(closes):
    """由收盘价构造 (o,h,l,c)：o=前收，h/l=±0（聚焦收盘动力学）。"""
    o = [closes[0]] + closes[:-1]
    h = [max(a, b) for a, b in zip(o, closes)]
    l = [min(a, b) for a, b in zip(o, closes)]
    return o, h, l, closes


# ---- vol_metrics 纯函数 ----

def test_flat_prices_near_zero_vol():
    _o, h, l, c = _ohlc([100.0] * 30)
    m = vol_metrics(h, l, c)
    assert abs(m["rv"]) < 1e-6
    assert abs(m["velocity"]) < 1e-6
    assert abs(m["accel"]) < 1e-6


def test_uptrend_positive_velocity():
    _o, h, l, c = _ohlc([100.0 + i for i in range(30)])
    m = vol_metrics(h, l, c)
    assert m["velocity"] > 0


def test_acceleration_positive_on_convex_up():
    _o, h, l, c = _ohlc([100.0 + i * i * 0.1 for i in range(30)])
    m = vol_metrics(h, l, c)
    assert m["accel"] > 0


def test_too_few_bars_returns_empty():
    assert vol_metrics([1.0], [1.0], [1.0]) == {}


def test_regime_expansion_recent_volatility_up():
    """近窗波动 >> 长窗基线 → 扩张（放量）。"""
    calm = [100.0] * 40
    osc = [100.0 + (2.0 if i % 2 else -2.0) for i in range(20)]
    _o, h, l, c = _ohlc(calm + osc)
    m = vol_metrics(h, l, c)
    assert m["vol_ratio"] > 1.4
    assert m["regime"] == "扩张"


def test_regime_squeeze_recent_calm():
    """近窗波动 << 长窗基线 → 压缩（蓄势）。"""
    osc = [100.0 + (2.0 if i % 2 else -2.0) for i in range(40)]
    calm = [100.0] * 20
    _o, h, l, c = _ohlc(osc + calm)
    m = vol_metrics(h, l, c)
    assert m["vol_ratio"] < 0.7
    assert m["regime"] == "压缩"


def test_regime_normal_steady_volatility():
    """波动稳定 → 常态（ratio≈1）。"""
    osc = [100.0 + (1.0 if i % 2 else -1.0) for i in range(80)]
    _o, h, l, c = _ohlc(osc)
    m = vol_metrics(h, l, c)
    assert m["regime"] == "常态"


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


def test_pdarray_band_boundary_non_degenerate():
    """P2-4：非退化区间内钉住 0.5±band 分区（band=0.03）。"""
    h = [110.0] * 30          # 区间高 110
    l = [100.0] * 30          # 区间低 100 → EQ=105
    # 末价 105 → pd=0.5 → 均衡（rng>0，走 band 分类而非退化 early-return）
    assert pdarray(h, l, [100.0] * 29 + [105.0])["pd_zone"] == "均衡"
    # 末价 106 → pd=0.6 > 0.53 → 溢价
    assert pdarray(h, l, [100.0] * 29 + [106.0])["pd_zone"] == "溢价"
    # 末价 104 → pd=0.4 < 0.47 → 折价
    assert pdarray(h, l, [100.0] * 29 + [104.0])["pd_zone"] == "折价"


# ---- VolatilityMonitor（逐周期）----

class _FakeCandle:
    __slots__ = ("o", "h", "l", "c")
    def __init__(self, o, h, l, c):
        self.o, self.h, self.l, self.c = o, h, l, c


class _FakeStore:
    def __init__(self, series, latest=None):
        self._series = series  # {(coin,tf): [(o,h,l,c), ...]} 或 {coin: [...]}（所有 tf 共用）
        self._latest = latest or {}  # {coin: last_ms}（可选，供新鲜度测试）

    def get_candles(self, coin, tf, limit=1000):
        data = self._series.get((coin, tf), self._series.get(coin, []))
        return [_FakeCandle(*x) for x in data]

    def latest_candle_ms(self, coin, tf):
        return self._latest.get(coin)


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


def test_rank_captures_freshness_last_ms():
    """rank 给每币带最新 bar 时间(供新鲜度展示)；store 无 latest_candle_ms 时 last_ms=0 不崩。"""
    up = [(c, c, c, c) for c in [100.0 + i for i in range(30)]]
    store = _FakeStore({"BTC": up}, latest={"BTC": 1_700_000_000_000})
    mon = VolatilityMonitor({"BTC": "BTCUSDT"}, ["15m"], store)
    rows = mon.rank(0)
    assert rows[0]["last_ms"] == 1_700_000_000_000


def test_rank_freshness_absent_store_method_safe():
    """store 无 latest_candle_ms（旧 fake）→ last_ms=0，不抛。"""
    up = [(c, c, c, c) for c in [100.0 + i for i in range(30)]]
    class _NoLatest:
        def get_candles(self, coin, tf, limit=1000):
            return [_FakeCandle(*x) for x in up]
    mon = VolatilityMonitor({"BTC": "BTCUSDT"}, ["15m"], _NoLatest())
    assert mon.rank(0)[0]["last_ms"] == 0


def test_volatility_highlights_synthesizes_matrix():
    """动向摘要：从矩阵提取 压缩(蓄势)/扩张(放量)/极端PD。"""
    rows = [
        {"coin": "A", "score": 1, "by_tf": {"15m": {
            "velocity": 0.1, "vol_ratio": 0.3, "regime": "压缩", "pd_pct": 0.5, "pd_zone": "均衡"}}},
        {"coin": "B", "score": 1, "by_tf": {"1H": {
            "velocity": 5.0, "vol_ratio": 2.0, "regime": "扩张", "pd_pct": 0.95, "pd_zone": "溢价"}}},
        {"coin": "C", "score": 1, "by_tf": {"4H": {
            "velocity": -1.0, "vol_ratio": 1.0, "regime": "常态", "pd_pct": 0.05, "pd_zone": "折价"}}},
    ]
    h = volatility_highlights(rows)
    assert [x["coin"] for x in h["squeeze"]] == ["A"]
    assert [x["coin"] for x in h["expansion"]] == ["B"]
    assert {x["coin"] for x in h["extreme_pd"]} == {"B", "C"}   # 0.95溢价 + 0.05折价


def test_volatility_highlights_empty():
    h = volatility_highlights([])
    assert h == {"squeeze": [], "expansion": [], "extreme_pd": []}


def test_market_regime_dominant():
    """市场态势聚合：主导 regime/PD + 计数。"""
    rows = [
        {"coin": "A", "by_tf": {
            "15m": {"regime": "压缩", "pd_zone": "折价"},
            "1H": {"regime": "压缩", "pd_zone": "折价"}}},
        {"coin": "B", "by_tf": {
            "15m": {"regime": "压缩", "pd_zone": "折价"},
            "1H": {"regime": "扩张", "pd_zone": "溢价"}}},
    ]
    mr = market_regime(rows)
    assert mr["n"] == 4
    assert mr["regime"]["压缩"] == 3 and mr["regime"]["扩张"] == 1
    assert mr["pd"]["折价"] == 3
    assert "蓄势" in mr["label"] and "折价" in mr["label"]


def test_market_regime_empty():
    mr = market_regime([])
    assert mr["n"] == 0 and mr["label"] == ""


def test_mtf_alignment_all_up():
    """各周期速度同向上 → 多头一致，aligned=total。"""
    by_tf = {"15m": {"velocity": 1.0}, "1H": {"velocity": 2.0}, "4H": {"velocity": 0.5}}
    a = mtf_alignment(by_tf)
    assert a["bias"] == "多" and a["aligned"] == 3 and a["total"] == 3
    assert a["score"] == 1.0


def test_mtf_alignment_all_down():
    by_tf = {"15m": {"velocity": -1.0}, "1H": {"velocity": -2.0}}
    a = mtf_alignment(by_tf)
    assert a["bias"] == "空" and a["aligned"] == 2


def test_mtf_alignment_mixed():
    """方向冲突 → 分歧，score<1。"""
    by_tf = {"15m": {"velocity": 2.0}, "1H": {"velocity": -2.0}, "4H": {"velocity": 1.0}}
    a = mtf_alignment(by_tf)
    assert a["bias"] == "分歧"
    assert a["aligned"] == 2 and a["total"] == 3   # 多数为多(2/3)
    assert a["score"] < 1.0


def test_mtf_alignment_empty():
    a = mtf_alignment({})
    assert a["bias"] == "分歧" and a["total"] == 0 and a["score"] == 0.0
