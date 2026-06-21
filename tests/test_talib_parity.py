"""TA-Lib 基准平价测试：以业界标准 TA-Lib 交叉验证本项目 numpy 指标的数值正确性。

数据质量保障(对齐 CLAUDE.md「可使用talib」「数据质量高」「先实证」)：
  · 本项目指标默认纯 numpy(无 TA-Lib 硬依赖、可移植、已向量化 ~1ms)；
  · 本测试仅在已装 TA-Lib 时运行(importorskip)，未装则自动跳过——零硬依赖；
  · 比对尾部(EMA 类 warmup 种子差异收敛后)，要求与 TA-Lib 浮点级一致。
曾用此法抓到 OBV 首值约定差异(应=首根成交量)并修正。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

talib = pytest.importorskip("talib")     # 未装 TA-Lib 则跳过整个模块

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smc_tracker.indicators import technical as T  # noqa: E402


def _series(n: int = 500, seed: int = 42):
    """确定性合成 OHLCV(随机游走)。"""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    vol = rng.uniform(1e3, 5e3, n)
    return high, low, close, vol


def _assert_tail(mine, ref, *, tail: int = 200, rtol: float = 1e-5, atol: float = 1e-6):
    """比对尾部 tail 根中两者均有限的点，最大相对/绝对误差需在容差内。"""
    mine = np.asarray(mine, float)
    ref = np.asarray(ref, float)
    m = np.isfinite(mine) & np.isfinite(ref)
    m[:len(m) - tail] = False
    assert m.sum() >= tail // 2, "重叠有效点过少"
    np.testing.assert_allclose(mine[m], ref[m], rtol=rtol, atol=atol)


def test_sma_parity():
    _, _, c, _ = _series()
    _assert_tail(T.sma(c, 20), talib.SMA(c, 20), atol=1e-9)


def test_ema_parity():
    _, _, c, _ = _series()           # EMA 种子差异(首值 vs SMA)在尾部收敛
    _assert_tail(T.ema(c, 50), talib.EMA(c, 50), rtol=1e-4, atol=1e-3)


def test_rsi_parity():
    _, _, c, _ = _series()
    _assert_tail(T.rsi(c, 14), talib.RSI(c, 14), atol=1e-9)


def test_macd_parity():
    _, _, c, _ = _series()
    ml, sl, hi = T.macd(c)
    tml, tsl, thi = talib.MACD(c, 12, 26, 9)
    _assert_tail(ml, tml, atol=1e-6)
    _assert_tail(sl, tsl, atol=1e-6)
    _assert_tail(hi, thi, atol=1e-6)


def test_bbands_parity():
    _, _, c, _ = _series()
    up, mid, lo = T.bollinger(c, 20, 2.0)
    tu, tm, tl = talib.BBANDS(c, 20, 2, 2, 0)
    _assert_tail(up, tu, atol=1e-8)
    _assert_tail(mid, tm, atol=1e-8)
    _assert_tail(lo, tl, atol=1e-8)


def test_atr_parity():
    h, l, c, _ = _series()
    _assert_tail(T.atr(h, l, c, 14), talib.ATR(h, l, c, 14), atol=1e-8)


def test_stochastic_k_parity():
    h, l, c, _ = _series()           # 本项目 stochastic 返回快速 %K → 对 STOCHF
    k, _ = T.stochastic(h, l, c, 14, 3)
    tk, _ = talib.STOCHF(h, l, c, 14, 3, 0)
    _assert_tail(k, tk, atol=1e-9)


def test_adx_parity():
    h, l, c, _ = _series()
    _assert_tail(T.adx(h, l, c, 14), talib.ADX(h, l, c, 14), atol=1e-6)


def test_obv_parity():
    _, _, c, v = _series()           # 曾因首值约定(=首根量)差异被此测试抓到→已修正
    _assert_tail(T.obv(c, v), talib.OBV(c, v), rtol=1e-9, atol=1e-6)


def test_cci_parity():
    h, l, c, _ = _series()
    _assert_tail(T.cci(h, l, c, 14), talib.CCI(h, l, c, 14), atol=1e-8)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ TA-Lib 平价全部通过")
