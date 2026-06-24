"""谐波 setup 结构指纹去重 —— 保证同一 setup 只进 review 闭环一次（QA H3-dedup 修复）。

QA H3-dedup：用 round(prz_mid,4) 做去重 key 会因 PRZ 中心浮点微抖每轮变化→去重退化成
每 15min 记一次→高自相关样本虚增 accuracy_report 的 n、高估置信区间。

改用**结构指纹** (coin, tf, pattern, direction, D_pivot_idx)：D pivot 是稳定的离散 K 线下标，
setup 生命周期内不变，保证同一 setup 在 TTL 内只记一次。TTL 后（setup 重新出现）可重记。
"""
from __future__ import annotations


def setup_fingerprint(
    coin: str, tf: str, pattern: str, direction: str, d_idx: int
) -> str:
    """结构指纹（不含浮点，稳定）。"""
    return f"{coin}|{tf}|{pattern}|{direction}|{d_idx}"


class SetupDedup:
    """基于结构指纹 + TTL 的去重器（asyncio 单线程，无锁）。"""

    __slots__ = ("ttl_ms", "_seen")

    def __init__(self, ttl_ms: int = 3_600_000) -> None:
        self.ttl_ms = ttl_ms
        self._seen: dict[str, int] = {}   # fingerprint -> 上次记录 ts

    def should_record(self, fingerprint: str, now_ms: int) -> bool:
        """该指纹此刻是否应记录（TTL 内已记过→False）。允许时更新时戳。"""
        last = self._seen.get(fingerprint)
        if last is not None and (now_ms - last) < self.ttl_ms:
            return False
        self._seen[fingerprint] = now_ms
        # 淘汰过期键（防长跑进程无限增长，m1 修复）：仅字典较大时整理，摊销 O(1)
        if len(self._seen) > 2048:
            self._seen = {k: t for k, t in self._seen.items() if (now_ms - t) < self.ttl_ms}
        return True
