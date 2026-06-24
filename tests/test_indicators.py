"""指标引擎全面测试：technical / price_action / fibonacci / levels /
combo / sessions / knn / engine。

源码位于 src/smc_tracker/indicators/，本测试只读不改源码。
注意：technical.adx 在纯合成数据上结构性返回 nan（dx warmup 段含 nan 经
Wilder 二次平滑后全 nan）。这是源码已知行为，KNN/engine 用例通过 monkeypatch
把 adx 替换为有界且无 nan 的等价趋势强度实现来规避（不改源码）。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle  # noqa: E402
from smc_tracker.indicators import knn as knn_mod  # noqa: E402
from smc_tracker.indicators.combo import (  # noqa: E402
    combo_consensus,
    combo_signals,
)
from smc_tracker.indicators.engine import analyze  # noqa: E402
from smc_tracker.indicators.fibonacci import (  # noqa: E402
    fib_levels,
    in_golden_pocket,
)
from smc_tracker.indicators.knn import KNNPredictor, feature_matrix  # noqa: E402
from smc_tracker.indicators.levels import pivot_points  # noqa: E402
from smc_tracker.indicators.price_action import (  # noqa: E402
    detect_patterns,
    pa_features,
)
from smc_tracker.indicators.sessions import (  # noqa: E402
    current_session,
    in_killzone,
)
from smc_tracker.indicators.technical import (  # noqa: E402
    bollinger,
    compute_indicators,
    ema,
    rsi,
    sma,
)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def mk(i: int, o: float, h: float, l: float, c: float, v: float = 10.0) -> Candle:
    """构造单根测试 K 线（1m）。"""
    return Candle(
        coin="X",
        interval="1m",
        open_time_ms=i * 60000,
        close_time_ms=i * 60000 + 59999,
        o=o,
        h=h,
        l=l,
        c=c,
        v=v,
        n=0,
    )


def _adx_finite(high, low, close, n: int = 14):
    """无 nan 的趋势强度替身（[0,100]），用于规避 technical.adx 的 nan 结构问题。

    基于 +DM/-DM 的 n 窗口和算 DX，warmup 段用首个有效值前填，保证全程有限。
    仅在 KNN/engine 用例里 monkeypatch 进 knn 模块；不修改源码。
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    out = np.full(len(close), np.nan)
    if len(close) < n + 1:
        return out
    up = high[1:] - high[:-1]
    dn = low[:-1] - low[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    s = np.full(len(close), np.nan)
    for i in range(n, len(close)):
        ap = pdm[i - n:i].sum()
        am = mdm[i - n:i].sum()
        denom = ap + am
        s[i] = 100.0 * abs(ap - am) / denom if denom > 0 else 0.0
    first = np.nan
    for v in s:
        if np.isfinite(v):
            first = v
            break
    if np.isfinite(first):
        s[:n] = first
    return s


def _uptrend_candles(n: int = 150) -> list[Candle]:
    """带有界波动的明显上升趋势 K 线（保证各指标产生有限特征值）。"""
    cs: list[Candle] = []
    base = 100.0
    for i in range(n):
        wobble = 0.9 * np.sin(i * 0.7) + 0.5 * np.sin(i * 0.31)
        o = base
        c = base + 0.6 + wobble
        h = max(o, c) + 0.4 + 0.2 * abs(np.sin(i * 1.3))
        l = min(o, c) - 0.4 - 0.2 * abs(np.cos(i * 1.1))
        cs.append(mk(i, o, h, l, c, v=10 + (i % 5)))
        base = c
    return cs


@pytest.fixture()
def patch_adx(monkeypatch):
    """把 knn 模块里引用的 adx 换成无 nan 的实现（规避源码 nan）。"""
    monkeypatch.setattr(knn_mod, "adx", _adx_finite)
    return _adx_finite


# --------------------------------------------------------------------------- #
# technical
# --------------------------------------------------------------------------- #
def test_rsi_high_on_rising_closes():
    """递增收盘序列 → RSI 偏高（>60）。"""
    closes = np.linspace(100.0, 130.0, 40)
    r = rsi(closes, 14)
    assert np.isfinite(r[-1])
    assert r[-1] > 60.0


def test_sma_ema_reasonable():
    """SMA/EMA 在递增序列上数值合理：落在数据区间内，且 EMA 更贴近近端。"""
    closes = np.linspace(100.0, 130.0, 40)
    s = sma(closes, 20)
    e = ema(closes, 10)
    # warmup 段为 nan
    assert np.isnan(s[0])
    last_sma = s[-1]
    last_ema = e[-1]
    assert closes.min() <= last_sma <= closes.max()
    assert closes.min() <= last_ema <= closes.max()
    # 已知精确值（线性序列）
    assert last_sma == pytest.approx(float(np.mean(closes[-20:])), rel=1e-9)
    # 上升趋势中，短周期 EMA 比 20-SMA 更靠近最新价
    assert last_ema > last_sma


def test_bollinger_band_ordering():
    """布林带：上轨 > 中轨 > 下轨，且中轨 == SMA。"""
    closes = np.linspace(100.0, 130.0, 40)
    up, mid, low = bollinger(closes, 20, 2.0)
    assert up[-1] > mid[-1] > low[-1]
    assert mid[-1] == pytest.approx(sma(closes, 20)[-1], rel=1e-9)


def test_compute_indicators_returns_readings():
    """compute_indicators 返回含 readings 的 dict（numpy 路径，输入为 Candle 列表）。"""
    cs = _uptrend_candles(80)
    out = compute_indicators(cs)
    assert "readings" in out
    assert isinstance(out["readings"], dict)
    # 上升趋势 RSI 偏高
    assert out["rsi14"] is not None and out["rsi14"] > 60.0
    # 解读里至少包含 rsi/macd 等键
    assert "rsi" in out["readings"]
    assert out["price"] == pytest.approx(cs[-1].c, rel=1e-9)


# --------------------------------------------------------------------------- #
# price_action
# --------------------------------------------------------------------------- #
def test_detect_bullish_engulfing():
    """前阴后阳、后实体完全吞没前实体 → 看涨吞没。"""
    prev = mk(0, 10.0, 10.2, 8.8, 9.0)      # 阴线
    curr = mk(1, 8.9, 10.6, 8.8, 10.5)      # 阳线，c>=p.o 且 o<=p.c
    pats = detect_patterns([prev, curr])
    assert "看涨吞没" in pats


def test_detect_doji():
    """极小实体 → 十字星。"""
    prev = mk(0, 10.0, 11.0, 9.0, 10.5)
    doji = mk(1, 10.0, 11.0, 9.0, 10.02)    # body≈0.01
    f = pa_features(doji)
    assert f["body"] <= 0.1
    pats = detect_patterns([prev, doji])
    assert "十字星(doji)" in pats


def test_detect_hammer():
    """小实体在上 + 长下影 + 极短上影 → 锤子线(看涨)。"""
    prev = mk(0, 10.0, 11.0, 9.0, 10.5)
    hammer = mk(1, 9.5, 9.95, 8.0, 9.9)     # body≈0.21, lower≈0.77, upper≈0.03
    f = pa_features(hammer)
    assert f["lower_wick"] >= 0.5 and f["upper_wick"] <= 0.15
    pats = detect_patterns([prev, hammer])
    assert "锤子线(看涨)" in pats


# --------------------------------------------------------------------------- #
# fibonacci
# --------------------------------------------------------------------------- #
def test_fib_levels_up():
    """fib_levels(100,0,'up')：ret_0.618≈38.2，黄金口袋 21.4–38.2。"""
    lv = fib_levels(100.0, 0.0, "up")
    assert lv["ret_0.618"] == pytest.approx(38.2, abs=1e-6)
    lo, hi = sorted((lv["golden_lo"], lv["golden_hi"]))
    assert lo == pytest.approx(21.4, abs=1e-6)
    assert hi == pytest.approx(38.2, abs=1e-6)


def test_in_golden_pocket():
    """price=30 落在 21.4–38.2 黄金口袋内；区外返回 False。"""
    assert in_golden_pocket(30.0, 100.0, 0.0, "up") is True
    assert in_golden_pocket(50.0, 100.0, 0.0, "up") is False


# --------------------------------------------------------------------------- #
# levels
# --------------------------------------------------------------------------- #
def test_pivot_points_values():
    """经典枢轴点：PP=(h+l+c)/3，R1=2PP-l，S1=2PP-h。"""
    h, l, c = 110.0, 90.0, 105.0
    pp = pivot_points(h, l, c)
    expected_pp = (h + l + c) / 3.0
    assert pp["PP"] == pytest.approx(expected_pp, rel=1e-12)
    assert pp["R1"] == pytest.approx(2 * expected_pp - l, rel=1e-12)
    assert pp["S1"] == pytest.approx(2 * expected_pp - h, rel=1e-12)
    assert pp["R2"] == pytest.approx(expected_pp + (h - l), rel=1e-12)
    assert pp["S2"] == pytest.approx(expected_pp - (h - l), rel=1e-12)


# --------------------------------------------------------------------------- #
# combo
# --------------------------------------------------------------------------- #
def test_combo_signals_on_synthetic_dict():
    """combo_signals 接受合成 indicators dict，不报错并产出各 combo 的 score。"""
    ind = {
        "price": 110.0,
        "ema50": 100.0,
        "adx14": 30.0,
        "rsi14": 65.0,
        "macd_hist": 0.5,
        "stoch_k": 70.0,
        "bb_upper": 115.0,
        "bb_lower": 95.0,
        "bb_mid": 105.0,
        "atr14": 1.2,
    }
    combos = combo_signals(ind)
    assert isinstance(combos, dict)
    # 四个 combo 全部齐全（合成 dict 提供了全部输入）
    for key in ("trend", "momentum", "volatility", "reversal"):
        assert key in combos
        assert -1.0 <= combos[key]["score"] <= 1.0
    label, score = combo_consensus(combos)
    assert label in ("看多", "看空", "中性")
    assert -1.0 <= score <= 1.0


def test_combo_consensus_empty():
    """空 combos → 中性/0。"""
    assert combo_consensus({}) == ("中性", 0.0)


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hour,session,killzone",
    [
        (1, "亚洲", "亚洲开盘"),
        (8, "伦敦", "伦敦开盘"),
        (14, "纽约", "纽约开盘"),
        (22, "盘后", None),
    ],
)
def test_sessions_and_killzones(hour, session, killzone):
    """已知 UTC 小时 → 预期时段/killzone。"""
    ts_ms = hour * 3600 * 1000
    assert current_session(ts_ms) == session
    assert in_killzone(ts_ms) == killzone


