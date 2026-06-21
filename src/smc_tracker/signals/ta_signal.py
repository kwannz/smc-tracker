"""TA 复合信号：纯技术分析多因子共振。

第一性原理：单个技术因子（趋势/形态/机器学习预测）各有噪声；当多个独立因子
指向同一方向且合成分数过阈值时，胜率显著高于任一单因子。本模块在一段 K 线上综合：

  1) indicators.analyze 的合成偏向 bias（combo + PA + KNN 内部融合）
  2) dow_trend 道氏趋势（uptrend/downtrend/range）
  3) 双顶/双底形态（看跌/看涨反转）
  4) KNN 历史相似态预测（可选）

各因子归一到 [-1, 1]（正=看多、负=看空），加权平均成最终 score；
仅当 |score| ≥ threshold 且无强反向矛盾时出 long/short 信号，否则返回 None（中性）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..indicators.engine import analyze
from ..indicators.knn import KNNPredictor
from ..indicators.patterns import detect_double_bottom, detect_double_top, dow_trend

# 各因子权重（合成 score 时使用）。bias 已内含 combo/PA/KNN，故占主导。
_WEIGHTS: dict[str, float] = {
    "bias": 0.4,
    "dow": 0.25,
    "pattern": 0.2,
    "knn": 0.15,
}


@dataclass(slots=True)
class TAResult:
    """TASignal.evaluate 的结构化结果（同时支持 dict 化）。"""
    direction: str                       # 'long' / 'short'
    score: float                         # [-1, 1]，正=多、负=空
    reasons: list[str]                   # 触发理由（中文）
    components: dict[str, float]         # 各因子归一分数
    coin: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "direction": self.direction,
            "score": self.score,
            "reasons": self.reasons,
            "components": self.components,
        }
        if self.coin:
            d["coin"] = self.coin
        return d


class TASignal:
    """纯 TA 多因子共振信号器。

    参数:
      threshold: 合成分数阈值，|score| ≥ threshold 才出信号（默认 0.3）。
      swing_lookback: 摆动点/斐波那契回看窗口（传给 analyze 与 dow_trend）。
      min_candles: 最少 K 线数，不足返回 None。
    """

    def __init__(self, threshold: float = 0.3, swing_lookback: int = 50,
                 min_candles: int = 30) -> None:
        self.threshold = threshold
        self.swing_lookback = swing_lookback
        self.min_candles = min_candles

    def evaluate(self, candles: list[Any], knn: KNNPredictor | None = None,
                 now_ms: int = 0) -> dict[str, Any] | None:
        """综合多因子产出信号 dict，未达共振/阈值返回 None。

        返回 {coin?, direction, score, reasons, components}。
        coin 取自 candles[-1].coin（非空时带上）。
        """
        if not candles or len(candles) < self.min_candles:
            return None

        a = analyze(candles, now_ms=now_ms, knn=knn, swing_lookback=self.swing_lookback)
        if "error" in a:
            return None

        components: dict[str, float] = {}
        reasons: list[str] = []

        # 1) 合成偏向 bias（已 ∈ 约 [-1,1]，夹紧防溢出）
        bias = _clip(float(a.get("bias", 0.0)))
        components["bias"] = bias
        if abs(bias) >= 0.2:
            reasons.append(f"综合偏向{a.get('bias_label', '')}({bias:+.2f})")

        # 2) 道氏趋势
        lb = min(3, max(1, self.swing_lookback // 10)) if self.swing_lookback >= 10 else 3
        dow = dow_trend(candles, lookback=lb)
        trend = dow["trend"]
        dow_score = 1.0 if trend == "uptrend" else -1.0 if trend == "downtrend" else 0.0
        components["dow"] = dow_score
        if dow_score:
            reasons.append("道氏" + ("上升趋势" if dow_score > 0 else "下降趋势"))

        # 3) 双顶/双底形态（双顶=看跌反转，双底=看涨反转）
        pattern_score = 0.0
        if detect_double_bottom(candles, lookback=lb) is not None:
            pattern_score += 1.0
            reasons.append("双底(看涨反转)")
        if detect_double_top(candles, lookback=lb) is not None:
            pattern_score -= 1.0
            reasons.append("双顶(看跌反转)")
        pattern_score = _clip(pattern_score)
        components["pattern"] = pattern_score

        # 4) KNN 预测（analyze 已算，复用其结果）
        knn_score = 0.0
        knn_pred = a.get("knn")
        if knn_pred:
            knn_score = _clip((knn_pred["p_up"] - 0.5) * 2.0)
            if knn_pred["confidence"] >= 0.2:
                reasons.append(
                    f"KNN{'看多' if knn_score > 0 else '看空'}"
                    f"({knn_pred['confidence'] * 100:.0f}%)")
        components["knn"] = knn_score

        # 合成分数：加权平均（仅对实际参与的因子归一权重）
        total_w = 0.0
        acc = 0.0
        for key, w in _WEIGHTS.items():
            if key == "knn" and not knn_pred:
                continue                 # 无 KNN 时不计其权重
            total_w += w
            acc += w * components[key]
        score = _clip(acc / total_w) if total_w else 0.0

        # 共振校验：方向因子（bias/dow/pattern/knn）需多数同向且无强反向矛盾
        directional = [components["bias"], components["dow"],
                       components["pattern"], components["knn"]]
        n_long = sum(1 for x in directional if x > 0.05)
        n_short = sum(1 for x in directional if x < -0.05)

        # 阈值 + 同向多数 + 不存在与最终方向相反的硬反转形态
        if abs(score) < self.threshold:
            return None
        direction = "long" if score > 0 else "short"
        if direction == "long" and (n_long <= n_short or pattern_score < 0):
            return None
        if direction == "short" and (n_short <= n_long or pattern_score > 0):
            return None

        coin = getattr(candles[-1], "coin", "") or ""
        res = TAResult(direction=direction, score=round(score, 4),
                       reasons=reasons, components={k: round(v, 4)
                                                    for k, v in components.items()},
                       coin=coin)
        return res.to_dict()

    def fmt(self, sig: dict[str, Any] | None) -> str:
        """信号 dict 的简短文本。None → 中性提示。"""
        if not sig:
            return "TA: 无信号(中性)"
        d = "做多🟢" if sig["direction"] == "long" else "做空🔴"
        coin = (sig.get("coin", "") + " ").lstrip()
        head = f"📈 TA信号 {d} {coin}分={sig['score']:+.2f}".rstrip()
        comp = sig.get("components", {})
        comp_s = " ".join(f"{k}={v:+.2f}" for k, v in comp.items())
        reasons = " | ".join(sig.get("reasons", [])) or "—"
        return f"{head}\n   因子[{comp_s}]\n   理由: {reasons}"


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    """夹紧到 [lo, hi]。"""
    return lo if x < lo else hi if x > hi else x
