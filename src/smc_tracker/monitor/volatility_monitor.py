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
_RV_LONG = 60   # 波动 regime 长窗基线（短窗 σ / 长窗 σ → 压缩/扩张）
_VEL_WIN = 5
_PD_WIN = 60
# 波动 regime 阈值：短/长 σ 比值 < 压缩阈=蓄势(领先突破)，> 扩张阈=放量
_SQUEEZE, _EXPAND = 0.7, 1.4
# 运动分权重：加速度领先量加权最高，其次速度，波动率辅助
_W_VEL, _W_ACCEL, _W_RV = 1.0, 1.5, 0.5


def vol_metrics(h: Any, l: Any, c: Any, *,
                rv_win: int = _RV_WIN, vel_win: int = _VEL_WIN,
                rv_long_win: int = _RV_LONG) -> dict:
    """单周期 HLC → 波动专业指标（numpy 向量化）。数据 <3 根返回 {}。（open 不参与，故不收）

    返回：rv(已实现波动率=对数收益σ,%)、atr_pct(真实波幅均值/价,%)、range_pct(当前 bar 区间,%)、
         velocity(近窗%变化=1 阶导)、accel(速度差=2 阶导)、
         vol_ratio(短窗σ/长窗σ)、regime(压缩/扩张/常态=波动状态领先信号)。
    """
    c = np.asarray(c, dtype=float)
    n = c.size
    if n < 3:
        return {}
    cc = np.clip(c, 1e-12, None)                      # 防 log(0)/除 0
    logret = np.diff(np.log(cc))
    rv = float(np.std(logret[-rv_win:], ddof=0)) * 100.0
    rv_long = float(np.std(logret[-rv_long_win:], ddof=0)) * 100.0 if logret.size else 0.0
    vol_ratio = rv / rv_long if rv_long > 1e-9 else 1.0
    regime = "压缩" if vol_ratio < _SQUEEZE else ("扩张" if vol_ratio > _EXPAND else "常态")
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
            "velocity": velocity, "accel": accel,
            "vol_ratio": vol_ratio, "regime": regime}


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


def volatility_highlights(rows: list[dict], *, max_each: int = 5) -> dict:
    """把逐周期矩阵综合成动向摘要（可操作情报）。纯函数。

    - squeeze：处于压缩(蓄势，常先于突破)的 (coin,tf)，按 vol_ratio 升序（最压缩在前）。
    - expansion：处于扩张(放量启动)的 (coin,tf)，按 |velocity| 降序（最大动量在前）。
    - extreme_pd：PD≤10%(深折价)或≥90%(深溢价)的 (coin,tf)，按偏离 EQ 程度降序。
    """
    sq: list[dict] = []
    ex: list[dict] = []
    epd: list[dict] = []
    for r in rows:
        for tf, m in r.get("by_tf", {}).items():
            rg = m.get("regime")
            if rg == "压缩":
                sq.append({"coin": r["coin"], "tf": tf, "vol_ratio": m.get("vol_ratio", 1.0)})
            elif rg == "扩张":
                ex.append({"coin": r["coin"], "tf": tf, "velocity": m.get("velocity", 0.0)})
            p = m.get("pd_pct", 0.5)
            if p <= 0.1 or p >= 0.9:
                epd.append({"coin": r["coin"], "tf": tf, "pd_pct": p,
                            "pd_zone": m.get("pd_zone", "")})
    sq.sort(key=lambda x: x["vol_ratio"])
    ex.sort(key=lambda x: abs(x["velocity"]), reverse=True)
    epd.sort(key=lambda x: abs(x["pd_pct"] - 0.5), reverse=True)
    return {"squeeze": sq[:max_each], "expansion": ex[:max_each], "extreme_pd": epd[:max_each]}


def market_regime(rows: list[dict]) -> dict:
    """聚合全监控集逐周期 regime/PD → 市场级波动态势（市场广度/regime）。纯函数。

    返回 {n, regime:{压缩,扩张,常态}, pd:{折价,溢价,均衡}, label}。
    label：主导 regime + 主导 PD（如"蓄势(压缩) 12/21 · 普遍折价(偏超卖) 15/21"）；n=0 时 label=""。
    """
    rc = {"压缩": 0, "扩张": 0, "常态": 0}
    pc = {"折价": 0, "溢价": 0, "均衡": 0}
    n = 0
    for r in rows:
        for _tf, m in r.get("by_tf", {}).items():
            n += 1
            rc[m.get("regime", "常态")] = rc.get(m.get("regime", "常态"), 0) + 1
            pc[m.get("pd_zone", "均衡")] = pc.get(m.get("pd_zone", "均衡"), 0) + 1
    if n == 0:
        return {"n": 0, "regime": rc, "pd": pc, "label": ""}
    reg_dom = max(rc, key=lambda k: rc[k])
    pd_dom = max(pc, key=lambda k: pc[k])
    reg_lbl = {"压缩": "蓄势(压缩)", "扩张": "放量(扩张)", "常态": "常态"}[reg_dom]
    pd_lbl = {"折价": "普遍折价(偏超卖)", "溢价": "普遍溢价(偏超买)", "均衡": "均衡"}[pd_dom]
    label = f"{reg_lbl} {rc[reg_dom]}/{n} · {pd_lbl} {pc[pd_dom]}/{n}"
    return {"n": n, "regime": rc, "pd": pc, "label": label}


