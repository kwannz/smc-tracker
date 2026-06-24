"""谐波 review 闭环：把 completed setup 转成可落 predictions 表的预测记录（纯函数）。

QA 修复落地（设计 v2 §4）：
- **只记 completed**（kind="谐波-反应式"）；forming **不在投影时记**（H1：投影时价离 PRZ 任意远、
  方向被符号反转，测的是随机漂移而非谐波反转）——forming 留给后续"价格逼近 PRZ"事件再记。
- **结构指纹去重**(coin,tf,pattern,direction,D_idx) via SetupDedup（H3：避免每轮重记的高自相关）。
- 携带 **bg_px**（row["price"]，Bitget 价）修价格覆盖（H_price：不依赖 meme-only coin_to_symbol，
  避免谐波币静默丢失的幸存者偏差）。

诚实：completed = 价格已到 PRZ 并反转（反应式），kind 名"谐波-反应式"如实标注，
review 校准时应优先看长 horizon + market-neutral，不把它当真前瞻 alpha。
"""
from __future__ import annotations

from typing import Any

from .harmonic_dedup import SetupDedup, setup_fingerprint

_KIND_REACTIVE = "谐波-反应式"


def _dir_of(dir_raw: str) -> str | None:
    if dir_raw == "bull":
        return "long"
    if dir_raw == "bear":
        return "short"
    return None


def build_harmonic_predictions(
    rows: list[dict], dedup: SetupDedup, now_ms: int
) -> list[dict[str, Any]]:
    """从 refresh() 的 rows 构建 completed 预测记录列表（去重后）。

    返回每条: {coin, kind, direction, bg_px}，供 app._record_pred 落 predictions 表。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        coin = row.get("coin", "")
        tf = row.get("tf", "")
        bg_px = float(row.get("price", 0.0) or 0.0)
        for hit in row.get("completed") or []:
            direction = _dir_of(str(hit.get("direction", "")))
            if direction is None:
                continue
            pattern = str(hit.get("pattern", ""))
            pts = hit.get("points") or {}
            d_info = pts.get("D")
            d_idx = int(d_info[0]) if d_info and len(d_info) >= 1 else -1
            fp = setup_fingerprint(coin, tf, pattern, direction, d_idx)
            if not dedup.should_record(fp, now_ms):
                continue
            out.append({
                "coin": coin,
                "kind": _KIND_REACTIVE,
                "direction": direction,
                "bg_px": bg_px,
            })
    return out
