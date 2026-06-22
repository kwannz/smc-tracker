"""HL 事件**分类聚合**汇总器：把零散 HL 事件按分类收集，周期渲染成**一张**分类汇总卡片文本。

产品意图（用户「信息过多，核心还是 HL，分类集中在分类卡片汇总」）：
  事件级告警每条即时推会刷屏 → 改为按分类入缓冲，周期 flush 成一张分类汇总卡片
  （每类一个 section、核心抓庄信号在前、空类省略、单类超量截断标注），降噪且信息集中。
高优先级（超级共振/可疑地址）是否仍即时由 app 决定，本类只负责「分类聚合 + 渲染」纯逻辑，可测。
"""
from __future__ import annotations

from ..util import fmt_ts

# 有序分类：决定卡片内 section 顺序——核心抓庄信号(跟庄/超级/共振/共识)在前，
# 领先意图(挂单墙)、行情衍生(暴涨/TA)、辅助(持仓)在后，符合阅读优先级。
_CATEGORIES: list[tuple[str, str]] = [
    ("whale", "🐋 跟庄信号"),
    ("super", "🌟 超级共振"),
    ("signal", "⚡ SMC 共振"),
    ("consensus", "🤝 庄家共识"),
    ("divergence", "🔀 背离"),
    ("suspicious", "🚨 可疑地址"),
    ("wall", "🧱 挂单墙"),
    ("pump", "🚀 暴涨暴跌"),
    ("ta", "📐 TA 信号"),
    ("position", "📊 持仓变化"),
]
_KNOWN = {k for k, _ in _CATEGORIES}


class HLDigest:
    """HL 事件分类聚合缓冲。add(分类, 明细行) 收集；render(now_ms) 渲染汇总卡片文本并清空。"""

    __slots__ = ("_buf", "max_per_cat")

    def __init__(self, max_per_cat: int = 8) -> None:
        self.max_per_cat = max(1, max_per_cat)
        self._buf: dict[str, list[str]] = {}

    def add(self, category: str, line: str) -> None:
        """把一条 HL 事件明细按分类入缓冲。未知分类静默忽略（数据质量守卫，不抛异常）。

        防内存膨胀：单类缓冲超 2×max_per_cat 时裁到最新 max_per_cat（render 仍报真实总数前需先记数，
        故此处仅做硬上限保护，正常一周期内远不及）。
        """
        if category not in _KNOWN or not line:
            return
        lst = self._buf.setdefault(category, [])
        lst.append(line)
        cap = self.max_per_cat * 4
        if len(lst) > cap:
            del lst[: len(lst) - cap]

    def pending(self) -> int:
        """当前缓冲内事件总数（供 app 判断是否需要推送）。"""
        return sum(len(v) for v in self._buf.values())

    def render(self, now_ms: int = 0) -> str | None:
        """渲染**一张**分类汇总卡片文本并清空缓冲；无任何事件返回 None（不推空卡）。"""
        total = self.pending()
        if total == 0:
            return None
        lines: list[str] = [
            f"🦅 HL 抓庄分类汇总 [{fmt_ts(now_ms)}]",
            f"近窗共 {total} 条 HL 事件（按分类汇总，已降噪去刷屏）",
        ]
        for key, title in _CATEGORIES:
            items = self._buf.get(key)
            if not items:
                continue
            shown = items[-self.max_per_cat:]
            omitted = len(items) - len(shown)
            head = f"\n【{title}】{len(items)} 条"
            if omitted > 0:
                head += f"（显示最新 {len(shown)}，省略 {omitted}）"
            lines.append(head)
            lines.extend(f"  • {x}" for x in shown)
        self._buf.clear()
        return "\n".join(lines)
