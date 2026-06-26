"""波动 regime 跟踪器：跨刷新检测 (coin,tf) 压缩→扩张 转换（波动收敛→放大 的同步确认）。

设计（极简）：压缩(波动收敛)切到扩张(波动放大)是波动状态已变化的*确认*（非前瞻预测，诚实标注，
CLAUDE.md §二：真领先信号见订单簿/OI 速度）；带 per-(coin,tf) 冷却防刷屏。纯内存状态，无 DB；
update 接受 VolatilityMonitor.rank 输出。
"""
from __future__ import annotations


class VolatilityRegimeTracker:
    """记忆每 (coin,tf) 上一次 regime，检测 压缩/常态 → 扩张 的新突破事件。"""

    __slots__ = ("_prev", "_last_emit_ms", "cooldown_ms")

    def __init__(self, cooldown_ms: int = 1_800_000) -> None:
        self._prev: dict[tuple[str, str], str] = {}         # (coin,tf) -> 上次 regime
        self._last_emit_ms: dict[tuple[str, str], int] = {}  # (coin,tf) -> 上次告警 ts
        self.cooldown_ms = cooldown_ms

    def update(self, rows: list[dict], now_ms: int) -> list[dict]:
        """rows=rank() 输出。返回新突破事件 [{coin,tf,vol_ratio,velocity}]（仅 压缩/常态→扩张 且过冷却）。

        首次见到某 (coin,tf) 不论 regime 都不报（无法判断"转换"）；
        prev 已是扩张 → 本次扩张不报（非新转换）。
        """
        events: list[dict] = []
        for r in rows:
            coin: str = r.get("coin", "")
            for tf, m in r.get("by_tf", {}).items():
                key = (coin, tf)
                cur: str = m.get("regime", "常态")
                prev = self._prev.get(key)  # None = 首次见

                # 突破条件：prev 不是首见(None)、prev 不是扩张、当前是扩张
                if cur == "扩张" and prev is not None and prev != "扩张":
                    last = self._last_emit_ms.get(key, 0)
                    if now_ms - last >= self.cooldown_ms:
                        events.append({
                            "coin": coin,
                            "tf": tf,
                            "vol_ratio": m.get("vol_ratio", 0.0),
                            "velocity": m.get("velocity", 0.0),
                        })
                        self._last_emit_ms[key] = now_ms

                self._prev[key] = cur

        return events

    def render(self, events: list[dict], now_ms: int) -> str:
        """渲染突破告警卡片。空事件列表返回 ""。"""
        if not events:
            return ""
        from ..util import fmt_ts  # noqa: PLC0415
        # 诚实标注：扩张是波动已放大的*当前态确认*(非方向预测)。#177 null 对照纠 #153"90%续/0.73"窗口伪影;
        # #178 立:波动**水平**可前瞻(EWMA→未来h-bar平均波动 corr 0.30@1bar→0.45@10bar=真实幅度edge),
        # 但逐bar|收益|记忆快衰减(0.28→0.05)、方向仍不定(方向类皆~0)。与 vol 板同口径。
        lines = [f"🔶 波动扩张确认 [{fmt_ts(now_ms)}] 压缩→放量（已放大·波动水平可前瞻corr~0.4·非90%续·方向不定#178）"]
        for e in events:
            lines.append(
                f"  {e['coin']}/{e['tf']} 放量 速度{e['velocity']:+.2f}% (σ比 {e['vol_ratio']:.2f})"
            )
        return "\n".join(lines)
