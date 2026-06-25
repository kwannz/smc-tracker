"""实时波动追踪（专业细节）：监控清单币 → 已采集多周期 K 线 → 波动/速度/加速度排序。

设计（CLAUDE.md：领先信号 + 低延迟 + 模块化扁平 + 极简）：
  - vol_metrics：纯 numpy 向量化函数，从单周期 OHLC 算专业波动指标，确定性可测。
  - VolatilityMonitor：读 store.get_candles（复用已采集 K 线，不重拉），按"运动分"排序出当前在动的币。
  - 领先信号优先：velocity(1 阶导)+accel(2 阶导) 先于价格，是项目珍视的前瞻量（CLAUDE.md §二）。
"""
from __future__ import annotations

from typing import Any

import numpy as np

# 指标窗口（根）：rv/atr 用近 _RV_WIN 根，速度用近 _VEL_WIN 根
_RV_WIN = 20
_VEL_WIN = 5
# 运动分权重：加速度领先量加权最高，其次速度，波动率辅助
_W_VEL, _W_ACCEL, _W_RV = 1.0, 1.5, 0.5


def vol_metrics(o: Any, h: Any, l: Any, c: Any, *,
                rv_win: int = _RV_WIN, vel_win: int = _VEL_WIN) -> dict:
    """从单周期 OHLC 算波动专业指标（numpy 向量化）。数据 <3 根返回 {}。

    返回：rv(已实现波动率=对数收益σ,%)、atr_pct(真实波幅均值/价,%)、
         range_pct(当前 bar 区间,%)、velocity(近窗%变化=1 阶导)、accel(速度差=2 阶导)。
    """
    c = np.asarray(c, dtype=float)
    n = c.size
    if n < 3:
        return {}
    cc = np.clip(c, 1e-12, None)                      # 防 log(0)/除 0
    logret = np.diff(np.log(cc))
    rv = float(np.std(logret[-rv_win:], ddof=0)) * 100.0
    hi, lo = np.asarray(h, float), np.asarray(l, float)
    prev = cc[:-1]
    tr = np.maximum.reduce([hi[1:] - lo[1:], np.abs(hi[1:] - prev), np.abs(lo[1:] - prev)])
    last = cc[-1]
    atr_pct = float(np.mean(tr[-rv_win:]) / last) * 100.0
    range_pct = float((hi[-1] - lo[-1]) / last) * 100.0
    k = min(vel_win, n - 1)
    velocity = float((cc[-1] - cc[-1 - k]) / cc[-1 - k]) * 100.0
    vel_prev = (float((cc[-1 - k] - cc[-1 - 2 * k]) / cc[-1 - 2 * k]) * 100.0
                if n >= 2 * k + 1 else 0.0)
    accel = velocity - vel_prev
    return {"rv": rv, "atr_pct": atr_pct, "range_pct": range_pct,
            "velocity": velocity, "accel": accel}


def move_score(m: dict) -> float:
    """运动分：|速度|·_W_VEL + |加速度|·_W_ACCEL + 波动率·_W_RV（加速度领先量加权最高）。"""
    return (_W_VEL * abs(m.get("velocity", 0.0))
            + _W_ACCEL * abs(m.get("accel", 0.0))
            + _W_RV * m.get("rv", 0.0))


class VolatilityMonitor:
    """读已采集多周期 K 线，按运动分排序出当前在动的监控清单币。"""

    __slots__ = ("coin_to_symbol", "timeframes", "store", "bars", "primary_tf")

    def __init__(self, coin_to_symbol: dict[str, str], timeframes: list[str],
                 store: Any, bars: int = 120) -> None:
        self.coin_to_symbol = coin_to_symbol
        self.timeframes = list(timeframes)
        self.store = store
        self.bars = bars
        self.primary_tf = self.timeframes[0] if self.timeframes else "15m"

    def _metrics(self, coin: str, tf: str) -> dict | None:
        """读 store 单 coin/tf K 线 → vol_metrics；不足或异常返回 None。"""
        try:
            cs = self.store.get_candles(coin, tf, self.bars) if self.store else []
        except Exception:  # noqa: BLE001 — 单币失败不影响整体
            return None
        if len(cs) < 3:
            return None
        m = vol_metrics([x.o for x in cs], [x.h for x in cs],
                        [x.l for x in cs], [x.c for x in cs])
        return m or None

    def rank(self, now_ms: int) -> list[dict]:
        """对每个监控币算 primary_tf 运动指标 + 运动分，按分降序返回行。"""
        rows: list[dict] = []
        for coin in self.coin_to_symbol:
            m = self._metrics(coin, self.primary_tf)
            if not m:
                continue
            rows.append({"coin": coin, "tf": self.primary_tf,
                         "score": move_score(m), **m})
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    def render(self, rows: list[dict], now_ms: int, top: int = 15) -> str:
        """渲染波动监控板卡片（专业细节：速度/加速度/σ/ATR%）。空行返回 ""。"""
        if not rows:
            return ""
        lines = [f"🌀 实时波动追踪板 [{self.primary_tf}] · 速度+加速度领先信号（Top {top}）"]
        for r in rows[:top]:
            v, a = r["velocity"], r["accel"]
            vdir = "🟢↑" if v >= 0 else "🔴↓"
            adir = "加速" if a * v > 0 else ("减速" if a * v < 0 else "—")
            lines.append(
                f"{r['coin']:<10} {vdir}{abs(v):.2f}%  a{a:+.2f}({adir})"
                f"  σ{r['rv']:.2f}%  ATR{r['atr_pct']:.2f}%"
            )
        return "\n".join(lines)
