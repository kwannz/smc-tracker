"""实时波动追踪（专业细节·逐周期）：监控清单币 → 已采集多周期 K 线 → 每周期独立指标。

设计（CLAUDE.md：领先信号 + 低延迟 + 模块化扁平 + 极简；用户#：不做共振，每周期各显指标 + PDArray）：
  - vol_metrics：纯 numpy 向量化，单周期 OHLC → rv/atr/range/velocity(1阶导)/accel(2阶导)。
  - pdarray：ICT 溢价/折价数组（Premium/Discount Array）——价在 dealing range 的位置（开源 ICT 标准）。
  - VolatilityMonitor：读 store.get_candles（复用已采 K 线，不重拉），**逐周期**算指标并展示，按运动分排序。
  - 可比性诚实标注：velocity/accel/rv 用固定 _VEL_WIN/_RV_WIN 根，**同周期内跨币可比**；但 5 根在 15m 与
    1W 时间跨度差 1~2 个数量级，**跨周期幅度不可直接比较**（rv∝√t、accel 随 bar 时长增大）。
    pd_pct 是区间占比 [0,1]，跨周期可比。score=max(各周期) 偏向最长周期，仅作"是否在动"的粗排，非精确强度。
"""
from __future__ import annotations

from typing import Any

import numpy as np

# 指标窗口（根）：rv/atr 用近 _RV_WIN 根，速度用近 _VEL_WIN 根，PD dealing range 用近 _PD_WIN 根
_RV_WIN = 20
_VEL_WIN = 5
_PD_WIN = 60
# 运动分权重：加速度领先量加权最高，其次速度，波动率辅助
_W_VEL, _W_ACCEL, _W_RV = 1.0, 1.5, 0.5


def vol_metrics(h: Any, l: Any, c: Any, *,
                rv_win: int = _RV_WIN, vel_win: int = _VEL_WIN) -> dict:
    """单周期 HLC → 波动专业指标（numpy 向量化）。数据 <3 根返回 {}。（open 不参与，故不收）

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


def pdarray(h: Any, l: Any, c: Any, *, win: int = _PD_WIN, band: float = 0.03) -> dict:
    """ICT 溢价/折价数组（PD Array）：当前价在 dealing range（近 win 根高低）的位置。

    返回：pd_pct∈[0,1]（0=折价极值，0.5=均衡 EQ，1=溢价极值）、pd_zone(溢价/折价/均衡，EQ±band)。
    区间为 0 时归为均衡。
    """
    hh = np.asarray(h, float)[-win:]
    ll = np.asarray(l, float)[-win:]
    price = float(np.asarray(c, float)[-1])
    hi, lo = float(np.max(hh)), float(np.min(ll))
    rng = hi - lo
    if rng <= 0:
        return {"pd_pct": 0.5, "pd_zone": "均衡"}
    pd = (price - lo) / rng
    zone = "溢价" if pd > 0.5 + band else ("折价" if pd < 0.5 - band else "均衡")
    return {"pd_pct": pd, "pd_zone": zone}


def move_score(m: dict) -> float:
    """运动分：|速度|·_W_VEL + |加速度|·_W_ACCEL + 波动率·_W_RV（加速度领先量加权最高）。"""
    return (_W_VEL * abs(m.get("velocity", 0.0))
            + _W_ACCEL * abs(m.get("accel", 0.0))
            + _W_RV * m.get("rv", 0.0))


class VolatilityMonitor:
    """逐周期读已采 K 线算波动+PD 指标，按运动分排序出当前在动的监控清单币。"""

    __slots__ = ("coin_to_symbol", "timeframes", "store", "bars")

    def __init__(self, coin_to_symbol: dict[str, str], timeframes: list[str],
                 store: Any, bars: int = 120) -> None:
        self.coin_to_symbol = coin_to_symbol
        self.timeframes = list(timeframes) or ["15m"]
        self.store = store
        self.bars = bars

    def _tf_metrics(self, coin: str, tf: str) -> dict | None:
        """单 coin/tf：vol_metrics + pdarray 合并；不足或异常返回 None。"""
        try:
            cs = self.store.get_candles(coin, tf, self.bars) if self.store else []
        except Exception:  # noqa: BLE001 — 单组合失败不影响整体
            return None
        if len(cs) < 3:
            return None
        h = [x.h for x in cs]; l = [x.l for x in cs]; c = [x.c for x in cs]
        m = vol_metrics(h, l, c)
        if not m:
            return None
        m.update(pdarray(h, l, c))
        return m

    def rank(self, now_ms: int = 0) -> list[dict]:
        """每币逐周期算指标 → {coin, score(各周期运动分取最大), by_tf}，按 score 降序。

        now_ms 预留（与兄弟监控板 bb/harmonic 统一签名；当前排序不依赖时间）。
        """
        rows: list[dict] = []
        for coin in self.coin_to_symbol:
            by_tf = {tf: m for tf in self.timeframes
                     if (m := self._tf_metrics(coin, tf)) is not None}
            if not by_tf:
                continue
            rows.append({"coin": coin,
                         "score": max(move_score(m) for m in by_tf.values()),
                         "by_tf": by_tf})
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    def render(self, rows: list[dict], now_ms: int = 0, top: int = 8) -> str:
        """逐周期渲染波动追踪板（每周期一行：速度/加速度/σ/ATR/区间/PD 溢价折价）。空返回 ""。"""
        if not rows:
            return ""
        from ..util import fmt_ts  # noqa: PLC0415
        ts = fmt_ts(now_ms) if now_ms else ""
        lines = [f"🌀 实时波动追踪板 [{ts}] · 每周期指标(速度+加速度+区间+PD溢价折价) Top {top}"]
        for r in rows[:top]:
            lines.append(f"━ {r['coin']:<8} 运动分 {r['score']:.1f}")
            for tf in self.timeframes:
                m = r["by_tf"].get(tf)
                if not m:
                    continue
                v, a = m["velocity"], m["accel"]
                vdir = "🟢↑" if v >= 0 else "🔴↓"
                adir = "加速" if a * v > 0 else ("减速" if a * v < 0 else "—")
                lines.append(
                    f"  {tf:<4} {vdir}{abs(v):.2f}% a{a:+.2f}{adir}"
                    f" σ{m['rv']:.2f}% ATR{m['atr_pct']:.2f}% 幅{m['range_pct']:.2f}%"
                    f" PD{m['pd_pct'] * 100:.0f}%{m['pd_zone']}"
                )
        return "\n".join(lines)
