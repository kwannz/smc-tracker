"""统一技术分析：在一段 K 线上算齐 指标/combo/PA/斐波那契/支撑压力/KNN/时段，汇总成一张图。"""
from __future__ import annotations

from typing import Any

from .combo import combo_consensus, combo_signals
from .fibonacci import fib_levels, in_golden_pocket, nearest_fib
from .knn import KNNPredictor
from .levels import nearest_levels, support_resistance
from .price_action import detect_patterns, pa_bias
from .sessions import current_session, in_killzone
from .technical import compute_indicators


def analyze(candles: list[Any], now_ms: int = 0, knn: KNNPredictor | None = None,
            swing_lookback: int = 50) -> dict[str, Any]:
    """综合技术分析。返回指标/combo/PA/斐波那契/SR/KNN/时段 + 合成偏向。"""
    if len(candles) < 30:
        return {"error": "K线不足"}
    ind = compute_indicators(candles)
    combos = combo_signals(ind)
    combo_dir, combo_score = combo_consensus(combos)
    patterns = detect_patterns(candles)
    pa = pa_bias(candles)
    price = ind.get("price") or candles[-1].c

    # 斐波那契：用最近 swing_lookback 根的高低做摆动
    window = candles[-swing_lookback:]
    hi = max(c.h for c in window)
    lo = min(c.l for c in window)
    direction = "up" if candles[-1].c >= (hi + lo) / 2 else "down"
    fib = fib_levels(hi, lo, direction)
    in_ote = in_golden_pocket(price, hi, lo, direction)
    near_fib = nearest_fib(price, hi, lo, direction)  # (名称, 价格)|None：离价最近的斐波那契位

    sr = support_resistance(candles, lookback=3)
    near = nearest_levels(price, sr)

    knn_pred = None
    if knn is not None:
        knn_pred = knn.predict_latest(candles)

    # 合成偏向：combo + PA + KNN
    parts = [combo_score, pa]
    if knn_pred:
        parts.append((knn_pred["p_up"] - 0.5) * 2.0)
    bias = sum(parts) / len(parts)

    return {
        "price": price,
        "indicators": ind,
        "combos": combos, "combo_dir": combo_dir, "combo_score": combo_score,
        "patterns": patterns, "pa_bias": pa,
        "fib": fib, "in_ote": in_ote, "near_fib": near_fib,
        "support": near["support"], "resistance": near["resistance"],
        "knn": knn_pred,
        "session": current_session(now_ms) if now_ms else None,
        "killzone": in_killzone(now_ms) if now_ms else None,
        "bias": bias,
        "bias_label": "看多" if bias > 0.2 else "看空" if bias < -0.2 else "中性",
    }


def fmt_analysis(coin: str, a: dict[str, Any]) -> str:
    """分析结果的简短文本。"""
    if "error" in a:
        return f"{coin}: {a['error']}"
    ind = a["indicators"]
    knn = a.get("knn")
    knn_s = (f" KNN={knn['direction']}({knn['confidence']*100:.0f}%)" if knn else "")
    pats = ("|" + ",".join(a["patterns"])) if a["patterns"] else ""
    kz = f" ⏰{a['killzone']}" if a.get("killzone") else ""
    nf = a.get("near_fib")
    nf_s = f" 贴近{nf[0]}位" if nf else ""
    return (f"📊 {coin} 价={a['price']:g} 偏向={a['bias_label']}({a['bias']:+.2f}){knn_s}\n"
            f"   RSI={ind.get('rsi14') or 0:.0f} ADX={ind.get('adx14') or 0:.0f} "
            f"MACD={'+' if (ind.get('macd_hist') or 0) > 0 else '-'} "
            f"combo={a['combo_dir']} 支撑={a['support'] or '-'} 压力={a['resistance'] or '-'}"
            f"{' OTE' if a['in_ote'] else ''}{nf_s}{pats}{kz}")
