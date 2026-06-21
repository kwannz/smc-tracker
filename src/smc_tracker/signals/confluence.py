"""多信号叠加共振：多个独立信号源在同一 coin 同向 = 超级信号。

各信号(跟庄/多庄共识/三源背离/SMC结构)是不同角度的独立判断；当它们指向同一 coin 同一方向时，
置信度远高于任一单独信号。本模块在时间窗内聚合各信号源，按 coin 统计同向源数，达阈值即出超级信号。

coin 命名跨表不一(kPEPE vs PEPE)，统一 normalize 后聚合。
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from ..memecoins import normalize

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ConfluenceSignal:
    coin: str
    direction: str               # 'long' / 'short'
    n_sources: int
    sources: list[str]           # 同向信号源名
    opposing: int                # 反向源数
    score: float
    ts: int

    def fmt(self) -> str:
        d = "做多🟢" if self.direction == "long" else "做空🔴"
        return (f"🌟超级信号 {d} {self.coin} {self.n_sources}源共振"
                f"[{'+'.join(self.sources)}] 分={self.score:.2f}"
                + (f" (反向{self.opposing})" if self.opposing else ""))


ConfluenceCallback = Callable[[ConfluenceSignal], Any]

# (表名, 信号源显示名, 取方向的函数)
_SOURCES = [
    ("whale_signals", "跟庄", lambda d: d),
    ("consensus", "共识", lambda d: d),
    ("signals", "SMC", lambda d: d),
    ("divergence", "背离", lambda d: "long" if d == "bullish" else "short"),
    # 前瞻是领先维度独立源(挂单意图先于成交)；direction 已是 long/short，直接透传
    ("flow_predictions", "前瞻", lambda d: d),
    # OKX 资金费×净流向背离；direction 已归一为 long/short，直接透传
    ("okx_signals", "OKX", lambda d: d),
]


class ConfluenceAggregator:
    def __init__(self, store: Any, window_ms: int = 3_600_000, min_sources: int = 2,
                 cooldown_ms: int = 1_800_000, on_signal: ConfluenceCallback | None = None) -> None:
        self.store = store
        self.window_ms = window_ms
        self.min_sources = min_sources
        self.cooldown_ms = cooldown_ms
        self.on_signal = on_signal
        self._last: dict[tuple[str, str], int] = {}
        self.signals_emitted = 0
        # 弱引用注入（set_efficacy 注入后生效；未注入时 score 退化为纯源数量公式）
        self.efficacy: Any | None = None

    def set_efficacy(self, efficacy: Any) -> None:
        """注入 SignalEfficacy 实例，弱耦合避免构造顺序依赖。"""
        self.efficacy = efficacy

    def scan(self, now_ms: int) -> list[ConfluenceSignal]:
        since = now_ms - self.window_ms
        by_coin: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: {"long": set(), "short": set()})
        c = self.store.conn
        for table, name, to_dir in _SOURCES:
            try:
                rows = c.execute(
                    f"SELECT coin, direction FROM {table} WHERE ts>=?", (since,)).fetchall()
            except sqlite3.OperationalError as exc:
                # 表不存在或 schema 漂移/锁，记日志后跳过该源，不吞其他异常
                log.warning("confluence.scan: 跳过源表 %r — %s", table, exc)
                continue
            for coin, d in rows:
                direction = to_dir(d)
                if direction in ("long", "short"):
                    by_coin[normalize(coin)][direction].add(name)

        out: list[ConfluenceSignal] = []
        for coin, s in by_coin.items():
            nl, ns = len(s["long"]), len(s["short"])
            n_agree = max(nl, ns)
            if n_agree < self.min_sources:
                continue
            if nl == ns:                       # 多空源数相等 → 矛盾，不出
                continue
            direction = "long" if nl > ns else "short"
            sources = sorted(s[direction])
            opposing = min(nl, ns)
            key = (coin, direction)
            if key in self._last and now_ms - self._last[key] < self.cooldown_ms:
                continue
            # 按各源历史命中率加权(高效源贡献大/反指源降权);无 efficacy 退化纯数量
            weighted_agree = sum(
                (self.efficacy.weight_of(src) if self.efficacy is not None else 1.0)
                for src in sources
            )
            score = min(0.5 + 0.2 * weighted_agree - 0.15 * opposing, 1.0)
            sig = ConfluenceSignal(coin=coin, direction=direction, n_sources=n_agree,
                                   sources=sources, opposing=opposing, score=score, ts=now_ms)
            self._last[key] = now_ms
            self.signals_emitted += 1
            if hasattr(self.store, "insert_confluence"):
                self.store.insert_confluence((
                    sig.ts, sig.coin, sig.direction, sig.n_sources,
                    "+".join(sig.sources), sig.opposing, sig.score))
            if self.on_signal is not None:
                self.on_signal(sig)
            out.append(sig)
        return out
