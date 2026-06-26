"""三源背离信号：CEX 散户拥挤方向 ⟂ DEX 聪明钱流向。

第一性原理（聪明钱 vs 散户）：
- CEX 拥挤方向用**资金费**代理：funding>0 = 多头付空头 = 多头拥挤；funding<0 = 空头拥挤。
  OI 上升放大（新仓在涌入，拥挤加剧）。
- DEX 聪明钱方向用 Hyperliquid 主动净流向（MemeTradeMonitor.coin_net）。
- **背离**（二者相悖）才是信号：
  · 多头拥挤(funding>0) 且 聪明钱净卖 → **看跌**（聪明钱向拥挤多头分销，常见顶部）。
  · 空头拥挤(funding<0) 且 聪明钱净买 → **看涨**（聪明钱从恐慌空头吸筹，逼空前兆）。

**实测(#170,sm∩oi 14币样本外·前向8h)——不对称,看涨侧才是真信号：**
  · 背离**看涨(逼空)**：+0.72% vs 基线−0.11% = **超基线+0.83pp**(方向类信号里唯一有 edge 迹象,
    与"逼空比分销更暴烈可测"的市场结构一致)——**但 n=17 小样本,暂为强迹象非定论**。
  · 背离**看跌(分销)**：−0.16%,仅低基线 −0.06pp，**弱/接近无**——分销侧不如逼空侧可靠。
  对照:裸净流向/OI velocity/加速度方向力皆~0(#167-168)；背离的逼空侧是它们中唯一幸存者。
  诚实用法：**优先信看涨(逼空)背离,看跌(分销)背离当弱提示**;小样本,勿重仓单押。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class DivergenceSignal:
    coin: str
    direction: str          # 'bullish' / 'bearish'
    score: float
    funding: float
    oi_change_pct: float
    dex_flow_usd: float
    reason: str
    ts: int

    def fmt(self) -> str:
        emoji = "🟢吸筹背离" if self.direction == "bullish" else "🔴分销背离"
        return f"🔀背离 {emoji} {self.coin} 分={self.score:.2f} | {self.reason}"


DivCallback = Callable[[DivergenceSignal], Any]


class DivergenceDetector:
    def __init__(
        self,
        store: Any | None = None,
        min_funding: float = 0.00003,
        min_flow_usd: float = 30_000.0,
        funding_scale: float = 0.0003,
        flow_scale: float = 200_000.0,
        threshold: float = 0.15,
        on_signal: DivCallback | None = None,
    ) -> None:
        self.store = store
        self.min_funding = min_funding
        self.min_flow_usd = min_flow_usd
        self.funding_scale = funding_scale
        self.flow_scale = flow_scale
        self.threshold = threshold
        self.on_signal = on_signal
        self.signals_emitted = 0

    def evaluate(self, coin: str, funding: float, oi_change_pct: float,
                 dex_flow_usd: float, now_ms: int) -> DivergenceSignal | None:
        long_crowd = funding >= self.min_funding
        short_crowd = funding <= -self.min_funding
        smart_buy = dex_flow_usd >= self.min_flow_usd
        smart_sell = dex_flow_usd <= -self.min_flow_usd

        if long_crowd and smart_sell:
            direction = "bearish"
        elif short_crowd and smart_buy:
            direction = "bullish"
        else:
            return None

        funding_str = min(abs(funding) / self.funding_scale, 1.0)
        flow_str = math.tanh(abs(dex_flow_usd) / self.flow_scale)
        # OI 因子双向：
        #   增仓(oi>0)  → 拥挤加剧，放大背离强度，上限 1.5×
        #   中性(oi=0)  → 无影响，1.0
        #   减仓(oi<0)  → 去杠杆/拥挤瓦解，衰减背离强度，下限 0.7×
        # 语义：OI 上升说明新仓在涌入、拥挤加剧；OI 下降说明平仓离场、背离信号应打折
        if oi_change_pct > 0:
            oi_amp = 1.0 + min(oi_change_pct / 0.05, 1.0) * 0.5   # [1.0, 1.5]
        elif oi_change_pct < 0:
            oi_amp = 1.0 + max(oi_change_pct / 0.05, -1.0) * 0.3  # [0.7, 1.0]
        else:
            oi_amp = 1.0
        score = min(funding_str * flow_str * oi_amp, 1.0)
        if score < self.threshold:
            return None

        crowd = "多头拥挤" if direction == "bearish" else "空头拥挤"
        smart = "聪明钱净卖" if direction == "bearish" else "聪明钱净买"
        parts = [f"{crowd}(funding{funding*100:+.3f}%)", f"{smart}${dex_flow_usd:,.0f}"]
        if oi_change_pct > 0:
            parts.append(f"OI+{oi_change_pct*100:.1f}%")
        elif oi_change_pct < 0:
            parts.append(f"OI{oi_change_pct*100:.1f}%(去杠杆)")
        sig = DivergenceSignal(coin=coin, direction=direction, score=score,
                               funding=funding, oi_change_pct=oi_change_pct,
                               dex_flow_usd=dex_flow_usd, reason=" × ".join(parts), ts=now_ms)
        self.signals_emitted += 1
        if self.store is not None:
            self.store.insert_divergence((
                sig.ts, sig.coin, sig.direction, sig.score, sig.funding,
                sig.oi_change_pct, sig.dex_flow_usd, sig.reason))
        if self.on_signal is not None:
            self.on_signal(sig)
        return sig