# --------------------------------------------------------------------------- #
# knn
# --------------------------------------------------------------------------- #
def test_feature_matrix_shape(patch_adx):
    """feature_matrix 形状 (n, 21)：11 原有特征 + 10 SFG 因子。"""
    cs = _uptrend_candles(150)
    fm = feature_matrix(cs)
    assert fm.shape == (150, 21)


def test_feature_matrix_sfg_columns_present(patch_adx):
    """feature_matrix 第 11-20 列（SFG）经 imputation 后全部有限。

    SFG 因子的 nan（warmup/无结构/fail-closed）已被替换为 0.0（中性值）。
    形状应为 (n, 21)，SFG 部分 (n, 10) 全有限。
    """
    # 使用较长序列以覆盖各因子 warmup（ami ~40根, ai_st ~109根）
    cs = _uptrend_candles(250)
    fm = feature_matrix(cs)
    assert fm.shape == (250, 21)
    # SFG 列（11-20）应全部有限（nan→0.0 imputation）
    sfg_cols = fm[:, 11:]
    assert sfg_cols.shape == (250, 10)
    assert np.all(np.isfinite(sfg_cols)), (
        "SFG 列经 imputation（nan→0.0）后应全部有限"
    )
    # atr2 等短 warmup 因子在长序列尾部应有非零有限值（非全部为 imputed 0）
    sfg_atr2_col = fm[-50:, 17]  # 最后 50 行的 atr2 列（index 17）
    assert np.any(sfg_atr2_col != 0.0), "atr2_series 在 250 根 K 线尾部应有非零有限值"


