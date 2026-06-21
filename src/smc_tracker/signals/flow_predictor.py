"""前瞻资金流预测 —— 不看历史成交记录，看「资金正在往哪positioning」。

第一性原理（前瞻 > 回看）：
1. **订单簿失衡(l2Book)**：挂单是「尚未成交的意图」，资金已就位但还没动 → 比成交记录早一步。
2. **资金流加速度(2阶导)**：净流向的「加速度」领先于价格 —— 不是「已经流入多少」(回看)，
   而是「流入是否在加速」(前瞻)。
3. **OI 速度**：持仓正在快速建立 = 大资金正在布局。
三者同向 → 预测该 coin 即将的方向(positioning 先于 price)。
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any


def orderbook_imbalance(bids: list, asks: list, depth: int = 15) -> dict[str, float]:
    """l2Book 前 depth 档买卖盘名义深度失衡 ∈[-1,1]（正=买盘挂单占优=前瞻看涨）。
    bids/asks 为 [{'px','sz',...}, ...]。"""
    bd = sum(float(b["px"]) * float(b["sz"]) for b in bids[:depth])
    ad = sum(float(a["px"]) * float(a["sz"]) for a in asks[:depth])
    tot = bd + ad
    return {"imbalance": (bd - ad) / tot if tot else 0.0, "bid_usd": bd, "ask_usd": ad}


@dataclass(slots=True)
class FlowPrediction:
    coin: str
    direction: str            # 'long' / 'short'（预测方向）
    score: float              # 前瞻置信 [-1,1]
    flow_velocity: float      # 资金流速 $/min
    flow_accel: float         # 资金流加速度
    book_imbalance: float     # 订单簿失衡
    oi_velocity: float        # OI 速度
    reason: str
    ts: int

    def fmt(self) -> str:
        d = "🟢预测上行" if self.direction == "long" else "🔴预测下行"
        return (f"🔮前瞻 {d} {self.coin} 置信={abs(self.score):.2f} | "
                f"资金流速${self.flow_velocity:,.0f}/min 加速{self.flow_accel:+,.0f} "
                f"挂单失衡{self.book_imbalance:+.2f} OI速{self.oi_velocity:+.1%} | {self.reason}")


class FlowPredictor:
    def __init__(self, accel_scale: float = 100_000.0, threshold: float = 0.35,
                 window_ms: int = 600_000) -> None:
        self.accel_scale = accel_scale
        self.threshold = threshold
        self.window_ms = window_ms        # 速度计算窗口(半窗用于加速度)
        self._flow: dict[str, deque] = defaultdict(lambda: deque(maxlen=2000))

    def push(self, coin: str, net_delta_usd: float, ts: int) -> None:
        """累加一笔净流向样本(taker 买为正卖为负)。"""
        self._flow[coin].append((ts, net_delta_usd))

    def _vel(self, coin: str, t0: int, t1: int) -> float:
        """[t0,t1) 内净流向 / 分钟。"""
        s = sum(d for ts, d in self._flow[coin] if t0 <= ts < t1)
        minutes = max((t1 - t0) / 60000, 1e-9)
        return s / minutes

    def flow_velocity(self, coin: str, now_ms: int) -> float:
        return self._vel(coin, now_ms - self.window_ms, now_ms)

    def flow_acceleration(self, coin: str, now_ms: int) -> float:
        """近半窗速度 − 前半窗速度（>0=流入在加速，前瞻看涨）。"""
        half = self.window_ms // 2
        recent = self._vel(coin, now_ms - half, now_ms)
        prior = self._vel(coin, now_ms - self.window_ms, now_ms - half)
        return recent - prior

    def predict(self, coin: str, now_ms: int, book_imbalance: float = 0.0,
                oi_velocity: float = 0.0) -> FlowPrediction | None:
        vel = self.flow_velocity(coin, now_ms)
        accel = self.flow_acceleration(coin, now_ms)
        # 各前瞻分量 [-1,1]
        accel_sig = math.tanh(accel / self.accel_scale)
        oi_sig = max(-1.0, min(1.0, oi_velocity / 0.05))     # 5% OI 速度满分
        # 加权：加速度(领先) 0.45 + 挂单意图 0.35 + OI 0.20
        score = 0.45 * accel_sig + 0.35 * book_imbalance + 0.20 * oi_sig
        if abs(score) < self.threshold:
            return None
        # 要求加速度与挂单意图不矛盾(同向或一方近零)，避免假预测
        if accel_sig * book_imbalance < -0.04:
            return None
        direction = "long" if score > 0 else "short"
        parts = []
        if abs(accel_sig) > 0.1:
            parts.append("资金流" + ("加速流入" if accel > 0 else "加速流出"))
        if abs(book_imbalance) > 0.1:
            parts.append("挂单" + ("买盘厚" if book_imbalance > 0 else "卖盘厚"))
        if abs(oi_sig) > 0.1:
            parts.append("OI" + ("快增" if oi_velocity > 0 else "快减"))
        return FlowPrediction(coin, direction, score, vel, accel, book_imbalance,
                              oi_velocity, " + ".join(parts) or "多因子前瞻", now_ms)
