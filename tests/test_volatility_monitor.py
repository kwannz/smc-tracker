"""实时波动追踪 VolatilityMonitor + vol_metrics + pdarray 单测（合成数据，确定性）。

设计：每个周期独立展示指标（不做跨周期共振合并）；PDArray=ICT 溢价/折价数组。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.volatility_monitor import (
    vol_metrics, pdarray, VolatilityMonitor, volatility_highlights, market_regime,
    mtf_alignment, vol_percentile, coin_vol_state, vol_term_structure,
)


def _m(regime="常态", vol_pct=0.5, pd_pct=0.5):
    return {"regime": regime, "vol_pct": vol_pct, "pd_pct": pd_pct}


def test_coin_vol_state_expansion_takes_priority():
    """任一周期扩张且该周期 HVP≥0.7 → 🔶放量(进行中最优先)。"""
    by_tf = {"15m": _m("常态"), "1H": _m("扩张", vol_pct=0.85)}
    assert coin_vol_state(by_tf) == "🔶放量"


def test_coin_vol_state_hvp_extreme():
    """多数周期 HVP≥0.9 → 🔥高位剧烈。"""
    by_tf = {"15m": _m(vol_pct=0.95), "1H": _m(vol_pct=0.92), "4H": _m(vol_pct=0.5)}
    assert coin_vol_state(by_tf) == "🔥高位剧烈"


def test_coin_vol_state_squeeze():
    """多数周期压缩 → 🔸蓄势。"""
    by_tf = {"15m": _m("压缩"), "1H": _m("压缩"), "4H": _m("常态")}
    assert coin_vol_state(by_tf) == "🔸蓄势"


def test_coin_vol_state_deep_discount():
    """多数周期深折价(pd≤0.15) → 深折价。"""
    by_tf = {"15m": _m(pd_pct=0.05), "1H": _m(pd_pct=0.10), "4H": _m(pd_pct=0.5)}
    assert coin_vol_state(by_tf) == "深折价"


def test_coin_vol_state_normal_and_empty():
    assert coin_vol_state({"15m": _m()}) == "常态"
    assert coin_vol_state({}) == "常态"


# ── vol_term_structure：波动率期限结构(√t 归一后比短端 vs 长端) ──
_SCALE = (604_800_000 / 900_000) ** 0.5   # 1W vs 15m 的 √t 比≈25.9 = 平坦基准


def _rv(v):
    return {"rv": v}


def test_vol_term_structure_flat_when_sqrt_t_scaling():
    """rv 恰按 √t 缩放(rv_1W=rv_15m×√t比) → 归一相等 → 平坦。"""
    ts = vol_term_structure({"15m": _rv(1.0), "1W": _rv(_SCALE)})
    assert ts["shape"] == "平坦"
    assert abs(ts["ratio"] - 1.0) < 1e-6


def test_vol_term_structure_backwardation_near_term_stress():
    """近端 rv 高于 √t 基准(短端归一波动更大) → 倒挂(急性应激)。"""
    ts = vol_term_structure({"15m": _rv(2.0), "1W": _rv(_SCALE)})
    assert ts["shape"] == "倒挂"
    assert ts["ratio"] > 1.2


def test_vol_term_structure_contango_near_term_calm():
    """近端 rv 低于 √t 基准 → 顺挂(远端主导/风暴后趋缓)。"""
    ts = vol_term_structure({"15m": _rv(0.5), "1W": _rv(_SCALE)})
    assert ts["shape"] == "顺挂"
    assert ts["ratio"] < 0.83


def test_vol_term_structure_insufficient_and_dirty():
    """有效周期 <2 → 缺(不冒充结构)；rv 缺失/非有限的周期被剔除。"""
    assert vol_term_structure({"15m": _rv(1.0)})["shape"] == "缺"
    assert vol_term_structure({})["shape"] == "缺"
    assert vol_term_structure({"15m": _rv(float("nan")), "1H": _rv(1.0)})["shape"] == "缺"


def test_vol_percentile_high_when_recent_spike():
    """近端波动放大 → 当前 rv 处历史高位,百分位接近 1（开源 HVP 思路）。"""
    import math
    calm = [100.0] * 100
    spike = [100.0 + (3.0 if i % 2 else -3.0) for i in range(20)]   # 末段剧震
    p = vol_percentile(calm + spike)
    assert 0.0 <= p <= 1.0
    assert p > 0.8, f"近端剧震应处波动高位, got {p}"


def test_vol_percentile_low_when_recent_calm():
    """近端平静 → 当前 rv 处历史低位,百分位接近 0。"""
    spike = [100.0 + (3.0 if i % 2 else -3.0) for i in range(100)]
    calm = [100.0] * 30
    p = vol_percentile(spike + calm)
    assert p < 0.3, f"近端平静应处波动低位, got {p}"


def test_vol_percentile_insufficient_data_sentinel():
    """数据不足 → 返回 -1.0 哨兵(不冒充百分位)。"""
    assert vol_percentile([100.0, 101.0, 102.0]) == -1.0


def test_vol_percentile_nan_sentinel():
    """含 NaN → -1.0(数据质量守卫)。"""
    assert vol_percentile([100.0] * 50 + [float("nan")] + [100.0] * 50) == -1.0


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


def test_extreme_pd_inclusive_boundary():
    """P2-11：extreme_pd 用 <=0.1/>=0.9 包含边界。0.10/0.90 命中，0.11/0.89 不命中。"""
    def _row(pd):
        return {"coin": "X", "by_tf": {"15m": {"velocity": 0.0, "vol_ratio": 1.0,
                "regime": "常态", "pd_pct": pd, "pd_zone": "折价" if pd < 0.5 else "溢价"}}}
    hit_lo = volatility_highlights([_row(0.10)])["extreme_pd"]
    hit_hi = volatility_highlights([_row(0.90)])["extreme_pd"]
    miss_lo = volatility_highlights([_row(0.11)])["extreme_pd"]
    miss_hi = volatility_highlights([_row(0.89)])["extreme_pd"]
    assert len(hit_lo) == 1 and len(hit_hi) == 1
    assert miss_lo == [] and miss_hi == []


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


def test_render_freshness_stale_and_fresh():
    """P1-6a：render 陈旧分支覆盖。最快 15m→阈值 30min。陈旧→⚠️；新鲜→🕒无告警。"""
    up = [(c, c, c, c) for c in [100.0 + i for i in range(30)]]
    base = 1_700_000_000_000
    store = _FakeStore({"BTC": up}, latest={"BTC": base})
    mon = VolatilityMonitor({"BTC": "BTCUSDT"}, ["15m"], store)
    rows = mon.rank(0)
    # now 比最新 bar 晚 1 小时(>2×15m=30min) → 陈旧
    card_stale = mon.render(rows, now_ms=base + 3_600_000)
    assert "🕒" in card_stale and "陈旧" in card_stale
    # now 比最新 bar 晚 10 分钟(<30min) → 新鲜，无陈旧告警
    card_fresh = mon.render(rows, now_ms=base + 600_000)
    assert "🕒" in card_fresh and "陈旧" not in card_fresh


def test_render_freshness_threshold_dynamic_for_1h():
    """P1-2：周期为 1H 时陈旧阈值=2h(非固定30min)。最新 bar 后 1h 不应误报陈旧。"""
    up = [(c, c, c, c) for c in [100.0 + i for i in range(30)]]
    base = 1_700_000_000_000
    store = _FakeStore({"BTC": up}, latest={"BTC": base})
    mon = VolatilityMonitor({"BTC": "BTCUSDT"}, ["1H"], store)
    rows = mon.rank(0)
    card = mon.render(rows, now_ms=base + 3_600_000)  # 晚 1h，<2×1H=2h
    assert "🕒" in card and "陈旧" not in card  # 不误报(修 P1-2)


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


def test_market_regime_term_breadth():
    """市场级期限结构广度：主导倒挂时 label 追加'近端应激 N/M币'，term 计数正确。"""
    rows = [
        {"coin": "A", "by_tf": {"15m": {"regime": "扩张", "pd_zone": "均衡"}},
         "term": {"shape": "倒挂"}},
        {"coin": "B", "by_tf": {"15m": {"regime": "扩张", "pd_zone": "均衡"}},
         "term": {"shape": "倒挂"}},
        {"coin": "C", "by_tf": {"15m": {"regime": "常态", "pd_zone": "均衡"}},
         "term": {"shape": "平坦"}},
    ]
    mr = market_regime(rows)
    assert mr["term"] == {"倒挂": 2, "平坦": 1, "顺挂": 0}
    assert "近端应激" in mr["label"] and "2/3币" in mr["label"]


def test_market_regime_term_absent_keeps_label():
    """rows 无 term 键(向后兼容) → 不追加期限广度，label 仅含 regime/PD。"""
    rows = [{"coin": "A", "by_tf": {"15m": {"regime": "压缩", "pd_zone": "折价"}}}]
    mr = market_regime(rows)
    assert mr["term"] == {"倒挂": 0, "平坦": 0, "顺挂": 0}
    assert "应激" not in mr["label"] and "主导" not in mr["label"]


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
    """方向冲突 → 分歧，score 精确 2/3（nit-4：精确断言非恒真 <1）。"""
    by_tf = {"15m": {"velocity": 2.0}, "1H": {"velocity": -2.0}, "4H": {"velocity": 1.0}}
    a = mtf_alignment(by_tf)
    assert a["bias"] == "分歧"
    assert a["aligned"] == 2 and a["total"] == 3
    assert abs(a["score"] - 2 / 3) < 1e-9


def test_mtf_alignment_threshold_boundary():
    """P1-6b：钉住 _ALIGN_TH=0.7 边界。3上1下(score=0.75≥0.7)→多；2上1下(0.667<0.7)→分歧。"""
    up3down1 = {"a": {"velocity": 1.0}, "b": {"velocity": 1.0},
                "c": {"velocity": 1.0}, "d": {"velocity": -1.0}}
    a = mtf_alignment(up3down1)
    assert a["score"] == 0.75 and a["bias"] == "多"
    up2down1 = {"a": {"velocity": 1.0}, "b": {"velocity": 1.0}, "c": {"velocity": -1.0}}
    b = mtf_alignment(up2down1)
    assert abs(b["score"] - 2 / 3) < 1e-9 and b["bias"] == "分歧"


def test_mtf_alignment_empty():
    a = mtf_alignment({})
    assert a["bias"] == "分歧" and a["total"] == 0 and a["score"] == 0.0
