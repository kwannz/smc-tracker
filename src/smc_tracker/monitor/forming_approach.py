"""forming PRZ 实时逼近检测器 —— 把"价格正在逼近已投影 forming PRZ"变成秒级前瞻事件。

QA H6/H7 修复（设计 v2 §5）：
- **纯内存判定**：check(coin, price) 只判断价格是否进入缓存的 forming PRZ 带，返回事件列表，
  **绝不写库**。热路径（WS tick 回调）只调 check + 入队；周期 worker 出队再落 review，
  把同步 SQL 移出 WS 回调（修 H6 热路径阻塞）。
- **per-entry TTL**：陈旧 PRZ（超 ttl_ms）不触发（修 H7 陈旧假告警）。
- **冷却**：同一 PRZ（结构指纹）cooldown_ms 内不重复告警（修 H7 宽带刷屏）。
- **穿越作废**：价格越过 PRZ 远侧（形态失效）不再告警。

价格源诚实：调用方决定（Bitget tick / HL allMids），对不在价源里的币不触发——见调用方接线。
"""
from __future__ import annotations

from typing import Any


class FormingApproachTracker:
    """forming PRZ 缓存 + 逼近判定（asyncio 单线程，无锁）。"""

    __slots__ = ("ttl_ms", "cooldown_ms", "band_pct", "invalidate_pct", "_prz", "_cooldown")

    def __init__(
        self, ttl_ms: int = 1_800_000, cooldown_ms: int = 1_800_000,
        band_pct: float = 0.0, invalidate_pct: float = 0.0,
    ) -> None:
        self.ttl_ms = ttl_ms
        self.cooldown_ms = cooldown_ms
        self.band_pct = band_pct          # 进入带的额外容差比例（0=精确 [lo,hi]）
        self.invalidate_pct = invalidate_pct  # 越过远侧此比例=穿越作废（0=不作废）
        # coin -> [(lo, hi, direction, tf, pattern, d_idx, ts)]
        self._prz: dict[str, list[tuple]] = {}
        # 结构指纹 -> 上次告警 ts（冷却）
        self._cooldown: dict[str, int] = {}

    def update(self, rows: list[dict], now_ms: int) -> None:
        """用一轮 refresh 的 rows 重建 forming PRZ 缓存（整体覆盖，每条带 ts 供 TTL）。"""
        new: dict[str, list[tuple]] = {}
        for row in rows:
            coin = row.get("coin", "")
            tf = row.get("tf", "")
            for hit in row.get("forming") or []:
                prz = hit.get("prz")
                if not prz or len(prz) < 2 or prz[0] is None or prz[1] is None:
                    continue
                lo = float(min(prz[0], prz[1]))
                hi = float(max(prz[0], prz[1]))
                dir_raw = hit.get("direction", "")
                direction = "long" if dir_raw == "bull" else (
                    "short" if dir_raw == "bear" else dir_raw)
                if direction not in ("long", "short"):
                    continue
                pts = hit.get("points") or {}
                d = pts.get("D")
                d_idx = int(d[0]) if d and len(d) >= 1 else -1
                new.setdefault(coin, []).append(
                    (lo, hi, direction, tf, str(hit.get("pattern", "")), d_idx, now_ms))
        self._prz = new
        # 淘汰过期冷却键（防长跑无限增长，m1 修复）
        if len(self._cooldown) > 2048:
            self._cooldown = {
                k: t for k, t in self._cooldown.items() if (now_ms - t) < self.cooldown_ms
            }

    def check(self, coin: str, price: float, now_ms: int) -> list[dict[str, Any]]:
        """判断 price 是否逼近 coin 的某 forming PRZ；返回逼近事件列表（含冷却/TTL/作废）。"""
        out: list[dict[str, Any]] = []
        for (lo, hi, direction, tf, pattern, d_idx, ts) in self._prz.get(coin, ()):
            if now_ms - ts > self.ttl_ms:
                continue  # TTL：陈旧 PRZ 不触发
            # 穿越作废：越过远侧（形态失效）
            if self.invalidate_pct > 0:
                if direction == "long" and price < lo * (1 - self.invalidate_pct):
                    continue
                if direction == "short" and price > hi * (1 + self.invalidate_pct):
                    continue
            # 逼近：进入 PRZ 带（含容差）
            band_lo = lo * (1 - self.band_pct)
            band_hi = hi * (1 + self.band_pct)
            if not (band_lo <= price <= band_hi):
                continue
            fp = f"{coin}|{tf}|{pattern}|{direction}|{d_idx}"
            last = self._cooldown.get(fp)
            if last is not None and (now_ms - last) < self.cooldown_ms:
                continue  # 冷却中
            self._cooldown[fp] = now_ms
            out.append({
                "coin": coin, "tf": tf, "pattern": pattern, "direction": direction,
                "prz_lo": lo, "prz_hi": hi, "price": price,
            })
        return out
