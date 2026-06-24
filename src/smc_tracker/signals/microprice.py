"""微观结构盘口信号：OFI (Cont-Kukanov-Stoikov 2014) + queue imbalance + micro-price (Stoikov 2018).

**第一性原理实证（2026-06-24）**：
HL l2Book REST 确认格式：{"px":"61106.0","sz":"1.17158","n":14}
- bids: 降序排列（最高价在前）
- asks: 升序排列（最低价在前）
- sz 单位：coin 数量（非 USD）
- px/sz 均为字符串，需 to_float 解析

三件套（替换静态 orderbook_imbalance，防双计：合并为 book_intent 单一出口）：
1. queue_imbalance：前 depth 档 size 比（∈[-1,1]，正=买盘排队厚，前瞻看涨）
2. micro_price：Stoikov micro-price；tilt>0=micro 偏向 ask=买压（前瞻看涨）
3. ofi_delta：Cont-Kukanov-Stoikov L1 OFI 单帧增量（正=净买方订单流增加）

OFITracker：有状态聚合器，窗口内归一化 OFI，作合成前瞻分数。
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

import numpy as np

from ..util import to_float as _f

_EPS = 1e-9


def queue_imbalance(bids: list, asks: list, depth: int = 5) -> float:
    """买卖队列 size 失衡 ∈[-1,1]（正=买盘排队厚，前瞻看涨）。

    仅聚焦前 depth 档 size（非名义 USD），远档大额虚挂不影响结果。
    bids/asks: [{'px','sz',...}, ...]（px/sz 可为 str 或 float）。
    """
    bid_sz = sum(_f(b.get("sz")) for b in bids[:depth])
    ask_sz = sum(_f(a.get("sz")) for a in asks[:depth])
    tot = bid_sz + ask_sz
    if tot < _EPS:
        return 0.0
    return (bid_sz - ask_sz) / tot


def micro_price(bids: list, asks: list) -> dict[str, float]:
    """Stoikov micro-price（使用最优档 bid/ask）。

    micro = (bid_px*ask_sz + ask_px*bid_sz) / (bid_sz + ask_sz)
    tilt  = (micro - mid) / mid
    tilt > 0 = micro 偏 ask 侧 = 买方力量强（前瞻看涨）；
    tilt < 0 = micro 偏 bid 侧 = 卖方力量强。
    空盘/零量 → tilt=0（不除零）。

    返回 {"micro": float, "mid": float, "tilt": float}。
    """
    zero = {"micro": 0.0, "mid": 0.0, "tilt": 0.0}
    if not bids or not asks:
        return zero
    bid_px = _f(bids[0].get("px"))
    bid_sz = _f(bids[0].get("sz"))
    ask_px = _f(asks[0].get("px"))
    ask_sz = _f(asks[0].get("sz"))
    if bid_px <= 0 or ask_px <= 0:
        return zero
    mid = (bid_px + ask_px) / 2.0
    tot_sz = bid_sz + ask_sz
    if tot_sz < _EPS:
        return {"micro": mid, "mid": mid, "tilt": 0.0}
    micro = (bid_px * ask_sz + ask_px * bid_sz) / tot_sz
    tilt = (micro - mid) / mid if mid > _EPS else 0.0
    return {"micro": micro, "mid": mid, "tilt": tilt}


def ofi_delta(
    prev_bid: tuple[float, float],
    prev_ask: tuple[float, float],
    cur_bid: tuple[float, float],
    cur_ask: tuple[float, float],
) -> float:
    """Cont-Kukanov-Stoikov L1 OFI 单帧增量（正=净买方订单流增加，前瞻看涨）。

    参数均为 (px: float, sz: float)；任一 px 或 sz 无效（≤0 或 NaN）→ 返回 0.0。

    bid 方向增量 e_b：
        cur_bid_px > prev_bid_px → +cur_bid_sz  （新最优买价出现=净买单增）
        cur_bid_px == prev_bid_px → cur_bid_sz - prev_bid_sz
        cur_bid_px < prev_bid_px → -prev_bid_sz  （最优买价下移=买单撤）

    ask 方向增量 e_a（ask 方向相反：ask 下移=卖单增→压制买方）：
        cur_ask_px < prev_ask_px → +cur_ask_sz
        cur_ask_px == prev_ask_px → cur_ask_sz - prev_ask_sz
        cur_ask_px > prev_ask_px → -prev_ask_sz

    return e_b - e_a
    """
    pb_px, pb_sz = prev_bid
    pa_px, pa_sz = prev_ask
    cb_px, cb_sz = cur_bid
    ca_px, ca_sz = cur_ask

    # 任一关键值无效则返回 0
    import math as _math
    for v in (pb_px, pb_sz, pa_px, pa_sz, cb_px, cb_sz, ca_px, ca_sz):
        if not _math.isfinite(v) or v < 0:
            return 0.0

    # bid 增量
    if cb_px > pb_px:
        e_b = cb_sz
    elif cb_px == pb_px:
        e_b = cb_sz - pb_sz
    else:
        e_b = -pb_sz

    # ask 增量（ask 价格下移=卖方激进 → +e_a → 压制买方）
    if ca_px < pa_px:
        e_a = ca_sz
    elif ca_px == pa_px:
        e_a = ca_sz - pa_sz
    else:
        e_a = -pa_sz

    return e_b - e_a


@dataclass(slots=True)
class OFITracker:
    """有状态 OFI 聚合器（逐帧更新，窗口内归一化）。

    每帧调用 update(coin, bids, asks, ts) 得当帧 ofi_delta；
    normalized(coin, now_ms) 返回窗口内 Σofi/(Σ|ofi|+eps)∈[-1,1]，
    作合成前瞻盘口意图分数（正=净买方）。
    """
    # coin → (bid_px, bid_sz, ask_px, ask_sz) 上一帧最优档
    _prev: dict[str, tuple[float, float, float, float]] = field(
        default_factory=dict
    )
    # coin → deque[(ts_ms, ofi_delta)]
    _cum: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=500))
    )
    window_ms: int = 60_000

    def update(self, coin: str, bids: list, asks: list, ts: int) -> float:
        """处理新帧，返回当帧 ofi_delta（首帧无 prev → 记录并返回 0.0）。"""
        if not bids or not asks:
            return 0.0
        cb_px = _f(bids[0].get("px"))
        cb_sz = _f(bids[0].get("sz"))
        ca_px = _f(asks[0].get("px"))
        ca_sz = _f(asks[0].get("sz"))

        if coin not in self._prev:
            # 首帧：记录 prev，返回 0
            self._prev[coin] = (cb_px, cb_sz, ca_px, ca_sz)
            self._cum[coin].append((ts, 0.0))
            return 0.0

        pb_px, pb_sz, pa_px, pa_sz = self._prev[coin]
        delta = ofi_delta((pb_px, pb_sz), (pa_px, pa_sz),
                          (cb_px, cb_sz), (ca_px, ca_sz))
        self._prev[coin] = (cb_px, cb_sz, ca_px, ca_sz)
        self._cum[coin].append((ts, delta))
        return delta

    def normalized(self, coin: str, now_ms: int) -> float:
        """窗口内归一化 OFI ∈[-1,1]（正=净买方订单流占优）。"""
        cutoff = now_ms - self.window_ms
        buf = self._cum.get(coin)
        if not buf:
            return 0.0
        vals = np.array([d for ts, d in buf if ts >= cutoff], dtype=float)
        if vals.size == 0:
            return 0.0
        total = float(np.sum(vals))
        abs_total = float(np.sum(np.abs(vals)))
        return total / (abs_total + _EPS)
