"""价格行为(Price Action)：K 线形态识别 + PA 特征(供 KNN 用)。"""
from __future__ import annotations

from typing import Any


def pa_features(candle: Any) -> dict[str, float]:
    """单根 K 线的价格行为特征（归一化到 [0,1] / [-1,1]）。"""
    rng = candle.h - candle.l
    if rng <= 0:
        return {"body": 0.0, "upper_wick": 0.0, "lower_wick": 0.0, "dir": 0.0}
    body = abs(candle.c - candle.o) / rng
    upper = (candle.h - max(candle.o, candle.c)) / rng
    lower = (min(candle.o, candle.c) - candle.l) / rng
    return {"body": body, "upper_wick": upper, "lower_wick": lower,
            "dir": 1.0 if candle.c >= candle.o else -1.0}


def detect_patterns(candles: list[Any]) -> list[str]:
    """识别最后一根（及与前一根的）K 线形态。"""
    if len(candles) < 2:
        return []
    c, p = candles[-1], candles[-2]
    pats: list[str] = []
    f = pa_features(c)

    is_doji = f["body"] <= 0.1
    if is_doji:
        pats.append("十字星(doji)")
    # 锤子/上吊：小实体在上、长下影；十字星已命中则跳过，避免矛盾
    if not is_doji and f["body"] <= 0.35 and f["lower_wick"] >= 0.5 and f["upper_wick"] <= 0.15:
        pats.append("锤子线(看涨)")
    # 流星/射击之星：小实体在下、长上影；十字星已命中则跳过，避免矛盾
    if not is_doji and f["body"] <= 0.35 and f["upper_wick"] >= 0.5 and f["lower_wick"] <= 0.15:
        pats.append("流星线(看跌)")
    # pin bar（任一长影 ≥ 2/3）
    if f["lower_wick"] >= 0.66:
        pats.append("看涨pin bar")
    if f["upper_wick"] >= 0.66:
        pats.append("看跌pin bar")

    # 吞没
    p_bull = p.c >= p.o
    c_bull = c.c >= c.o
    if c_bull and not p_bull and c.c >= p.o and c.o <= p.c:
        pats.append("看涨吞没")
    if (not c_bull) and p_bull and c.o >= p.c and c.c <= p.o:
        pats.append("看跌吞没")

    # 内包/外包
    if c.h < p.h and c.l > p.l:
        pats.append("内包线(inside bar)")
    if c.h > p.h and c.l < p.l:
        pats.append("外包线(outside bar)")
    return pats


def pa_bias(candles: list[Any]) -> float:
    """价格行为偏向 [-1,1]：综合形态方向。

    改为"按方向集合是否命中"计分，避免强相关形态逐个累加导致单根K线饱和偏向。
    有看涨信号 +0.5，有看跌信号 -0.5，两者均有则相互抵消为 0。
    """
    pats = detect_patterns(candles)
    bull_set = {"锤子线(看涨)", "看涨pin bar", "看涨吞没"}
    bear_set = {"流星线(看跌)", "看跌pin bar", "看跌吞没"}
    has_bull = any(p in bull_set for p in pats)
    has_bear = any(p in bear_set for p in pats)
    score = (0.5 if has_bull else 0.0) + (-0.5 if has_bear else 0.0)
    return max(-1.0, min(1.0, score))
