"""共振信号引擎（第一性原理，可解释打分）。

核心思想：**SMC 结构方向 与 聪明钱主动流向 必须一致（共振）才出信号**，
OI 异动与链上大额转账作为「信心加成」放大分数。结构突破（BOS/CHoCH）是触发点，
流向/OI/链上是触发时的环境状态。

打分（正多负空）：
  structure_bias ∈ {±1.0(BOS), ±0.7(CHoCH)}      —— 来自最近一次结构事件
  flow_bias      = tanh(净主动流向USD / flow_scale) ∈ (-1,1)
  若 structure_bias 与 flow_bias 异号或任一为 0 → 无共振，不出信号
  base       = (|structure_bias| + |flow_bias|) / 2
  conviction = 1 + min(|oi变化%| / 0.05, 1)*0.3 + (0.2 若近窗有≥onchain_min 的链上大额)
  score      = sign × min(base × conviction, 1.0)
  |score| ≥ threshold 且 过冷却期 → 出信号
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from ..util import fmt_px as _fmt_px
from .risk import compute_risk


@dataclass(slots=True)
class Signal:
    coin: str
    direction: str            # 'long' / 'short'
    score: float              # 带符号共振分
    structure_bias: float
    flow_bias: float
    flow_net_usd: float
    oi_change_pct: float
    onchain_usd: float
    reason: str
    ts: int
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    rr: float = 0.0

    def fmt(self) -> str:
        emoji = "🟢做多" if self.direction == "long" else "🔴做空"
        base = f"⚡信号 {emoji} {self.coin} 分={self.score:+.2f} | {self.reason}"
        if self.entry:
            base += (f"\n   入场={_fmt_px(self.entry)} 止损={_fmt_px(self.stop)} "
                     f"目标={_fmt_px(self.target)} 盈亏比={self.rr:.2f}")
        return base


_STRUCT_BIAS = {
    ("BOS", "bull"): 1.0, ("BOS", "bear"): -1.0,
    ("CHoCH", "bull"): 0.7, ("CHoCH", "bear"): -0.7,
}

SignalCallback = Callable[[Signal], Any]


@dataclass(slots=True)
class _State:
    structure_bias: float = 0.0
    structure_ts: int = 0
    flow_net_usd: float = 0.0
    oi_change_pct: float = 0.0
    onchain_usd: float = 0.0
    onchain_ts: int = 0
    zone_confluence: bool = False     # 突破方向存在未回补的同向 OB/FVG
    sweep_confluence: bool = False    # 突破前发生同向流动性扫荡（聪明钱反转确认）
    # 风险参数所需价位
    price: float = 0.0
    swing_low: float = 0.0
    swing_high: float = 0.0
    ob_bottom: float = 0.0
    ob_top: float = 0.0
    last_signal_dir: str = ""
    last_signal_ts: int = 0


class SignalEngine:
    def __init__(
        self,
        store: Any | None = None,
        flow_scale: float = 200_000.0,
        threshold: float = 0.5,
        cooldown_ms: int = 300_000,
        onchain_min_usd: float = 50_000.0,
        onchain_window_ms: int = 600_000,
        target_rr: float = 2.0,
        max_stop_pct: float = 0.08,
        require_sweep: bool = False,     # 回测显示「扫荡确认」是唯一正期望过滤，可设为硬门槛
        on_signal: SignalCallback | None = None,
    ) -> None:
        self.store = store
        self.flow_scale = flow_scale
        self.threshold = threshold
        self.cooldown_ms = cooldown_ms
        self.onchain_min_usd = onchain_min_usd
        self.onchain_window_ms = onchain_window_ms
        self.target_rr = target_rr
        self.max_stop_pct = max_stop_pct
        self.require_sweep = require_sweep
        self.on_signal = on_signal
        self._st: dict[str, _State] = {}
        self.signals_emitted = 0

    def _state(self, coin: str) -> _State:
        s = self._st.get(coin)
        if s is None:
            s = _State()
            self._st[coin] = s
        return s

    # ---- 环境状态更新 ----
    def set_flow(self, coin: str, net_usd: float) -> None:
        self._state(coin).flow_net_usd = net_usd

    def set_oi_change(self, coin: str, pct: float) -> None:
        self._state(coin).oi_change_pct = pct

    def set_onchain(self, coin: str, usd: float, ts: int) -> None:
        s = self._state(coin)
        s.onchain_usd = usd
        s.onchain_ts = ts

    def set_zone(self, coin: str, confluence: bool) -> None:
        self._state(coin).zone_confluence = confluence

    def set_sweep(self, coin: str, confluence: bool) -> None:
        self._state(coin).sweep_confluence = confluence

    def set_levels(self, coin: str, price: float, swing_low: float = 0.0,
                   swing_high: float = 0.0, ob_bottom: float = 0.0,
                   ob_top: float = 0.0) -> None:
        """喂入风险参数所需价位（当前价 + 结构位）。"""
        s = self._state(coin)
        s.price, s.swing_low, s.swing_high = price, swing_low, swing_high
        s.ob_bottom, s.ob_top = ob_bottom, ob_top

    # ---- 触发：结构事件 ----
    def on_structure(self, coin: str, event: Any, now_ms: int) -> Signal | None:
        bias = _STRUCT_BIAS.get((event.type, event.direction), 0.0)
        s = self._state(coin)
        s.structure_bias = bias
        s.structure_ts = now_ms
        return self.evaluate(coin, now_ms)

    def evaluate(self, coin: str, now_ms: int) -> Signal | None:
        s = self._state(coin)
        sb = s.structure_bias
        if sb == 0.0:
            return None
        fb = math.tanh(s.flow_net_usd / self.flow_scale) if self.flow_scale else 0.0
        if fb == 0.0 or (sb > 0) != (fb > 0):     # 异号/无流向 → 无共振
            return None
        if self.require_sweep and not s.sweep_confluence:   # 硬门槛：必须有同向流动性扫荡
            return None

        sign = 1.0 if sb > 0 else -1.0
        base = (abs(sb) + abs(fb)) / 2.0
        oi_boost = min(abs(s.oi_change_pct) / 0.05, 1.0) * 0.3
        onchain_active = (s.onchain_usd >= self.onchain_min_usd
                          and now_ms - s.onchain_ts <= self.onchain_window_ms)
        onchain_boost = 0.2 if onchain_active else 0.0
        zone_boost = 0.15 if s.zone_confluence else 0.0
        sweep_boost = 0.12 if s.sweep_confluence else 0.0
        conviction = 1.0 + oi_boost + onchain_boost + zone_boost + sweep_boost
        score = sign * min(base * conviction, 1.0)

        if abs(score) < self.threshold:
            return None
        direction = "long" if score > 0 else "short"
        # 冷却 + 去重：同向信号冷却期内不重复
        if (s.last_signal_dir == direction
                and now_ms - s.last_signal_ts < self.cooldown_ms):
            return None

        # 风险参数（有价位时计算）：止损过远的劣质 setup 直接拒绝
        risk = None
        if s.price > 0:
            risk = compute_risk(direction, s.price, s.swing_low, s.swing_high,
                                s.ob_bottom, s.ob_top, target_rr=self.target_rr,
                                max_stop_pct=self.max_stop_pct)
            if risk is None:
                return None

        struct_name = "BOS" if abs(sb) == 1.0 else "CHoCH"
        parts = [f"{struct_name}{'↑' if sb > 0 else '↓'}",
                 f"聪明钱净{'+' if fb > 0 else ''}{s.flow_net_usd:,.0f}"]
        if oi_boost:
            parts.append(f"OI{s.oi_change_pct*100:+.1f}%")
        if onchain_active:
            parts.append(f"链上${s.onchain_usd:,.0f}")
        if zone_boost:
            parts.append("OB/FVG共振")
        if sweep_boost:
            parts.append("流动性扫荡")
        sig = Signal(
            coin=coin, direction=direction, score=score,
            structure_bias=sb, flow_bias=fb, flow_net_usd=s.flow_net_usd,
            oi_change_pct=s.oi_change_pct, onchain_usd=s.onchain_usd if onchain_active else 0.0,
            reason=" × ".join(parts), ts=now_ms,
            entry=risk.entry if risk else 0.0, stop=risk.stop if risk else 0.0,
            target=risk.target if risk else 0.0, rr=risk.rr if risk else 0.0,
        )
        s.last_signal_dir = direction
        s.last_signal_ts = now_ms
        self.signals_emitted += 1
        if self.store is not None:
            self.store.insert_signal((
                sig.ts, sig.coin, sig.direction, sig.score, sig.structure_bias,
                sig.flow_bias, sig.flow_net_usd, sig.oi_change_pct, sig.onchain_usd,
                sig.entry, sig.stop, sig.target, sig.rr, sig.reason))
        if self.on_signal is not None:
            self.on_signal(sig)
        return sig
