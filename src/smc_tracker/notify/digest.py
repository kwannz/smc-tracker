"""HL 事件**分类聚合**汇总器：把零散 HL 事件按分类收集，周期渲染成**一张**分类汇总卡片文本。

产品意图（用户「信息过多，核心还是 HL，分类集中在分类卡片汇总」）：
  事件级告警每条即时推会刷屏 → 改为按分类入缓冲，周期 flush 成一张分类汇总卡片
  （每类一个 section、核心抓庄信号在前、空类省略、单类超量截断标注），降噪且信息集中。
高优先级（超级共振/可疑地址）是否仍即时由 app 决定，本类只负责「分类聚合 + 渲染」纯逻辑，可测。
"""
from __future__ import annotations

from ..util import fmt_ts


def _fmt_usd(v: float) -> str:
    """名义金额紧凑格式（$1.50M / $278K / $640），取绝对值（符号由调用方处理）。"""
    a = abs(float(v))
    if a >= 1e9:
        return f"${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"${a / 1e6:.2f}M"
    if a >= 1e3:
        return f"${a / 1e3:.0f}K"
    return f"${a:.0f}"

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

    __slots__ = ("_buf", "_walls", "max_per_cat")

    def __init__(self, max_per_cat: int = 8) -> None:
        self.max_per_cat = max(1, max_per_cat)
        self._buf: dict[str, list[str]] = {}
        # 挂单墙单独**结构化聚合**（用户#：不要逐条原始事件，要按币 bid/ask 净意图 + 整体分析）
        # coin -> {bid_ntl, ask_ntl, bid_n, ask_n, px}
        self._walls: dict[str, dict[str, float]] = {}

    def add(self, category: str, line: str) -> None:
        """把一条 HL 事件明细按分类入缓冲。未知分类静默忽略（数据质量守卫，不抛异常）。

        挂单墙不走此路（须用 add_wall 结构化聚合）；其余分类逐条入缓冲。
        防内存膨胀：单类缓冲超 4×max_per_cat 时裁到最新（render 报真实总数，故仅硬上限保护）。
        """
        if category not in _KNOWN or category == "wall" or not line:
            return
        lst = self._buf.setdefault(category, [])
        lst.append(line)
        cap = self.max_per_cat * 4
        if len(lst) > cap:
            del lst[: len(lst) - cap]

    def add_wall(self, coin: str, side: str, notional: float, px: float = 0.0) -> None:
        """挂单墙**按币聚合**：累加该币 bid/ask 墙名义额与计数（render 出单币净意图，非逐条）。"""
        if not coin or side not in ("bid", "ask"):
            return
        w = self._walls.setdefault(
            coin, {"bid_ntl": 0.0, "ask_ntl": 0.0, "bid_n": 0, "ask_n": 0, "px": 0.0})
        w[f"{side}_ntl"] += float(notional)
        w[f"{side}_n"] += 1
        if px:
            w["px"] = float(px)

    def pending(self) -> int:
        """当前缓冲内事件总数（含挂单墙原始墙数；供 app 判断是否需要推送）。"""
        walls = sum(int(w["bid_n"] + w["ask_n"]) for w in self._walls.values())
        return sum(len(v) for v in self._buf.values()) + walls

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
            if key == "wall":
                lines.extend(self._render_walls(title))   # 挂单墙：按币聚合 + 整体分析
                continue
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
        self._walls.clear()
        return "\n".join(lines)

    @staticmethod
    def _net_tag(net: float) -> str:
        """净额 → 意图标注：正=净 bid(支撑/吸筹)，负=净 ask(压制/分销)，零=均衡。"""
        if net > 0:
            return f"净🟢bid {_fmt_usd(net)}（支撑/吸筹意图）"
        if net < 0:
            return f"净🔴ask {_fmt_usd(net)}（压制/分销意图）"
        return "bid/ask 均衡"

    def _render_walls(self, title: str) -> list[str]:
        """挂单墙 section：**整体分析 + 单一币种总结**（按币聚合 bid/ask 净意图），替代逐条原始事件。"""
        if not self._walls:
            return []
        coins = sorted(self._walls.items(),
                       key=lambda kv: kv[1]["bid_ntl"] + kv[1]["ask_ntl"], reverse=True)
        total_bid = sum(w["bid_ntl"] for _, w in coins)
        total_ask = sum(w["ask_ntl"] for _, w in coins)
        n_walls = sum(int(w["bid_n"] + w["ask_n"]) for _, w in coins)
        out = [
            f"\n【{title}】{len(coins)} 币 / {n_walls} 墙（领先意图·可能spoof，须与成交/OI 交叉验证）",
            f"  整体: {self._net_tag(total_bid - total_ask)}",
        ]
        for coin, w in coins:
            out.append(
                f"  • {coin}  🟢bid {_fmt_usd(w['bid_ntl'])}×{int(w['bid_n'])}"
                f" / 🔴ask {_fmt_usd(w['ask_ntl'])}×{int(w['ask_n'])}"
                f" → {self._net_tag(w['bid_ntl'] - w['ask_ntl'])}")
        return out