# 多周期一致性阈值：主导方向占比 ≥ 此值才判明确多/空，否则分歧
_ALIGN_TH = 0.7


def mtf_alignment(by_tf: dict) -> dict:
    """单币跨周期速度一致性（MTF trend alignment）：各周期 velocity 同向=高确信，冲突=分歧。纯函数。

    返回 {bias:多/空/分歧, aligned:主导方向周期数, total:非零周期数, score:主导占比[0,1]}。
    score 越接近 1 越一致（开源 MTF 趋势对齐思路：多周期共振方向 > 单周期噪声）。
    """
    up = down = 0
    for m in by_tf.values():
        v = m.get("velocity", 0.0)
        if v > 0:
            up += 1
        elif v < 0:
            down += 1
    total = up + down
    if total == 0:
        return {"bias": "分歧", "aligned": 0, "total": 0, "score": 0.0}
    dominant = max(up, down)
    score = dominant / total
    if score >= _ALIGN_TH:
        bias = "多" if up >= down else "空"
    else:
        bias = "分歧"
    return {"bias": bias, "aligned": dominant, "total": total, "score": score}


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

    def _latest_bar_ms(self, coin: str) -> int:
        """该币最快周期(timeframes[0])最新 bar 的 open_ms；store 无此能力或异常→0（不误判新鲜度）。"""
        fn = getattr(self.store, "latest_candle_ms", None)
        if fn is None or not self.timeframes:
            return 0
        try:
            return int(fn(coin, self.timeframes[0]) or 0)
        except Exception:  # noqa: BLE001
            return 0

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
                         "align": mtf_alignment(by_tf),
                         "last_ms": self._latest_bar_ms(coin),
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
        # 数据新鲜度（诚实标注：实时板不静默展示陈旧数据）：最新 bar 时间 + 陈旧告警
        fresh = max((r.get("last_ms", 0) for r in rows), default=0)
        if fresh > 0:
            stale = now_ms > 0 and (now_ms - fresh) > 1_800_000  # >30min 视为陈旧(最快 15m)
            note = "  ⚠️数据陈旧(采集器可能停摆)" if stale else ""
            lines.append(f"🕒 数据更新至 {fmt_ts(fresh)}{note}")
        # 市场级态势：把矩阵聚合成全市场波动广度
        mr = market_regime(rows)
        if mr["label"]:
            lines.append(f"📊 市场态势: {mr['label']}")
        # 动向摘要：把矩阵综合成可操作情报
        hl = volatility_highlights(rows)
        if hl["squeeze"]:
            lines.append("🔸蓄势(压缩): " + " ".join(
                f"{x['coin']}/{x['tf']}" for x in hl["squeeze"]))
        if hl["expansion"]:
            lines.append("🔶放量(扩张): " + " ".join(
                f"{x['coin']}/{x['tf']}({x['velocity']:+.1f}%)" for x in hl["expansion"]))
        if hl["extreme_pd"]:
            lines.append("⚡极端PD: " + " ".join(
                f"{x['coin']}/{x['tf']}({x['pd_zone']}{x['pd_pct'] * 100:.0f}%)"
                for x in hl["extreme_pd"]))
        for r in rows[:top]:
            al = r.get("align") or {"bias": "分歧", "aligned": 0, "total": 0}
            bias_mark = {"多": "🟢多", "空": "🔴空", "分歧": "⚪分歧"}[al["bias"]]
            lines.append(
                f"━ {r['coin']:<8} 运动分 {r['score']:.1f}"
                f" · {bias_mark}({al['aligned']}/{al['total']}周期一致)")
            for tf in self.timeframes:
                m = r["by_tf"].get(tf)
                if not m:
                    continue
                v, a = m["velocity"], m["accel"]
                vdir = "🟢↑" if v >= 0 else "🔴↓"
                adir = "加速" if a * v > 0 else ("减速" if a * v < 0 else "—")
                lines.append(
                    f"  {tf:<4} {vdir}{abs(v):.2f}% a{a:+.2f}{adir}"
                    f" σ{m['rv']:.2f}%[{m['regime']}] ATR{m['atr_pct']:.2f}% 幅{m['range_pct']:.2f}%"
                    f" PD{m['pd_pct'] * 100:.0f}%{m['pd_zone']}"
                )
        return "\n".join(lines)
