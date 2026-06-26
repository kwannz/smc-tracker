"""三源背离信号：CEX 散户拥挤方向 ⟂ DEX 聪明钱流向。

第一性原理（聪明钱 vs 散户）：
- CEX 拥挤方向用**资金费**代理：funding>0 = 多头付空头 = 多头拥挤；funding<0 = 空头拥挤。
  OI 上升放大（新仓在涌入，拥挤加剧）。
- DEX 聪明钱方向用 Hyperliquid 主动净流向（MemeTradeMonitor.coin_net）。
- **背离**（二者相悖）才是信号：
  · 多头拥挤(funding>0) 且 聪明钱净卖 → **看跌**（聪明钱向拥挤多头分销，常见顶部）。
  · 空头拥挤(funding<0) 且 聪明钱净买 → **看涨**（聪明钱从恐慌空头吸筹，逼空前兆）。

**实测(#170→#193 降级 UNVERIFIED)**:#170 初测逼空侧"+0.83pp(sm∩oi 14币·前向8h)"似方向类唯一幸存 edge,
  但 **#193 系统复核**:该测量与 #186/#187 **同类**(小 coin 样本前瞻收益,n=17 极小),而该类已两次被币内配对证伪翻转
  (#186 入场领先 +0.46%↔−0.53%、#187 共识 +7.1%↔−6%)。n=17 小到无法验证,故**降级:逼空 edge 未确立、勿当 edge 加码**;
  方向类至此**无确立 edge**(与"方向≈鞅不可测"一致)。逼空/分销拆 kind(#176)保留——仅为生产持续审判,**以 efficacy 实盘命中率为准**,
  两侧均当弱上下文。诚实用法:不据此重仓单押,看实盘 efficacy。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable


def pred_kind(direction: str) -> str:
    """背离预测落 predictions 表 / efficacy 学习的 kind——按方向**拆分**(单一真相源,
    流式+轮询两路径共用)。

    #176:旧实现两侧混记→'背离',使 accuracy_report/efficacy 的 by_kind 把逼空(bullish)与分销(bearish)
    聚合进同一命中率,无法独立审判。拆 kind 后生产分别审判,以 **efficacy 实盘命中率为准**。
    (#170 逼空 "+0.83pp" 已 #193 降级 unverified——小 coin 样本前瞻收益同 #186/#187、n=17 极小;
    拆 kind 仍有价值:让实盘独立度量两侧,不被混记掩盖,而非预设逼空有 edge。)
    """
    return "逼空背离" if direction == "bullish" else "分销背离"


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
