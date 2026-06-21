"""支撑/压力位：经典枢轴点(pivot points) + 摆动点聚类成 S/R 区。"""
from __future__ import annotations

from typing import Any


def pivot_points(high: float, low: float, close: float) -> dict[str, float]:
    """经典 floor pivot：用上一周期 HLC 算 PP/R1-3/S1-3。"""
    pp = (high + low + close) / 3.0
    return {
        "PP": pp,
        "R1": 2 * pp - low, "S1": 2 * pp - high,
        "R2": pp + (high - low), "S2": pp - (high - low),
        "R3": high + 2 * (pp - low), "S3": low - 2 * (high - pp),
    }


def _swings(candles: list[Any], lb: int) -> tuple[list[float], list[float]]:
    """分形摆动高/低点价格。"""
    highs, lows = [], []
    n = len(candles)
    for i in range(lb, n - lb):
        hi = candles[i].h
        lo = candles[i].l
        if all(hi > candles[j].h for j in range(i - lb, i + lb + 1) if j != i):
            highs.append(hi)
        if all(lo < candles[j].l for j in range(i - lb, i + lb + 1) if j != i):
            lows.append(lo)
    return highs, lows


def support_resistance(candles: list[Any], lookback: int = 3, tol_pct: float = 0.003
                       ) -> dict[str, list[tuple[float, int]]]:
    """把摆动高/低点按价位聚类成 S/R 区，返回 {resistance:[(价,触及次数)], support:[...]}（按触及次数降序）。"""
    highs, lows = _swings(candles, lookback)

    def cluster(prices: list[float]) -> list[tuple[float, int]]:
        prices = sorted(prices)
        zones: list[list[float]] = []
        for p in prices:
            if zones and abs(p - zones[-1][-1]) <= tol_pct * p:
                zones[-1].append(p)
            else:
                zones.append([p])
        out = [(sum(z) / len(z), len(z)) for z in zones]
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    return {"resistance": cluster(highs), "support": cluster(lows)}


def nearest_levels(price: float, sr: dict[str, list[tuple[float, int]]]
                   ) -> dict[str, float | None]:
    """离当前价最近的上方压力、下方支撑。"""
    res = [p for p, _ in sr.get("resistance", []) if p > price]
    sup = [p for p, _ in sr.get("support", []) if p < price]
    return {"resistance": min(res) if res else None,
            "support": max(sup) if sup else None}
