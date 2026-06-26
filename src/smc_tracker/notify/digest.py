"""HL 事件**分类聚合**汇总器：把零散 HL 事件按分类收集，周期渲染成**一张**分类汇总卡片文本。

产品意图（用户「信息过多，核心还是 HL，分类集中在分类卡片汇总」）：
  事件级告警每条即时推会刷屏 → 改为按分类入缓冲，周期 flush 成一张分类汇总卡片
  （每类一个 section、核心抓庄信号在前、空类省略、单类超量截断标注），降噪且信息集中。
高优先级（超级共振/可疑地址）是否仍即时由 app 决定，本类只负责「分类聚合 + 渲染」纯逻辑，可测。
"""
from __future__ import annotations

from ..util import fmt_ts


def _fmt_usd(v: float) -> str:
    """名义金额紧凑格式（$1.50M / $278K / $640），取绝对值（符号由调用方处理）。

    **有意不复用 util.fmt_usd**（#134 核实）：本函数是推送卡片专用契约——$ 前缀 + 绝对值 +
    整数 K（.0f）；util.fmt_usd(style='en') 是信号证据契约——无 $ + 保留符号 + .1f K + 小额走
    fmt_px。两者格式不同，盲合会改变推送卡片输出并破坏测试，故保留本地实现（同名≠重复）。
    """
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

    __slots__ = ("_buf", "_walls", "_bias", "max_per_cat")

    def __init__(self, max_per_cat: int = 8) -> None:
        self.max_per_cat = max(1, max_per_cat)
        self._buf: dict[str, list[str]] = {}
        # 挂单墙单独**结构化聚合**（用户#：不要逐条原始事件，要按币 bid/ask 净意图 + 整体分析）
        # coin -> {bid_ntl, ask_ntl, bid_n, ask_n, px}
        self._walls: dict[str, dict[str, float]] = {}
        # 币种多空比例（用户#：推送按币种多空比例组织）coin -> {"bull":n, "bear":n, "srcs":set}
        self._bias: dict[str, dict] = {}

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

    def add_bias(self, coin: str, bull: bool, source: str) -> None:
        """按币累计**多空方向**（用户#：推送按币种多空比例组织）。

        bull=True 计 1 票多(long/up/bullish/bid)，False 计 1 票空；source=信号源短标签(跟庄/SMC/背离…)。
        与各信号 _emit 同步调用（见 app），render 出每币 多/空计数 + 倾向 + 共识来源。
        """
        if not coin:
            return
        b = self._bias.setdefault(coin, {"bull": 0, "bear": 0, "srcs": set()})
        b["bull" if bull else "bear"] += 1
        if source:
            b["srcs"].add(source)

    def pending(self) -> int:
        """当前缓冲内事件总数（含挂单墙原始墙数；供 app 判断是否需要推送）。"""
        walls = sum(int(w["bid_n"] + w["ask_n"]) for w in self._walls.values())
        return sum(len(v) for v in self._buf.values()) + walls

    def render(self, now_ms: int = 0) -> str | None:
        """渲染**一张**分类汇总卡片文本并清空缓冲；无任何事件返回 None（不推空卡）。

        头部先出**币种多空比例**（用户#：按币种多空比例组织内容），再出各分类明细作证据。
        """
        if not (self._buf or self._walls or self._bias):
            return None
        total = self.pending()
        lines: list[str] = [
            f"🦅 HL 抓庄分类汇总 [{fmt_ts(now_ms)}]",
            f"近窗共 {total} 条 HL 事件（按币种多空比例聚合 + 分类明细，已降噪去刷屏）",
        ]
        lines.extend(self._render_bias())     # 头部：币种多空比例总览
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
        self._bias.clear()
        return "\n".join(lines)

    @staticmethod
    def _lean(bull_pct: int) -> str:
        """多空倾向标签：按多头占比分档（净多/偏多/分歧/偏空/净空），共识强弱一目了然。"""
        if bull_pct >= 80:
            return f"净多 {bull_pct}%"
        if bull_pct >= 60:
            return f"偏多 {bull_pct}%"
        if bull_pct > 40:
            return f"分歧 多{bull_pct}%"
        if bull_pct > 20:
            return f"偏空 {100 - bull_pct}%"
        return f"净空 {100 - bull_pct}%"

    def _render_bias(self) -> list[str]:
        """币种多空比例：合并各信号方向票 + 挂单墙净(bid=多/ask=空)，每币出 多/空计数 + 倾向 + 来源。"""
        tally: dict[str, dict] = {}
        for coin, b in self._bias.items():
            t = tally.setdefault(coin, {"bull": 0, "bear": 0, "srcs": set()})
            t["bull"] += b["bull"]
            t["bear"] += b["bear"]
            t["srcs"] |= b["srcs"]
        # 挂单墙净额计入多空（bid 净=支撑/吸筹=多；ask 净=压制/分销=空）
        for coin, w in self._walls.items():
            net = w["bid_ntl"] - w["ask_ntl"]
            if net == 0:
                continue
            t = tally.setdefault(coin, {"bull": 0, "bear": 0, "srcs": set()})
            t["bull" if net > 0 else "bear"] += 1
            t["srcs"].add("挂单墙")
        if not tally:
            return []
        # 按信号数(关注度)降序，取 Top（忙时防卡片过长；高活跃币=共识最值得看）
        ranked = sorted(tally.items(),
                        key=lambda kv: kv[1]["bull"] + kv[1]["bear"], reverse=True)
        cap = max(self.max_per_cat * 2, 12)
        shown, omitted = ranked[:cap], max(0, len(ranked) - cap)
        head = "\n【📊 币种多空比例】（聪明钱信号方向聚合，越偏=共识越强）"
        if omitted:
            head += f"（{len(ranked)} 币，显示活跃 Top {len(shown)}，省略 {omitted}）"
        out = [head]
        for coin, t in shown:
            tot = t["bull"] + t["bear"]
            pct = round(100 * t["bull"] / tot) if tot else 50
            srcs = "/".join(sorted(t["srcs"]))
            out.append(f"  • {coin}  🟢多{t['bull']} 🔴空{t['bear']} → {self._lean(pct)}（{srcs}）")
        return out

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
