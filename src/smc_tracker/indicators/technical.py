"""10 个核心技术指标（numpy，低延迟）。

返回与输入等长的序列（warmup 段为 nan）。OHLCV 取自 Candle 列表。
指标：SMA, EMA, RSI, MACD, Bollinger, ATR, Stochastic, ADX, OBV, VWAP, CCI。
"""
from __future__ import annotations

from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def ohlcv_arrays(candles: list[Any]) -> dict[str, np.ndarray]:
    """Candle 列表 → {o,h,l,c,v} numpy 数组。"""
    return {
        "o": np.array([c.o for c in candles], dtype=float),
        "h": np.array([c.h for c in candles], dtype=float),
        "l": np.array([c.l for c in candles], dtype=float),
        "c": np.array([c.c for c in candles], dtype=float),
        "v": np.array([c.v for c in candles], dtype=float),
    }


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """指数移动均线（α=2/(n+1)）。**首值种子**(out[0]=x[0] 起递推)——经典开源约定。

    与 TA-Lib 的 SMA 种子约定在 warmup 期不同，但种子影响指数衰减：足够数据后末值差极小
    (#145 交叉验证：5 币 MACD 末值最大 Δ0.8%、多数 <0.1%)。两者皆标准约定，非 bug——
    勿因"对 TA-Lib 不为零"误判。MACD/信号线均基于本函数，故同口径自洽。
    """
    out = np.full(len(x), np.nan)
    m = len(x)
    if m == 0:
        return out
    alpha = 2.0 / (n + 1.0)
    beta = 1.0 - alpha
    # 递推本质串行；用标量累加器避免逐步 numpy 索引开销，运算顺序与原实现一致
    xs = x.tolist()
    prev = xs[0]
    out[0] = prev
    for i in range(1, m):
        prev = alpha * xs[i] + beta * prev
        out[i] = prev
    return out


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder 平滑（RSI/ADX/ATR 用）。对前导 NaN 鲁棒（ADX 双重平滑必需）。"""
    out = np.full(len(x), np.nan)
    finite = np.isfinite(x)
    start = None
    for i in range(n - 1, len(x)):          # 首个「末尾 n 根全有效」的位置作种子
        if np.all(finite[i - n + 1:i + 1]):
            start = i
            break
    if start is None:
        return out
    out[start] = np.mean(x[start - n + 1:start + 1])
    for i in range(start + 1, len(x)):
        xi = x[i] if finite[i] else out[i - 1]   # 缺失值用上一平滑值续
        out[i] = (out[i - 1] * (n - 1) + xi) / n
    return out


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = _wilder(gain, n)
    al = _wilder(loss, n)
    rs = np.divide(ag, al, out=np.full_like(ag, np.inf), where=al != 0)
    rsi_vals = 100.0 - 100.0 / (1.0 + rs)
    # 全平盘修正：ag==0 且 al==0 时 RSI 应为 50(中性)而非 100
    flat = (ag == 0) & (al == 0)
    rsi_vals = np.where(flat, 50.0, rsi_vals)
    out[1:] = rsi_vals  # delta 短 1
    return out


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    """滑窗总体标准差(ddof=0)，与逐窗 np.std 数值一致。

    用 sliding_window_view 把各窗排成 (m,n) 后整体 np.std(axis=1)，
    每个窗内的算法与原逐窗 np.std 完全相同（不引入前缀和的相消误差）。
    """
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1:] = np.std(sliding_window_view(x, n), axis=1)
    return out


def bollinger(close: np.ndarray, n: int = 20, k: float = 2.0
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(close, n)
    std = _rolling_std(close, n)
    return mid + k * std, mid, mid - k * std


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    if len(close) < 2:
        return np.full(len(close), np.nan)
    prev_close = np.roll(close, 1)
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    tr[0] = high[0] - low[0]
    return _wilder(tr, n)


def stochastic(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               n: int = 14, d: int = 3) -> tuple[np.ndarray, np.ndarray]:
    k = np.full(len(close), np.nan)
    if len(close) >= n:
        hh = np.max(sliding_window_view(high, n), axis=1)   # 各窗最高
        ll = np.min(sliding_window_view(low, n), axis=1)     # 各窗最低
        rng = hh - ll
        valid = rng > 0
        vals = np.where(valid, 100.0 * (close[n - 1:] - ll) / np.where(valid, rng, 1.0), 50.0)
        k[n - 1:] = vals
    # %D = %K 的 d 周期均线；sma 用 cumsum 会被 warmup 段的前导 NaN 污染成全 NaN，
    # 故对 k 的有效段(从 n-1 起)单独做 NaN-safe 滑窗均值，避免 stoch_d 恒为 None。
    dd = np.full(len(close), np.nan)
    if len(close) >= n + d - 1:
        win = sliding_window_view(k[n - 1:], d)
        dd[n - 1 + d - 1:] = win.mean(axis=1)
    return k, dd


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    if len(close) < n + 1:
        return np.full(len(close), np.nan)
    up = high[1:] - high[:-1]
    dn = low[:-1] - low[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    prev_close = close[:-1]
    tr = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - prev_close),
                            np.abs(low[1:] - prev_close)])
    atr_ = _wilder(tr, n)
    pdi = 100.0 * _wilder(plus_dm, n) / np.where(atr_ == 0, np.nan, atr_)
    mdi = 100.0 * _wilder(minus_dm, n) / np.where(atr_ == 0, np.nan, atr_)
    dx = 100.0 * np.abs(pdi - mdi) / np.where((pdi + mdi) == 0, np.nan, pdi + mdi)
    adx_short = _wilder(dx, n)
    out = np.full(len(close), np.nan)
    out[1:] = adx_short
    return out


def obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    out = np.zeros(len(close))
    if len(close) == 0:
        return out
    out[0] = volume[0]                       # TA-Lib 约定：OBV[0]=首根成交量(对齐业界基准)
    if len(close) < 2:
        return out
    diff = np.diff(close)
    # 涨 +v、跌 -v、平 0，自首值累加（恒定基线不影响斜率/背离，但绝对值与 TA-Lib 一致）
    signed = np.where(diff > 0, volume[1:], np.where(diff < 0, -volume[1:], 0.0))
    out[1:] = out[0] + np.cumsum(signed)
    return out


def vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray
         ) -> np.ndarray:
    tp = (high + low + close) / 3.0
    cum_v = np.cumsum(volume)
    cum_tpv = np.cumsum(tp * volume)
    return np.divide(cum_tpv, cum_v, out=np.full_like(cum_tpv, np.nan), where=cum_v != 0)


def cci(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 20) -> np.ndarray:
    tp = (high + low + close) / 3.0
    out = np.full(len(close), np.nan)
    if len(close) >= n:
        win = sliding_window_view(tp, n)            # (m, n) 各滑窗
        ma = win.mean(axis=1)                        # 各窗典型价均值
        md = np.abs(win - ma[:, None]).mean(axis=1)  # 平均绝对偏差
        nz = md != 0
        out[n - 1:] = np.where(
            nz, (tp[n - 1:] - ma) / (0.015 * np.where(nz, md, 1.0)), 0.0)
    return out


def _last(x: np.ndarray) -> float | None:
    if len(x) == 0:
        return None
    v = x[-1]
    return float(v) if np.isfinite(v) else None


def compute_indicators(candles: list[Any]) -> dict[str, Any]:
    """计算全部 10 指标，返回最新值 + 简单解读（多/空/中性）。"""
    a = ohlcv_arrays(candles)
    o, h, l, c, v = a["o"], a["h"], a["l"], a["c"], a["v"]
    macd_line, sig_line, hist = macd(c)
    bb_up, bb_mid, bb_low = bollinger(c)
    k, dd = stochastic(h, l, c)
    out: dict[str, Any] = {
        "sma20": _last(sma(c, 20)),
        "ema50": _last(ema(c, 50)),
        "rsi14": _last(rsi(c, 14)),
        "macd": _last(macd_line), "macd_signal": _last(sig_line), "macd_hist": _last(hist),
        "bb_upper": _last(bb_up), "bb_mid": _last(bb_mid), "bb_lower": _last(bb_low),
        "atr14": _last(atr(h, l, c, 14)),
        "stoch_k": _last(k), "stoch_d": _last(dd),
        "adx14": _last(adx(h, l, c, 14)),
        "obv": _last(obv(c, v)),
        "vwap": _last(vwap(h, l, c, v)),
        "cci20": _last(cci(h, l, c, 20)),
        "price": _last(c),
    }
    out["readings"] = _readings(out)
    return out


def _readings(x: dict[str, Any]) -> dict[str, str]:
    """各指标的多空解读。"""
    r: dict[str, str] = {}
    rsi_v = x.get("rsi14")
    if rsi_v is not None:
        r["rsi"] = "超买" if rsi_v > 70 else "超卖" if rsi_v < 30 else "中性"
    if x.get("macd_hist") is not None:
        r["macd"] = "看多" if x["macd_hist"] > 0 else "看空"
    p, mid = x.get("price"), x.get("bb_mid")
    if p is not None and x.get("bb_upper") is not None:
        r["bollinger"] = ("上轨上方(强)" if p > x["bb_upper"] else
                          "下轨下方(弱)" if p < x["bb_lower"] else
                          "中轨上" if mid and p > mid else "中轨下")
    if x.get("adx14") is not None:
        r["adx"] = "强趋势" if x["adx14"] > 25 else "弱趋势/震荡"
    k = x.get("stoch_k")
    if k is not None:
        r["stoch"] = "超买" if k > 80 else "超卖" if k < 20 else "中性"
    if p is not None and x.get("vwap") is not None:
        r["vwap"] = "VWAP上方" if p > x["vwap"] else "VWAP下方"
    return r
