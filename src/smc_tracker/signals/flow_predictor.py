"""前瞻资金流预测 —— 不看历史成交记录，看「资金正在往哪positioning」(理论前瞻;实测见下方诚实警示)。

第一性原理（前瞻 > 回看）：
1. **订单簿失衡(l2Book)**：挂单是「尚未成交的意图」，资金已就位但还没动 → 理论比成交记录早一步。
2. **资金流加速度(2阶导)**：净流向的「加速度」理论领先于价格(流入是否在加速)。
3. **OI 速度**：持仓正在快速建立 = 大资金正在布局。
三者同向 → 押注该 coin 即将的方向(positioning 先于 price)。

**⚠️ 实测诚实警示(#167/#168,产线样本外)**：聚合到币级、在本数据上，**方向预测力近乎为零**——
聪明钱净流向 corr~0(#167)、流加速度 corr-0.04(#168,n=12小样本,求导未优于水平、若有也微弱反向)、
OI velocity corr+0.02。微观结构"订单流领先价格"是理论,但在此聚合粒度未兑现。系统真 edge 在**幅度**
(波动水平#153、pump 12-71×、谐波+0.5R #162-165)非**方向**。本预测器宜当弱上下文/确认,勿当强方向预测。
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..util import to_float as _f


def orderbook_imbalance(bids: list, asks: list, depth: int = 15) -> dict[str, float]:
    """l2Book 前 depth 档买卖盘名义深度失衡 ∈[-1,1]（正=买盘挂单占优=前瞻看涨）。
    bids/asks 为 [{'px','sz',...}, ...]。
    REST 降级保留函数（WS 有逐帧时优先用 orderbook_monitor.book_intent）。
    数据质量：使用 to_float 安全解析，拒 NaN/inf，不裸下标。
    """
    bd = sum(_f(b.get("px")) * _f(b.get("sz")) for b in bids[:depth])
    ad = sum(_f(a.get("px")) * _f(a.get("sz")) for a in asks[:depth])
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
                 window_ms: int = 600_000,
                 ema_alpha: float = 0.3,
                 min_accel_samples: int = 8) -> None:
        self.accel_scale = accel_scale
        self.threshold = threshold
        self.window_ms = window_ms        # 速度计算窗口(半窗用于加速度)
        self.ema_alpha = ema_alpha        # C.2: EMA 平滑系数（越大越快收敛）
        self.min_accel_samples = min_accel_samples  # C.2: 最少非空 bin 数；不足→降权
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

    def flow_acceleration(self, coin: str, now_ms: int) -> float | None:
        """C.2 EMA 预平滑后求加速度（后半 − 前半 EMA 均值）。

        1. 把窗口内样本聚合成 n_bins=10 个等宽 bin 的净流向速度序列（numpy 向量化）。
        2. 非空 bin 数 < min_accel_samples → 返回 None（样本不足，诚实降权）。
        3. 序列过在线 EMA(alpha) → 取后半均值 − 前半均值 = 平滑加速度。

        注：前后半为 trailing 时序（后半 = 近期），非居中窗。
        返回 float | None；调用方需处理 None（app.py: abs(... or 0.0)）。
        """
        t0 = now_ms - self.window_ms
        buf = self._flow[coin]
        if not buf:
            return None

        # 聚合成 n_bins 个等宽 bin
        n_bins = 10
        bin_ms = max(self.window_ms // n_bins, 1)
        bin_totals = np.zeros(n_bins, dtype=float)
        bin_counts = np.zeros(n_bins, dtype=int)
        for ts, d in buf:
            if ts < t0 or ts >= now_ms:
                continue
            idx = int((ts - t0) // bin_ms)
            if 0 <= idx < n_bins:
                bin_totals[idx] += d
                bin_counts[idx] += 1

        # 非空 bin 数不足 → 样本不足
        nonempty = int(np.sum(bin_counts > 0))
        if nonempty < self.min_accel_samples:
            return None

        # 各 bin 净流向速度（$/min）；空 bin 补 0
        bin_dur_min = bin_ms / 60_000.0
        vel_seq = bin_totals / bin_dur_min  # shape (n_bins,)

        # EMA 平滑（在线递推，trailing 非居中）
        alpha = self.ema_alpha
        ema = np.empty(n_bins, dtype=float)
        ema[0] = vel_seq[0]
        for i in range(1, n_bins):
            ema[i] = alpha * vel_seq[i] + (1.0 - alpha) * ema[i - 1]

        # 前后半均值差 = 平滑加速度（后半 bins 是近期）
        half = n_bins // 2
        prior_mean = float(np.mean(ema[:half]))
        recent_mean = float(np.mean(ema[half:]))
        return recent_mean - prior_mean

    def predict(self, coin: str, now_ms: int, book_imbalance: float = 0.0,
                oi_velocity: float = 0.0) -> FlowPrediction | None:
        vel = self.flow_velocity(coin, now_ms)
        # C.2: flow_acceleration 可能返回 None（样本不足）
        accel_raw = self.flow_acceleration(coin, now_ms)
        accel = accel_raw if accel_raw is not None else 0.0

        # 各前瞻分量 [-1,1]
        accel_sig = math.tanh(accel / self.accel_scale)
        if accel_raw is None:
            accel_sig = 0.0   # 样本不足时加速度分量不参与（降权到 0）
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
        if accel_raw is None:
            parts.append("流加速样本不足")
        elif abs(accel_sig) > 0.1:
            parts.append("资金流" + ("加速流入" if accel > 0 else "加速流出"))
        if abs(book_imbalance) > 0.1:
            parts.append("挂单" + ("买盘厚" if book_imbalance > 0 else "卖盘厚"))
        if abs(oi_sig) > 0.1:
            parts.append("OI" + ("快增" if oi_velocity > 0 else "快减"))
        return FlowPrediction(coin, direction, score, vel, accel, book_imbalance,
                              oi_velocity, " + ".join(parts) or "多因子前瞻", now_ms)
