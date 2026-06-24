"""Bitget 逐笔成交监控 —— taker 资金流加速度（flow_score），补 R2 唯一未接前瞻数据源。

设计（设计 v2 §2；QA 宇宙错配的最后一块）：
谐波宇宙的 flow_score（资金流加速度，FlowPredictor 里权重最高的前瞻分量）原先只有 HL meme 有；
本监控订阅 Bitget public `trade` channel（ws_client 已支持任意 channel），累积 per-coin taker
净流向（买 +、卖 −）喂 FlowPredictor，flow_score = tanh(资金流加速度) ∈[-1,1]。

诚实/稳健（第一性原理：上线前先稳健处理两种可能格式，实证后收敛）：
- 防御性解析 dict 行 {"price","size","side"} 与 list 行 [ts,price,size,side]；脏值经 to_float 拒。
- 无样本 → flow_score 返 None（forward_mult 缺数据=中性，不佯装）。
- 热路径友好：on_trade 仅累积内存 deque，不写库、不阻塞。
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable

from ..bitget.ws_client import BitgetSub
from ..signals.flow_predictor import FlowPredictor
from ..util import to_float as _f

_DEAD_ZONE: float = 0.05   # flow_score 死区：|score|<此值视为噪声→0（M1 修复）


def parse_trade_delta(data: list) -> float:
    """从 Bitget trade channel 的 data 解析签名净流向 USD（买 +、卖 −）。"""
    net = 0.0
    for row in data or ():
        if isinstance(row, dict):
            price = _f(row.get("price"))
            size = _f(row.get("size"))
            side = str(row.get("side", "")).lower()
        elif isinstance(row, (list, tuple)) and len(row) >= 4:
            # [ts, price, size, side]
            price = _f(row[1])
            size = _f(row[2])
            side = str(row[3]).lower()
        else:
            continue
        if price <= 0.0 or size <= 0.0:
            continue
        notional = price * size
        net += notional if side in ("buy", "b") else -notional
    return net


class BitgetTradeMonitor:
    """订阅 Bitget trade channel，累积 taker 净流向 → flow_score。"""

    __slots__ = ("_sym2coin", "ws", "_fp", "_seen", "_now_fn", "_last_px")

    def __init__(
        self,
        sym2coin: dict[str, str],
        ws: Any | None = None,
        accel_scale: float = 100_000.0,
        window_ms: int = 600_000,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        self._sym2coin = dict(sym2coin)            # {bitget_symbol: coin}
        self.ws = ws
        # C.2: min_accel_samples=1（最宽松）：BitgetTradeMonitor 面向实际成交，
        # 样本到达时间不规则；只要有至少 1 个非空 bin 即计算加速度（flow_score 有死区兜底）。
        self._fp = FlowPredictor(accel_scale=accel_scale, window_ms=window_ms,
                                 min_accel_samples=1)
        self._seen: set[str] = set()               # 已收到成交的 coin
        self._now_fn = now_fn or (lambda: int(time.time() * 1000))
        self._last_px: dict[str, float] = {}       # coin -> 最新成交价（供 forming 逼近用实时价）

    def attach(self) -> None:
        """订阅所有 symbol 的 trade channel（ws 已连前后均可，subscribe 自处理）。"""
        if self.ws is None:
            return
        for sym in self._sym2coin:
            self.ws.subscribe(BitgetSub(channel="trade", inst_id=sym), self.on_trade)

    def record(self, coin: str, delta_usd: float, ts: int) -> None:
        """累积一笔（或一批）净流向样本。"""
        self._fp.push(coin, delta_usd, ts)
        self._seen.add(coin)

    def on_trade(self, arg: dict, data: list, recv_ns: int, now_ms: int | None = None) -> None:
        """WS handler：instId→coin，解析净流向累积。失败不崩（ws_client 已兜底）。"""
        inst = (arg or {}).get("instId", "")
        coin = self._sym2coin.get(inst)
        if coin is None:
            return
        delta = parse_trade_delta(data)
        ts = now_ms if now_ms is not None else self._now_fn()
        self.record(coin, delta, ts)
        # 记录最新成交价（最后一笔有效价），供 forming 逼近检测用实时 Bitget 价
        for row in data or ():
            if isinstance(row, dict):
                p = _f(row.get("price"))
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                p = _f(row[1])
            else:
                continue
            if p > 0:
                self._last_px[coin] = p

    def last_price(self, coin: str) -> float | None:
        """最新成交价；未收到成交→None。"""
        return self._last_px.get(coin)

    def flow_score(self, coin: str, now_ms: int | None = None) -> float | None:
        """资金流加速度信号 ∈[-1,1]（正=加速流入=看涨）；无样本/不足→None；噪声级→0（死区）。

        C.2: flow_acceleration 返回 float|None（样本不足降权 None）→ 直接透传 None。
        """
        if coin not in self._seen:
            return None
        now = now_ms if now_ms is not None else self._now_fn()
        accel = self._fp.flow_acceleration(coin, now)
        # C.2: 样本不足 → 返回 None（诚实降权，不伪造 0 加速度）
        if accel is None:
            return None
        score = max(-1.0, min(1.0, math.tanh(accel / self._fp.accel_scale)))
        # 死区（M1 修复）：与 FlowPredictor.threshold 哲学一致，噪声级加速度不当前瞻信号
        return 0.0 if abs(score) < _DEAD_ZONE else score