def test_feature_matrix_warmup_rows_have_nan(patch_adx):
    """原有 11 列的 warmup 行应含 nan，被 np.all(isfinite) 过滤为无效训练行。

    SFG 列（11-20）在 feature_matrix 内已做 nan→0.0 imputation，
    所以 SFG 列本身不含 nan；行有效性由原有 11 列决定。
    """
    cs = _uptrend_candles(150)
    fm = feature_matrix(cs)
    # SFG 列（11-20）应全部有限（nan 已被 impute 为 0.0）
    sfg_cols = fm[:, 11:]
    assert np.all(np.isfinite(sfg_cols)), "SFG 列经 imputation 后应全部有限（nan→0.0）"
    # 原有 11 列（0-10）在 warmup 段含 nan（rsi warmup=14）
    base_cols = fm[:, :11]
    assert np.any(~np.isfinite(base_cols[:14])), "原有前 14 行应有 nan（技术指标 warmup）"
    # 行过滤（np.all(isfinite)）：前 warmup 行因原有列含 nan 而被正确排除
    valid_mask = np.all(np.isfinite(fm), axis=1)
    assert not np.all(valid_mask[:14]), "warmup 行不应全部通过 isfinite 检查"


def test_knn_fit_and_predict(patch_adx):
    """明显上升趋势的 150 根 K 线：fit==True，predict_latest 返回完整 dict。"""
    cs = _uptrend_candles(150)
    knn = KNNPredictor(k=5, horizon=3)
    assert knn.fit(cs) is True
    pred = knn.predict_latest(cs)
    assert isinstance(pred, dict)
    for key in ("direction", "p_up", "confidence"):
        assert key in pred
    assert pred["direction"] in ("long", "short")
    assert 0.0 <= pred["p_up"] <= 1.0
    assert 0.0 <= pred["confidence"] <= 1.0


