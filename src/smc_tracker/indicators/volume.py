"""每币种成交量监控（numpy，低延迟）。

功能：
- relative_volume: 最新量 / 近 n 根均量（RVOL）。
- volume_spike: 放量检测（最新量 >= mult×均量）。
- volume_trend: 量能 SMA 斜率 → rising/falling/flat。
- volume_profile: 按价格分箱累计成交量 + POC（控制点）。
- VolumeMonitor: 维护每币种近 N 根量的环形窗口，实时检测放量。
"""
from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np


def _volumes(candles: list[Any]) -> np.ndarray:
    """Candle 列表 → 成交量 numpy 数组。"""
    return np.array([c.v for c in candles], dtype=float)


def relative_volume(candles: list[Any], n: int = 20) -> float:
    """最新成交量 / 近 n 根均量（RVOL）。

    用最新 K 线之前的 n 根计算基准均量（不含当前根，避免自污染）。
    数据不足或均量为 0 时返回 0.0。
    """
    v = _volumes(candles)
    if len(v) < 2:
        return 0.0
    window = v[-(n + 1):-1] if len(v) > n else v[:-1]
    base = float(np.mean(window)) if len(window) else 0.0
    if base <= 0.0:
        return 0.0
    return float(v[-1] / base)


def volume_spike(candles: list[Any], n: int = 20, mult: float = 2.0) -> dict[str, Any]:
    """最新量是否 >= mult×近 n 根均量。

    返回 {spike: bool, ratio: float}，ratio 为 RVOL。
    """
    ratio = relative_volume(candles, n)
    return {"spike": ratio >= mult and ratio > 0.0, "ratio": ratio}


def volume_trend(candles: list[Any], n: int = 20) -> str:
    """量能 SMA 斜率 → 'rising' / 'falling' / 'flat'。

    取最近 n 根量的 SMA 序列做线性拟合斜率；以均量归一化判定阈值。
    数据不足返回 'flat'。
    """
    v = _volumes(candles)
    if len(v) < 2:
        return "flat"
    w = v[-n:] if len(v) > n else v
    if len(w) < 2:
        return "flat"
    x = np.arange(len(w), dtype=float)
    slope = float(np.polyfit(x, w, 1)[0])
    mean_v = float(np.mean(w))
    if mean_v <= 0.0:
        return "flat"
    # 归一化斜率：每根相对均量的变化率，阈值 1%
    norm = slope / mean_v
    if norm > 0.01:
        return "rising"
    if norm < -0.01:
        return "falling"
    return "flat"


def volume_profile(candles: list[Any], bins: int = 10) -> dict[str, Any]:
    """按价格分箱累计成交量。

    用每根 K 线典型价 (h+l+c)/3 落入对应价格箱并累加成交量。
    返回 {levels: [(price, vol), ...], poc: 控制点价格(成交量最大箱中心)}。
    无数据返回空 levels、poc=None。
    """
    if not candles:
        return {"levels": [], "poc": None}
    h = np.array([c.h for c in candles], dtype=float)
    l = np.array([c.l for c in candles], dtype=float)
    c_ = np.array([c.c for c in candles], dtype=float)
    v = np.array([c.v for c in candles], dtype=float)
    tp = (h + l + c_) / 3.0
    lo, hi = float(np.min(l)), float(np.max(h))
    if hi <= lo:
        # 价格无区间：单一价位汇总
        return {"levels": [(lo, float(np.sum(v)))], "poc": lo}
    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    # 典型价落入箱索引（右端归入最后一箱）
    idx = np.clip(np.searchsorted(edges, tp, side="right") - 1, 0, bins - 1)
    vol_per_bin = np.zeros(bins, dtype=float)
    np.add.at(vol_per_bin, idx, v)
    levels = [(float(centers[i]), float(vol_per_bin[i])) for i in range(bins)]
    poc = float(centers[int(np.argmax(vol_per_bin))])
    return {"levels": levels, "poc": poc}


class VolumeMonitor:
    """每币种放量监控器。

    维护每币种近 N 根成交量的环形窗口；update 喂入新 K 线后，
    若最新量 >= spike_mult × 窗口内历史均量则触发，返回 {coin, ratio, vol}。
    """

    def __init__(self, window: int = 20, spike_mult: float = 3.0) -> None:
        self.window = window
        self.spike_mult = spike_mult
        self._vols: dict[str, deque[float]] = {}

    def update(self, coin: str, candle: Any) -> dict[str, Any] | None:
        """喂入一根 K 线，检测放量。触发返回 {coin, ratio, vol}，否则 None。"""
        buf = self._vols.get(coin)
        if buf is None:
            buf = deque(maxlen=self.window)
            self._vols[coin] = buf
        vol = float(candle.v)
        result: dict[str, Any] | None = None
        if buf:  # 需有历史基准
            base = sum(buf) / len(buf)
            if base > 0.0:
                ratio = vol / base
                if ratio >= self.spike_mult:
                    result = {"coin": coin, "ratio": ratio, "vol": vol}
        buf.append(vol)
        return result
