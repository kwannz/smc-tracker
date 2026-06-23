"""KNN 历史相似形态打分器（方向性 setup 验证辅助）。

警告（CLAUDE.md §二）：KNN≈随机基线，已知历史回测 lift 高≠实盘赚钱。
本模块仅作「历史相似度辅助参考」，**不可单独依赖做交易决策**。

逻辑流程：
1. 归一化 setup 方向（多空白名单，大小写敏感，严格匹配）。
2. 用 KNNPredictor(k, horizon) fit candles；样本不足返回 None。
3. predict_latest(candles) 取最新 K 线预测；None → None。
4. 比较 KNN 预测方向与 setup 方向，计算 supports 标志。
5. 返回 KNNVerdict（含诚实 note）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..indicators.knn import KNNPredictor

# ── 方向白名单（大小写敏感，严格匹配） ────────────────────────────────────────
_LONG_ALIASES: frozenset[str] = frozenset({"bull", "long", "up", "bullish"})
_SHORT_ALIASES: frozenset[str] = frozenset({"bear", "short", "down", "bearish"})

# 诚实标注模板（CLAUDE.md §二，不夸大 KNN 效果）
_HONEST_NOTE = (
    "KNN≈随机基线(CLAUDE.md §二)，仅作历史相似度辅助，不可单独依赖"
)


@dataclass(slots=True)
class KNNVerdict:
    """KNN 历史相似形态打分结果。

    Attributes:
        supports: KNN 历史方向是否支持该 setup 方向。
        p_up: KNN 预测上涨概率（0~1）。
        knn_confidence: |p_up - 0.5| * 2，归一化置信度（0~1）。
        samples: KNN 训练时有效历史样本数。
        note: 诚实标注（包含 KNN≈随机基线警告）。
    """
    supports: bool
    p_up: float
    knn_confidence: float
    samples: int
    note: str


def validate_direction(
    candles: list[Any],
    direction: str,
    *,
    k: int = 15,
    horizon: int = 5,
) -> KNNVerdict | None:
    """用 KNN 历史相似形态验证 setup 方向，返回 KNNVerdict 或 None。

    Args:
        candles: K 线列表（需含足够历史，典型 ≥ 100 根）。
        direction: setup 方向字符串（大小写敏感白名单）：
            做多："bull"/"long"/"up"/"bullish"；
            做空："bear"/"short"/"down"/"bearish"；
            其他 → None。
        k: KNN 邻居数，默认 15。
        horizon: 预测前瞻根数，默认 5。

    Returns:
        KNNVerdict 实例；以下情况返回 None：
        - direction 不在白名单内；
        - candles 样本不足 KNN fit；
        - KNNPredictor.predict_latest 无法计算特征。

    Note:
        KNN≈随机基线（CLAUDE.md §二），高 lift≠赚钱，仅作辅助参考。
    """
    # ── 1. 归一化方向 ──────────────────────────────────────────────────────
    if direction in _LONG_ALIASES:
        setup_long = True
    elif direction in _SHORT_ALIASES:
        setup_long = False
    else:
        # 非法方向（含空串、大小写不匹配等）→ 明确拒绝，不静默降级
        return None

    # ── 2. 数据质量守卫：空列表快速退出 ────────────────────────────────────
    if not candles:
        return None

    # ── 3. 拟合 KNN ────────────────────────────────────────────────────────
    predictor = KNNPredictor(k=k, horizon=horizon)
    if not predictor.fit(candles):
        # 样本不足（有效行 < k），返回 None，不崩溃
        return None

    # ── 4. 预测最新 K 线 ────────────────────────────────────────────────────
    pred = predictor.predict_latest(candles)
    if pred is None:
        # 最新特征含 NaN（warmup 段），无法预测
        return None

    # ── 5. 计算 supports ─────────────────────────────────────────────────────
    knn_long = pred["direction"] == "long"
    supports = (setup_long and knn_long) or (not setup_long and not knn_long)

    return KNNVerdict(
        supports=supports,
        p_up=float(pred["p_up"]),
        knn_confidence=float(pred["confidence"]),
        samples=int(pred["samples"]),
        note=_HONEST_NOTE,
    )
