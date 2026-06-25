"""信号有效性自适应加权（meta-labeling 闭环）。

用系统自身的预测回顾数据(predictions 表)反哺信号质量：
- Wilson score 95% 置信区间评估各 kind 信号"真实命中率区间"（小样本稳健）
- 统计显著优于随机 → 加权；统计显著反指 → 降权 + 标注 contrarian
- 样本不足 → 中性权重 1.0（不冒进、不扰乱）

设计原则：纯函数 wilson_interval 可单测；SignalEfficacy 完全依赖 predictions 表，
不修改 PredictionReview，只读数据。权重仅作推送标注与打分参考，不抑制信号。

命中率来源：直接使用 predictions 表 correct 字段（与 accuracy_report() 保持一致）。
|realized_ret| > _RET_OUTLIER(10.0=1000%) 行同 review.py 一致过滤（单位错配/陈旧价）。

注：v2 曾尝试 market_neutral_stats 横截面去均值，但该函数要求同 bucket 内有多个不同
coin 的预测才能做横截面。refresh() 内按 kind 分组后，同一 kind 在同一 1h bucket 可能
包含 7 条不同 TF 的 MTF 记录，bucket_mean = 这 7 条的均值，不是真正的市场 beta。
在牛市单调上涨场景下，仅 4h/12h/1d 超过均值 → hit_rate≈3/7=42.9% → 错贴 contrarian。
改回直接读 correct 字段，语义正确、与 accuracy_report() 一致，诚实反映系统准确率。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..util import to_float

# 离群值阈值，与 review.py._RET_OUTLIER 保持一致（|ret|>10=1000% 为单位错配/陈旧价）
_RET_OUTLIER = 10.0


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% 置信区间（业界标准二项比例区间，小样本稳健）。

    n=0 时返回 (0.0, 1.0)（无信息，区间最宽）。
    标准公式：
      center = (p̂ + z²/2n) / (1 + z²/n)
      margin = z / (1 + z²/n) * sqrt(p̂(1-p̂)/n + z²/(4n²))
      lower = center - margin, upper = center + margin
    """
    if n == 0:
        return (0.0, 1.0)
    p_hat = hits / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4.0 * n * n))
    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return (lower, upper)


@dataclass(slots=True)
class KindEfficacy:
    """单个 kind 的信号有效性评估结果。"""
    kind: str           # 信号种类：'跟庄'/'共识'/'背离'/'超级'
    n: int              # 已评估样本数（离群值已过滤）
    hits: int           # 命中数（predictions.correct=1，与 accuracy_report() 一致）
    hit_rate: float     # 命中率 hits/n（0~1）
    lower: float        # Wilson 95% CI 下界
    upper: float        # Wilson 95% CI 上界
    weight: float       # 自适应权重（0.3~1.5）
    contrarian: bool    # True=统计显著反指，应反向解读
    note: str           # 权重解释说明（供推送/调试）


