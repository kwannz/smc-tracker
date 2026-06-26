"""多庄共识信号：多个聪明钱(庄)同时同向押注同一 coin = 强信号。

第一性原理：单个庄可能看错，但多个**相互独立**的顶级交易员在同一标的同向，是高置信信号。
聚合所有监控地址的当前持仓(AddressMonitor.all_positions)，按 coin 统计多空人数与净名义，
当某 coin 出现明显多数共识(人数达阈值且多数方≥2×少数方、净名义够大)即出信号。

同时产出「庄持仓面板」：每个 coin 谁在多/空、净名义多少。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(slots=True)
class CoinPositioning:
    coin: str
    n_long: int
    n_short: int
    net_notional: float          # 带符号净名义 USD（多正空负，用于面板）
    long_notional: float = 0.0   # 多头侧名义合计（正）
    short_notional: float = 0.0  # 空头侧名义合计（正）
    long_labels: list[str] = field(default_factory=list)
    short_labels: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConsensusSignal:
    coin: str
    direction: str               # 'long' / 'short'
    n_agree: int
    n_oppose: int
    net_notional: float
    score: float
    labels: list[str]
    ts: int

    def fmt(self) -> str:
        d = "做多🟢" if self.direction == "long" else "做空🔴"
        who = "、".join(self.labels[:4]) + ("…" if len(self.labels) > 4 else "")
        return (f"🤝多庄共识 {d} {self.coin} {self.n_agree}庄同向(对{self.n_oppose}) "
                f"净${self.net_notional:,.0f} 分={self.score:.2f} | {who}")


ConsensusCallback = Callable[[ConsensusSignal], Any]


def positioning(positions: dict[tuple[str, str], float], prices: dict[str, float],
                labels: dict[str, str]) -> list[CoinPositioning]:
    """聚合每个 coin 的多空持仓面板（按 |净名义| 降序）。"""
    by_coin: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"long": [], "short": [], "net": 0.0, "ln": 0.0, "sn": 0.0})
    for (addr, coin), szi in positions.items():
        px = prices.get(coin)
        if not px or szi == 0:
            continue
        notional = szi * px
        e = by_coin[coin]
        e["net"] += notional
        lbl = labels.get(addr, addr[:8])
        if szi > 0:
            e["long"].append(lbl); e["ln"] += notional
        else:
            e["short"].append(lbl); e["sn"] += -notional
    out = [CoinPositioning(coin=c, n_long=len(e["long"]), n_short=len(e["short"]),
                           net_notional=e["net"], long_notional=e["ln"],
                           short_notional=e["sn"], long_labels=e["long"],
                           short_labels=e["short"])
           for c, e in by_coin.items()]
    out.sort(key=lambda p: abs(p.net_notional), reverse=True)
    return out


class WhaleConsensus:
    """多庄同向共识信号。

    ⚠**#187→#188 共识放大假设 UNESTABLISHED(诚实标注,维持现状)**:#187 初测似"双庄前瞻 alpha 峰值、≥3 反转"非单调,
    但 #188 加中位数+bootstrap+扩样本复核**推翻**——双庄 24h alpha **+7.1%↔−6.0% 跨运行符号翻转**(共识事件集中少数币,
    结果由"哪些币 candle 加载"主导,HL 限流致不稳定)。**当前数据无法确立共识是否放大信号(既未证实也未证伪)**。
    故 min_consensus=3 / score∝n_agree **维持不改**(#187 "设计反向"宣称已撤回);需更大无限流样本+per-coin 控制再议。
    脚本 scripts/audit_consensus_strength.py。
    """

    def __init__(self, store: Any | None = None, min_consensus: int = 3,
                 min_net_notional: float = 200_000.0, cooldown_ms: int = 1_800_000,
                 on_signal: ConsensusCallback | None = None) -> None:
        self.store = store
        self.min_consensus = min_consensus
        self.min_net_notional = min_net_notional
        self.cooldown_ms = cooldown_ms
        self.on_signal = on_signal
        self._last: dict[tuple[str, str], int] = {}     # (coin,direction) -> last_ts
        self.signals_emitted = 0

    def scan(self, positions: dict[tuple[str, str], float], prices: dict[str, float],
             labels: dict[str, str], now_ms: int) -> list[ConsensusSignal]:
        out: list[ConsensusSignal] = []
        for p in positioning(positions, prices, labels):
            n_agree = max(p.n_long, p.n_short)
            n_oppose = min(p.n_long, p.n_short)
            if n_agree < self.min_consensus:
                continue
            # 明显多数：多数方至少是少数方 2 倍
            if not (n_agree >= 2 * n_oppose):
                continue
            direction = "long" if p.n_long > p.n_short else "short"
            # 用同向一方的名义合计（与方向一致，避免反向大仓位造成矛盾）
            agree_notional = p.long_notional if direction == "long" else p.short_notional
            if agree_notional < self.min_net_notional:
                continue
            key = (p.coin, direction)
            if key in self._last and now_ms - self._last[key] < self.cooldown_ms:
                continue
            score = min(n_agree / 8.0 + math.tanh(agree_notional / 5_000_000), 1.0)
            sig = ConsensusSignal(
                coin=p.coin, direction=direction, n_agree=n_agree, n_oppose=n_oppose,
                net_notional=agree_notional, score=score,
                labels=(p.long_labels if direction == "long" else p.short_labels),
                ts=now_ms)
            self._last[key] = now_ms
            self.signals_emitted += 1
            if self.store is not None:
                self.store.insert_consensus((
                    sig.ts, sig.coin, sig.direction, sig.n_agree, sig.n_oppose,
                    sig.net_notional, sig.score, "、".join(sig.labels[:6])))
            if self.on_signal is not None:
                self.on_signal(sig)
            out.append(sig)
        return out
