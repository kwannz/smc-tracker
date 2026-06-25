"""波动 regime 突破跟踪器：跨刷新检测 (coin,tf) 压缩→扩张 转换（蓄势→放量=突破前瞻信号）。

设计（CLAUDE.md §二 领先信号 + 极简）：压缩(蓄势)切到扩张(放量)常先于价格突破；带 per-(coin,tf)
冷却防刷屏。纯内存状态，无 DB；update 接受 VolatilityMonitor.rank 输出。
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
        lines = [f"🔶 波动突破告警 [{fmt_ts(now_ms)}] 蓄势→放量（领先突破信号）"]
        for e in events:
            lines.append(
                f"  {e['coin']}/{e['tf']} 放量 速度{e['velocity']:+.2f}% (σ比 {e['vol_ratio']:.2f})"
            )
        return "\n".join(lines)
