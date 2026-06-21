"""KNN 预测器：用「技术指标 + 价格行为」特征向量，找历史 K 个最相似状态，
按它们之后的涨跌投票预测当前方向（纯 numpy，低延迟，无 sklearn 依赖）。

第一性原理：相似的市场状态(指标组合)往往有相似的后续走向。
特征 = [RSI, MACD柱, Stoch%K, ADX, CCI, 布林位置, ATR占比, 实体, 上影, 下影, 方向]。
标签 = 未来 horizon 根后的涨/跌。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .price_action import pa_features
from .technical import adx, atr, bollinger, cci, macd, ohlcv_arrays, rsi, stochastic

FEATURE_NAMES = ["rsi", "macd_hist", "stoch_k", "adx", "cci", "bb_pos",
                 "atr_pct", "body", "upper_wick", "lower_wick", "dir"]


def feature_matrix(candles: list[Any]) -> np.ndarray:
    """逐根 K 线的特征矩阵 (n, 11)。warmup 段含 nan。"""
    a = ohlcv_arrays(candles)
    h, l, c = a["h"], a["l"], a["c"]
    n = len(c)
    rsi_s = rsi(c, 14)
    _, _, hist = macd(c)
    k_s, _ = stochastic(h, l, c)
    adx_s = adx(h, l, c, 14)
    cci_s = cci(h, l, c, 20)
    up, _mid, low = bollinger(c, 20)
    atr_s = atr(h, l, c, 14)
    feats = np.full((n, len(FEATURE_NAMES)), np.nan)
    for i in range(n):
        denom = up[i] - low[i]
        bb_pos = (c[i] - low[i]) / denom if denom and np.isfinite(denom) else 0.5
        pf = pa_features(candles[i])
        feats[i] = [rsi_s[i], hist[i], k_s[i], adx_s[i], cci_s[i], bb_pos,
                    atr_s[i] / c[i] if c[i] else 0.0,
                    pf["body"], pf["upper_wick"], pf["lower_wick"], pf["dir"]]
    return feats


class KNNPredictor:
    def __init__(self, k: int = 15, horizon: int = 5) -> None:
        self.k = k
        self.horizon = horizon
        self._X: np.ndarray | None = None
        self._y: np.ndarray | None = None
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None

    def fit(self, candles: list[Any]) -> bool:
        feats = feature_matrix(candles)
        c = np.array([cd.c for cd in candles], dtype=float)
        rows, ys = [], []
        for i in range(len(c) - self.horizon):
            if not np.all(np.isfinite(feats[i])):
                continue
            rows.append(feats[i])
            ys.append(1 if c[i + self.horizon] > c[i] else 0)
        if len(rows) < self.k:
            return False
        X = np.array(rows)
        self._mean = X.mean(axis=0)
        self._std = X.std(axis=0) + 1e-9
        self._X = (X - self._mean) / self._std
        self._y = np.array(ys)
        return True

    def predict(self, feat: np.ndarray) -> dict[str, Any] | None:
        if self._X is None or self._y is None:
            return None
        feat = np.asarray(feat, dtype=float)
        if not np.all(np.isfinite(feat)):
            return None
        fn = (feat - self._mean) / self._std
        dist = np.sqrt(((self._X - fn) ** 2).sum(axis=1))
        idx = np.argsort(dist)[:self.k]
        w = 1.0 / (dist[idx] + 1e-9)
        p_up = float((w * self._y[idx]).sum() / w.sum())
        return {
            "direction": "long" if p_up > 0.5 else "short",
            "p_up": p_up,
            "confidence": abs(p_up - 0.5) * 2.0,
            "k": self.k, "samples": int(len(self._X)),
        }

    def predict_latest(self, candles: list[Any]) -> dict[str, Any] | None:
        feats = feature_matrix(candles)
        return self.predict(feats[-1])
