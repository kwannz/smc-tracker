"""布林带计算层单元测试（TDD 红→绿，合成数据，确定性）。

覆盖：
  - talib.BBANDS 与 technical.bollinger 数值平价（importorskip 零硬依赖）
  - bb_bands：主路径 talib，回退 numpy bollinger，两者平价
  - analyze_tf：price>upper→pct_b>1→pos_label含"压力"/bull True；
    price<lower→含"支撑"/bull False；candles 不足→None
  - aggregate_coin：5多2空→consensus_pct=71、lean_label 偏多；全多→净多；全空→净空
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# ---- 合成 Candle 工厂 ----

@dataclass
class _FakeCandle:
    """最小假 Candle：只需 o/h/l/c/v 字段。"""
    coin: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    o: float
    h: float
    l: float
    c: float
    v: float
    n: int = 0


def _make_candles(closes: list[float], coin: str = "BTC") -> list[_FakeCandle]:
    """用给定收盘价列表构造假 Candle 序列（等价 5m 间隔）。"""
    out = []
    for i, c in enumerate(closes):
        ts = i * 300_000
        out.append(_FakeCandle(
            coin=coin, interval="5m",
            open_time_ms=ts, close_time_ms=ts + 300_000,
            o=c, h=c * 1.001, l=c * 0.999, c=c, v=1000.0,
        ))
    return out


def _rng_close(n: int = 300, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))


# ---- A. talib 与 technical.bollinger 平价（importorskip）----

talib = pytest.importorskip("talib", reason="TA-Lib 未装，跳过平价测试")


def test_bbands_talib_vs_numpy_parity():
    """talib.BBANDS(matype=0) 与 bollinger(numpy SMA ddof=0) 浮点级平价。"""
    from smc_tracker.indicators.technical import bollinger as np_bb
    close = _rng_close(300)
    tu, tm, tl = talib.BBANDS(close, timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)
    nu, nm, nl = np_bb(close, n=20, k=2.0)
    # 比对尾部 200 根（warmup NaN 之后）
    tail = 200
    m = np.isfinite(tu) & np.isfinite(nu)
    m[:-tail] = False
    assert m.sum() >= 80, "有效重叠点过少"
    np.testing.assert_allclose(nu[m], tu[m], rtol=1e-6, atol=1e-8)
    np.testing.assert_allclose(nm[m], tm[m], rtol=1e-6, atol=1e-8)
    np.testing.assert_allclose(nl[m], tl[m], rtol=1e-6, atol=1e-8)


def test_bb_bands_function_uses_talib_primary():
    """bb_bands 主路径使用 talib，与直接 talib.BBANDS 结果一致。"""
    from smc_tracker.indicators.bollinger_bands import bb_bands
    close = _rng_close(300)
    u, m, l = bb_bands(close, period=20, k=2.0)
    tu, tm, tl = talib.BBANDS(close, timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)
    mask = np.isfinite(u) & np.isfinite(tu)
    assert mask.sum() > 100
    np.testing.assert_allclose(u[mask], tu[mask], rtol=1e-6, atol=1e-8)
    np.testing.assert_allclose(m[mask], tm[mask], rtol=1e-6, atol=1e-8)
    np.testing.assert_allclose(l[mask], tl[mask], rtol=1e-6, atol=1e-8)


# ---- B. analyze_tf ----

def test_analyze_tf_price_above_upper():
    """价格明显高于上轨 → pct_b>1, pos_label 含"压力", bull=True。"""
    from smc_tracker.indicators.bollinger_bands import analyze_tf
    # 构造 30 根稳定价，然后末根大幅拉高
    closes = [100.0] * 30
    closes[-1] = 200.0  # 强制 price >> upper
    candles = _make_candles(closes)
    result = analyze_tf(candles, period=20)
    assert result is not None
    assert result["pct_b"] > 1.0
    assert "压力" in result["pos_label"]
    assert result["bull"] is True
    # price 字段正确
    assert abs(result["price"] - 200.0) < 1e-6


def test_analyze_tf_price_below_lower():
    """价格明显低于下轨 → pct_b<0, pos_label 含"支撑", bull=False。"""
    from smc_tracker.indicators.bollinger_bands import analyze_tf
    closes = [100.0] * 30
    closes[-1] = 0.5  # 强制 price << lower
    candles = _make_candles(closes)
    result = analyze_tf(candles, period=20)
    assert result is not None
    assert result["pct_b"] < 0.0
    assert "支撑" in result["pos_label"]
    assert result["bull"] is False


def test_analyze_tf_price_in_middle():
    """价格在中轨上方（0.5<pct_b<0.8）→ bull=True, pos_label 偏多相关。"""
    from smc_tracker.indicators.bollinger_bands import analyze_tf
    # 缓步上升序列，末值略高于均值
    closes = list(np.linspace(90, 105, 30))
    candles = _make_candles(closes)
    result = analyze_tf(candles, period=20)
    assert result is not None
    assert result["pct_b"] > 0.0, f"pct_b={result['pct_b']}"
    assert isinstance(result["bull"], bool)
    assert "pos_label" in result
    assert "bandwidth" in result
    assert isinstance(result["squeeze"], bool)


def test_analyze_tf_insufficient_candles():
    """K 线不足 period+1 → 返回 None。"""
    from smc_tracker.indicators.bollinger_bands import analyze_tf
    closes = [100.0] * 15  # period=20，少于 period+1
    candles = _make_candles(closes)
    assert analyze_tf(candles, period=20) is None


def test_analyze_tf_return_keys():
    """analyze_tf 返回 dict 含全部必要键。"""
    from smc_tracker.indicators.bollinger_bands import analyze_tf
    closes = [100.0 + i * 0.1 for i in range(50)]
    candles = _make_candles(closes)
    result = analyze_tf(candles, period=20)
    assert result is not None
    for key in ("upper", "mid", "lower", "price", "pct_b", "bandwidth", "squeeze",
                "pos_label", "bull"):
        assert key in result, f"缺少键 {key}"


def test_analyze_tf_bandwidth_squeeze_guard():
    """分母守卫：upper==lower（极端常数序列）→ pct_b 返回 0.5，不崩溃。"""
    from smc_tracker.indicators.bollinger_bands import analyze_tf
    # 完全常数序列 → std=0 → upper=lower=mid
    closes = [100.0] * 50
    candles = _make_candles(closes)
    result = analyze_tf(candles, period=20)
    assert result is not None
    assert result["pct_b"] == pytest.approx(0.5)


# ---- C. aggregate_coin ----

def test_aggregate_coin_5bull_2bear():
    """5多2空 → consensus_pct=71, lean_label 包含「偏多」。"""
    from smc_tracker.indicators.bollinger_bands import aggregate_coin
    tf_results = {
        "1m":  {"bull": True,  "squeeze": False, "pct_b": 0.6},
        "5m":  {"bull": True,  "squeeze": False, "pct_b": 0.7},
        "15m": {"bull": True,  "squeeze": True,  "pct_b": 0.75},
        "30m": {"bull": False, "squeeze": False, "pct_b": 0.4},
        "1H":  {"bull": True,  "squeeze": False, "pct_b": 0.65},
        "4H":  {"bull": True,  "squeeze": False, "pct_b": 0.8},
        "1D":  {"bull": False, "squeeze": False, "pct_b": 0.3},
    }
    agg = aggregate_coin(tf_results)
    assert agg["bull_n"] == 5
    assert agg["bear_n"] == 2
    assert agg["total"] == 7
    assert agg["consensus_pct"] == 71  # round(100*5/7)=71
    assert "偏多" in agg["lean_label"]
    assert agg["squeeze_n"] == 1


def test_aggregate_coin_all_bull():
    """全多 → lean_label 净多（consensus_pct=100）。"""
    from smc_tracker.indicators.bollinger_bands import aggregate_coin
    tf_results = {
        f"tf{i}": {"bull": True, "squeeze": False, "pct_b": 0.8}
        for i in range(5)
    }
    agg = aggregate_coin(tf_results)
    assert agg["consensus_pct"] == 100
    assert "净多" in agg["lean_label"]


def test_aggregate_coin_all_bear():
    """全空 → lean_label 净空（consensus_pct=0）。"""
    from smc_tracker.indicators.bollinger_bands import aggregate_coin
    tf_results = {
        f"tf{i}": {"bull": False, "squeeze": False, "pct_b": 0.2}
        for i in range(5)
    }
    agg = aggregate_coin(tf_results)
    assert agg["consensus_pct"] == 0
    assert "净空" in agg["lean_label"]


def test_aggregate_coin_skip_none():
    """None 值的 timeframe 被忽略，不计入 total。"""
    from smc_tracker.indicators.bollinger_bands import aggregate_coin
    tf_results: dict = {
        "1H": {"bull": True,  "squeeze": False, "pct_b": 0.7},
        "4H": None,   # 应跳过
        "1D": {"bull": False, "squeeze": False, "pct_b": 0.3},
    }
    agg = aggregate_coin(tf_results)
    assert agg["total"] == 2
    assert agg["bull_n"] == 1
    assert agg["bear_n"] == 1


def test_aggregate_coin_empty():
    """无数据 → consensus_pct=50，lean_label=分歧，total=0。"""
    from smc_tracker.indicators.bollinger_bands import aggregate_coin
    agg = aggregate_coin({})
    assert agg["total"] == 0
    assert agg["consensus_pct"] == 50


def test_aggregate_coin_lean_labels():
    """验证 lean_label 各档位正确分档。"""
    from smc_tracker.indicators.bollinger_bands import aggregate_coin

    def _mk(bull_n: int, total: int) -> dict:
        tfs = {}
        for i in range(total):
            tfs[f"tf{i}"] = {"bull": i < bull_n, "squeeze": False, "pct_b": 0.6 if i < bull_n else 0.4}
        return aggregate_coin(tfs)

    assert "净多" in _mk(5, 5)["lean_label"]       # 100%
    assert "净多" in _mk(4, 5)["lean_label"]       # 80% → 净多（consensus_pct=80 >=80）
    assert "偏多" in _mk(3, 5)["lean_label"]       # 60% (>=60 偏多)
    # 分歧区间 40<x<60
    # 偏空
    assert "净空" in _mk(1, 5)["lean_label"]       # 20% → 净空（consensus_pct=20 <=20）
    assert "净空" in _mk(0, 5)["lean_label"]       # 0%


# ---- D. 🟡 审计缺陷：pct_b==0.5 中性不计入 bull/bear ----

def test_aggregate_coin_neutral_pct_b_not_counted():
    """🟡 审计缺陷：pct_b==0.5（band_width=0 的兜底值）被算作 bear，污染共识。

    修复：pct_b==0.5 时跳过，不计入 bull 也不计入 bear，total 相应减少。
    修复前：bull=(pct_b>0.5)=False → 算入 bear_n，导致横盘周期虚假偏空。
    """
    from smc_tracker.indicators.bollinger_bands import aggregate_coin

    # 构造：2 个明确多头(pct_b=0.8)，1 个中性(pct_b=0.5，band_width=0 的兜底)
    tf_results = {
        "1H": {"bull": True,  "squeeze": False, "pct_b": 0.8},
        "4H": {"bull": True,  "squeeze": False, "pct_b": 0.7},
        "1D": {"bull": False, "squeeze": False, "pct_b": 0.5},  # 中性，应被跳过
    }
    agg = aggregate_coin(tf_results)

    # 修复后：中性不计入，total=2，bull=2，bear=0，consensus_pct=100
    assert agg["total"] == 2, (
        f"中性 pct_b=0.5 应跳过，total 应为 2，实际 {agg['total']}"
    )
    assert agg["bull_n"] == 2, f"bull_n 应为 2，实际 {agg['bull_n']}"
    assert agg["bear_n"] == 0, (
        f"中性不应计入 bear，bear_n 应为 0，实际 {agg['bear_n']} "
        f"（修复前 pct_b=0.5 → bull=False → 被算入 bear）"
    )
    assert agg["consensus_pct"] == 100, (
        f"2多0空 consensus_pct 应为 100，实际 {agg['consensus_pct']}"
    )


def test_aggregate_coin_all_neutral_not_counted():
    """全部 pct_b==0.5（极端横盘）→ total=0，consensus_pct=50，不崩溃。"""
    from smc_tracker.indicators.bollinger_bands import aggregate_coin

    tf_results = {
        "1H": {"bull": False, "squeeze": False, "pct_b": 0.5},
        "4H": {"bull": False, "squeeze": False, "pct_b": 0.5},
    }
    agg = aggregate_coin(tf_results)

    assert agg["total"] == 0, f"全中性 total 应为 0，实际 {agg['total']}"
    assert agg["bull_n"] == 0
    assert agg["bear_n"] == 0
    assert agg["consensus_pct"] == 50  # 与空 dict 行为一致


def test_aggregate_coin_mixed_with_neutral_not_polluting():
    """中性混入多空时：共识只计多空，不被中性污染。

    3多+1空+1中性 → 修复后 total=4, bull=3, bear=1, consensus_pct=75。
    修复前 total=5, bull=3, bear=2, consensus_pct=60（中性被算空，拉低共识）。
    """
    from smc_tracker.indicators.bollinger_bands import aggregate_coin

    tf_results = {
        "1m":  {"bull": True,  "squeeze": False, "pct_b": 0.7},
        "5m":  {"bull": True,  "squeeze": False, "pct_b": 0.8},
        "15m": {"bull": True,  "squeeze": False, "pct_b": 0.65},
        "1H":  {"bull": False, "squeeze": False, "pct_b": 0.3},
        "4H":  {"bull": False, "squeeze": False, "pct_b": 0.5},  # 中性，应跳过
    }
    agg = aggregate_coin(tf_results)

    assert agg["total"] == 4, f"中性跳过后 total 应为 4，实际 {agg['total']}"
    assert agg["bull_n"] == 3
    assert agg["bear_n"] == 1
    assert agg["consensus_pct"] == 75  # round(100*3/4)=75
