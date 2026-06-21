"""热路径延迟埋点：低开销环形缓冲 + P50/P99/max 统计（实证「低延迟」）。

第一性原理：低延迟不能靠声称，要测。WS 接收即打单调戳 recv_ns(monotonic_ns)，
处理完成时再打一次，差值即「接收→信号」端到端延迟。本模块以预分配 numpy 环形缓冲
记录各阶段样本(record 为 O(1)，不阻塞事件循环)，周期报告 P50/P99/max。
"""
from __future__ import annotations

import numpy as np


class LatencyTracker:
    """多阶段延迟采样器：每阶段一个固定容量环形缓冲，满则覆盖最旧。"""

    __slots__ = ("capacity", "_buf", "_idx", "_cnt")

    def __init__(self, capacity: int = 2048) -> None:
        self.capacity = max(8, int(capacity))
        self._buf: dict[str, np.ndarray] = {}
        self._idx: dict[str, int] = {}     # 下一写入位置
        self._cnt: dict[str, int] = {}     # 已填样本数(≤capacity)

    def record(self, stage: str, ms: float) -> None:
        """记录一条延迟样本(毫秒)。O(1)，热路径安全。非有限值忽略。"""
        if ms != ms or ms == float("inf"):     # NaN/inf 守卫(数据质量)
            return
        b = self._buf.get(stage)
        if b is None:
            b = np.empty(self.capacity, dtype=float)
            self._buf[stage] = b
            self._idx[stage] = 0
            self._cnt[stage] = 0
        i = self._idx[stage]
        b[i] = ms
        self._idx[stage] = (i + 1) % self.capacity
        if self._cnt[stage] < self.capacity:
            self._cnt[stage] += 1

    def stats(self, stage: str) -> dict[str, float] | None:
        """返回该阶段 {n,p50,p99,max,mean}；无样本返回 None。"""
        c = self._cnt.get(stage, 0)
        if not c:
            return None
        s = self._buf[stage][:c]
        return {
            "n": float(c),
            "p50": float(np.percentile(s, 50)),
            "p99": float(np.percentile(s, 99)),
            "max": float(s.max()),
            "mean": float(s.mean()),
        }

    def fmt(self) -> str:
        """多阶段一行式摘要(供周期报告)。无样本返回空串。"""
        parts: list[str] = []
        for stage in self._buf:
            st = self.stats(stage)
            if st:
                parts.append(f"  {stage}: P50={st['p50']:.2f}ms "
                             f"P99={st['p99']:.2f}ms max={st['max']:.2f}ms (n={int(st['n'])})")
        return "\n".join(parts)