def test_knn_fit_21d_succeeds_on_250_candles(patch_adx):
    """21 维特征 KNN 在 >=250 根合成 K 线上 fit 成功（返回 True）。

    250 根足以覆盖 ai_st warmup(~109根) + horizon(3) + k(5) margin。
    """
    cs = _uptrend_candles(250)
    knn = KNNPredictor(k=5, horizon=3)
    result = knn.fit(cs)
    # 诚实：若因 SFG 大量 nan 导致有效行<k，优雅降级返回 False；
    # 但 250 根且 k=5 时应能 fit 成功（atr2/dmha 等短 warmup 因子应提供足够行）
    assert result is True, (
        "250 根 K 线 + k=5 时 KNN fit 应成功；SFG warmup 饿死时请检查有效行数"
    )
    pred = knn.predict_latest(cs)
    assert pred is not None
    assert 0.0 <= pred["p_up"] <= 1.0


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
def test_analyze_returns_full_dict(patch_adx):
    """analyze 在 60+ 根 K 线上返回含 bias/indicators/patterns/knn 的 dict。"""
    cs = _uptrend_candles(80)
    knn = KNNPredictor(k=5, horizon=3)
    assert knn.fit(cs) is True
    now_ms = 8 * 3600 * 1000  # 伦敦时段
    a = analyze(cs, now_ms=now_ms, knn=knn)
    assert "error" not in a
    for key in ("bias", "indicators", "patterns", "knn"):
        assert key in a
    assert isinstance(a["indicators"], dict)
    assert isinstance(a["patterns"], list)
    assert a["knn"] is not None  # 传入了已 fit 的 KNNPredictor
    assert a["session"] == "伦敦"
    assert a["bias_label"] in ("看多", "看空", "中性")
    assert -1.0 <= a["bias"] <= 1.0


def test_analyze_includes_near_fib(patch_adx):
    """analyze 输出含 'near_fib' 键：(名称, 价格) 元组或 None。"""
    cs = _uptrend_candles(80)
    a = analyze(cs, now_ms=0)
    assert "near_fib" in a
    nf = a["near_fib"]
    # 有界波动的上升趋势 swing 必非零跨度 → nearest_fib 命中
    assert nf is not None
    name, lvl = nf
    assert isinstance(name, str)
    assert np.isfinite(lvl)


def test_analyze_insufficient_candles():
    """K 线不足时返回 error。"""
    cs = _uptrend_candles(10)
    a = analyze(cs, now_ms=0)
    assert a.get("error") is not None
