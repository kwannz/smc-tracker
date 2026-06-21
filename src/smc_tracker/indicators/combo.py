"""4 个 combo 复合指标：把多个基础指标融合成趋势/动量/波动/反转判断。"""
from __future__ import annotations

from typing import Any


def _g(d: dict, k: str) -> float | None:
    v = d.get(k)
    return v if isinstance(v, (int, float)) else None


def combo_signals(ind: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """输入 compute_indicators 的结果，输出 4 个 combo 的 (label, score∈[-1,1])。"""
    price = _g(ind, "price")
    out: dict[str, dict[str, Any]] = {}

    # 1) 趋势 combo：价 vs EMA50 + ADX 强度
    ema50, adx = _g(ind, "ema50"), _g(ind, "adx14")
    if price is not None and ema50 is not None and adx is not None:
        dirn = 1.0 if price > ema50 else -1.0
        strength = min(adx / 50.0, 1.0)
        out["trend"] = {"label": ("强" if adx > 25 else "弱") + ("多" if dirn > 0 else "空"),
                        "score": dirn * strength}

    # 2) 动量 combo：RSI + MACD柱 + Stoch
    rsi, hist, k = _g(ind, "rsi14"), _g(ind, "macd_hist"), _g(ind, "stoch_k")
    if rsi is not None and hist is not None and k is not None:
        s = ((rsi - 50) / 50.0 + (1 if hist > 0 else -1) + (k - 50) / 50.0) / 3.0
        out["momentum"] = {"label": "看多" if s > 0.15 else "看空" if s < -0.15 else "中性",
                           "score": max(-1.0, min(1.0, s))}

    # 3) 波动 combo：布林带宽 + ATR（挤压=待变盘）
    up, low, mid, atr = (_g(ind, "bb_upper"), _g(ind, "bb_lower"),
                         _g(ind, "bb_mid"), _g(ind, "atr14"))
    if up is not None and low is not None and mid is not None and atr is not None and price is not None:
        width = (up - low) / mid if mid not in (None, 0) else 0.0
        squeeze = width < (atr / price) * 2.0 if price != 0 else False
        out["volatility"] = {"label": "挤压(待变盘)" if squeeze else "扩张",
                             "score": -1.0 if squeeze else 1.0}

    # 4) 反转 combo：RSI/Stoch 极值 + 布林带外
    if rsi is not None and k is not None and up is not None and low is not None and price is not None:
        rev = 0.0
        if rsi > 70 and k > 80 and price >= up:
            rev = -1.0          # 超买+触上轨 → 看跌反转
        elif rsi < 30 and k < 20 and price <= low:
            rev = 1.0           # 超卖+触下轨 → 看涨反转
        out["reversal"] = {"label": "看涨反转" if rev > 0 else "看跌反转" if rev < 0 else "无",
                           "score": rev}

    return out


def combo_consensus(combos: dict[str, dict[str, Any]]) -> tuple[str, float]:
    """4 combo 的合成方向与分数（均值）。"""
    scores = [c["score"] for c in combos.values() if "score" in c]
    if not scores:
        return "中性", 0.0
    avg = sum(scores) / len(scores)
    return ("看多" if avg > 0.2 else "看空" if avg < -0.2 else "中性"), avg