class SignalEfficacy:
    """信号有效性自适应加权器。

    从 predictions 表读取已评估的历史预测，按 kind 聚合命中率，
    用 Wilson score 95% CI 评估统计显著性，据此自适应加权。

    不依赖 PredictionReview 类（只读 predictions 表），不修改任何已有逻辑。
    """

    def __init__(self, store: Any, min_sample: int = 20) -> None:
        self.store = store
        self.min_sample = min_sample
        # kind -> KindEfficacy，初始空表（未刷新前 weight_of 返回 1.0）
        self._table: dict[str, KindEfficacy] = {}

    def refresh(self, now_ms: int, lookback_ms: int = 604_800_000) -> dict[str, KindEfficacy]:
        """从 predictions 表刷新各 kind 的命中率有效性评估。

        仅读取 evaluated=1 且 ts >= now_ms - lookback_ms 且 correct IS NOT NULL 的行
        （默认近 7 天）。按 kind 聚合 correct 字段（1=命中，0=未命中），
        用 Wilson score 95% CI 评估统计显著性，据此自适应加权。

        |realized_ret| > _RET_OUTLIER 的行同 accuracy_report() 一致过滤（单位错配/陈旧价）。
        更新 self._table 并返回。任何 DB 异常由调用方处理（不吞异常）。
        """
        since = now_ms - lookback_ms
        rows = self.store.conn.execute(
            "SELECT kind, correct, realized_ret FROM predictions"
            " WHERE evaluated=1 AND correct IS NOT NULL AND ts>=?",
            (since,),
        ).fetchall()

        # 按 kind 聚合 correct 字段，过滤离群值（与 accuracy_report() 保持一致）
        by_kind: dict[str, dict[str, int]] = {}
        for kind, correct, realized_ret in rows:
            # 离群值过滤：|ret|>10(=1000%) 几乎必为单位错配/陈旧价
            if abs(to_float(realized_ret, 0.0)) > _RET_OUTLIER:
                continue
            if kind not in by_kind:
                by_kind[kind] = {"hits": 0, "n": 0}
            by_kind[kind]["n"] += 1
            if correct == 1:
                by_kind[kind]["hits"] += 1

        table: dict[str, KindEfficacy] = {}
        for kind, agg in by_kind.items():
            n = agg["n"]
            hits = agg["hits"]
            hit_rate = hits / n if n > 0 else 0.0
            lower, upper = wilson_interval(hits, n)

            if n < self.min_sample:
                # 样本不足：中性权重，不推断
                weight = 1.0
                contrarian = False
                note = f"样本不足{n}<{self.min_sample},中性"
            elif lower > 0.5:
                # Wilson CI 下界 > 0.5 → 统计显著优于随机 → 加权
                weight = min(1.5, 1.0 + (lower - 0.5) * 2)
                contrarian = False
                note = f"命中{hit_rate:.0%}(n={n}),加权"
            elif upper < 0.5:
                # Wilson CI 上界 < 0.5 → 统计显著反指 → 降权 + 标注
                weight = max(0.3, 1.0 - (0.5 - upper) * 2)
                contrarian = True
                note = f"⚠️反指{hit_rate:.0%}(n={n}),降权"
            else:
                # 区间跨越 0.5：统计上无法区分是否优于随机 → 中性
                weight = 1.0
                contrarian = False
                note = f"命中{hit_rate:.0%}(n={n}),区间含50%,中性"

            table[kind] = KindEfficacy(
                kind=kind,
                n=n,
                hits=hits,
                hit_rate=hit_rate,
                lower=lower,
                upper=upper,
                weight=weight,
                contrarian=contrarian,
                note=note,
            )

        self._table = table
        return table

    def weight_of(self, kind: str) -> float:
        """返回该 kind 的自适应权重；未刷新/无记录时返回 1.0（安全默认）。"""
        e = self._table.get(kind)
        return e.weight if e is not None else 1.0

    def label_of(self, kind: str) -> str:
        """返回简短标注供推送追加；无记录时返回空串（不影响原消息）。

        命中率基于 predictions.correct 字段，与 accuracy_report() 语义一致。
        示例：
          '[共识命中72%(n=72)]'
          '[⚠️跟庄反指29%(n=14)]'
        """
        e = self._table.get(kind)
        if e is None or e.n == 0:
            return ""
        if e.contrarian:
            return f"[⚠️{kind}反指{e.hit_rate:.0%}(n={e.n})]"
        return f"[{kind}命中{e.hit_rate:.0%}(n={e.n})]"

    def is_contrarian(self, kind: str) -> bool:
        """该 kind 是否统计显著反指。未知时返回 False（保守）。"""
        e = self._table.get(kind)
        return e.contrarian if e is not None else False

    def fmt(self) -> str:
        """多行摘要：各 kind 命中率/区间/权重/标注，供周期推送/控制台。

        hit_rate 基于 predictions.correct 字段（与 accuracy_report() 一致）。
        若 _table 为空返回 '(无已评估预测数据)'。
        """
        if not self._table:
            return "(无已评估预测数据)"
        lines: list[str] = []
        for kind, e in sorted(self._table.items()):
            ci = f"[{e.lower:.2f},{e.upper:.2f}]"
            arrow = "⚠️反指" if e.contrarian else ("↑加权" if e.weight > 1.0 else "→中性")
            lines.append(
                f"  {kind}: {e.hits}/{e.n} 命中 ({e.hit_rate:.0%}) "
                f"CI{ci} 权重×{e.weight:.2f} {arrow} | {e.note}"
            )
        return "\n".join(lines)
