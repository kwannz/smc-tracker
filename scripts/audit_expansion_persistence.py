#!/usr/bin/env python3
"""严格重测「波动扩张持续性」——#153 的 90%/0.73 是否滚动窗重叠伪影?

CLAUDE.md §一-3(第一性原理:先实证)+§四-2(真实数据)+用户「修复验证/偏差」。
背景:#149 已发现**滚动 rv 重叠窗→机械自相关**(打乱 null 仍 0.944 未归零=测错),
改 ARCH 标准 |logret| 自相关后真实 lag-1=0.236。但 #153 报"P(仍扩张|当前扩张)=90.2%、
corr(rv_t,rv_{t+10})=0.725",用的正是同款滚动 vol_ratio(rv_win=20 在 lag-10 共享 10/20、
rv_long=60 共享 50/60)——疑同类伪影,且 **#153 未做 null 对照**。

方法:对真实 Bitget 15m K 线,用 vol_metrics 同款定义(rv_win=20/rv_long=60/expand=1.4)
算 regime 序列,在 lag h=10 measure:
  observed  P(扩张_{t+h}|扩张_t)、lift=cond/base、corr(rv_t,rv_{t+h})
  null      打乱 logret(销毁真实聚集、**保留**窗口重叠机械相关)后同样 measure(多 seed 均值)
  clean     #149 口径 |logret| 自相关(无滚动窗=真实聚集量)
诚实结论 = observed − null;null≈observed ⇒ 90%/0.73 纯属窗口伪影,须全系统纠正。

用法:PYTHONPATH=src ./.venv/bin/python scripts/audit_expansion_persistence.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.bitget import BitgetREST  # noqa: E402

_RV_WIN, _RV_LONG, _EXPAND = 20, 60, 1.4          # 与 volatility_monitor 同款
_LAG = 10                                          # #153 的 "10bar 后"
_TF = "15m"
_BARS = 1500
_NULL_SEEDS = 8
_COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "TRX",
          "DOT", "LTC", "BCH", "NEAR", "APT", "ARB", "OP", "SUI", "INJ", "TIA",
          "SEI", "PEPE", "WIF", "FIL", "ATOM"]


def _regime_series(logret: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (rv 序列, expanded 布尔序列),按 logret 位置对齐(前 _RV_LONG-1 个无效已切掉)。

    rv[i]=std(末 20 logret)、rv_long[i]=std(末 60 logret)、expanded=rv/rv_long>1.4。
    与 vol_metrics 完全同款(滚动、重叠)——故意复现 #153 的算法以检验其伪影性。
    """
    if logret.size < _RV_LONG + 1:
        return np.array([]), np.array([])
    rv = sliding_window_view(logret, _RV_WIN).std(axis=1, ddof=0)        # len-19
    rv_long = sliding_window_view(logret, _RV_LONG).std(axis=1, ddof=0)  # len-59
    # 对齐到 rv_long 的位置(更短),rv 截取尾部同长
    rv = rv[_RV_LONG - _RV_WIN:]
    ratio = np.divide(rv, rv_long, out=np.ones_like(rv), where=rv_long > 1e-9)
    return rv, ratio > _EXPAND


def _measure(logret: np.ndarray) -> tuple[int, int, int, float, float] | None:
    """单序列 measure:返回 (扩张配对数, 其中 t+lag 仍扩张数, 有效位数, 扩张总数, corr 用的两列拼接占位)。

    实际返回聚合所需原料:(n_pairs_expanded, n_persist, n_valid, n_expanded_total, _)
    corr 单独在调用方按 coin 累加 rv 对。
    """
    rv, expanded = _regime_series(logret)
    if rv.size <= _LAG + 1:
        return None
    last = rv.size - _LAG
    e_t = expanded[:last]
    e_future = expanded[_LAG:_LAG + last]
    n_valid = last
    n_expanded_total = int(e_t.sum())
    n_pairs = int(e_t.sum())
    n_persist = int((e_t & e_future).sum())
    return n_pairs, n_persist, n_valid, n_expanded_total, 0.0


def _rv_pairs(logret: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    rv, _ = _regime_series(logret)
    if rv.size <= _LAG + 1:
        return np.array([]), np.array([])
    last = rv.size - _LAG
    return rv[:last], rv[_LAG:_LAG + last]


def _abs_logret_autocorr(logret: np.ndarray, lags=(1, 5, 10, 20)) -> dict[int, float]:
    """#149 口径:|logret| 自相关(无滚动窗=真实波动聚集,非窗口伪影)。"""
    a = np.abs(logret)
    a = a - a.mean()
    out = {}
    for L in lags:
        if a.size <= L + 5:
            continue
        x, y = a[:-L], a[L:]
        denom = np.sqrt((x * x).sum() * (y * y).sum())
        out[L] = float((x * y).sum() / denom) if denom > 1e-12 else 0.0
    return out


def _ewma_fc_series(logret: np.ndarray, lam: float = 0.94) -> np.ndarray:
    """单遍 EWMA σ% 预测序列(匹配 volatility_monitor.ewma_vol 的 seed/λ 递推)。

    fc[t]=用 logret[:t+1] 在 t 时刻可得的 EWMA 波动率(%),即"在 t 对未来波动的预测"。
    seed=首 20 logret 样本方差,其后逐根 var=λ·var+(1-λ)·r²。
    """
    n = logret.size
    fc = np.full(n, np.nan)
    if n < 3:
        return fc
    seed_n = min(20, n)
    var = float(np.var(logret[:seed_n], ddof=0))
    fc[seed_n - 1] = np.sqrt(var) * 100.0
    for t in range(seed_n, n):
        var = lam * var + (1.0 - lam) * logret[t - 1] ** 2
        fc[t] = np.sqrt(var) * 100.0
    return fc


def _garch_sig2(logret: np.ndarray, alpha: float, beta: float) -> tuple[np.ndarray, float]:
    """GARCH(1,1) 条件方差序列 σ²_t(方差目标 ω=(1-α-β)·样本方差)。返回 (σ²_t, 长期方差)。"""
    n = logret.size
    vlong = float(np.var(logret, ddof=0)) or 1e-12
    omega = (1.0 - alpha - beta) * vlong
    s = np.empty(n)
    sig2 = vlong
    for t in range(n):
        s[t] = sig2
        sig2 = omega + alpha * logret[t] ** 2 + beta * sig2
    return s, vlong


def _fit_garch(series_map: dict[str, np.ndarray]) -> tuple[float, float]:
    """池化网格拟合 (α,β):最大化高斯对数似然 -0.5Σ(logσ²+r²/σ²)(跳前20根种子)。开源标准 GARCH(1,1) 拟合。"""
    best, best_ll = (0.08, 0.90), -1e18
    for alpha in (0.02, 0.04, 0.06, 0.08, 0.10, 0.14, 0.18):
        for beta in (0.75, 0.80, 0.85, 0.88, 0.90, 0.93, 0.96):
            if alpha + beta >= 0.999:
                continue
            ll = 0.0
            for lr in series_map.values():
                if lr.size < 40:
                    continue
                s, _ = _garch_sig2(lr, alpha, beta)
                s2, r2 = s[20:], lr[20:] ** 2
                ll += float(-0.5 * np.sum(np.log(s2) + r2 / s2))
            if ll > best_ll:
                best_ll, best = ll, (alpha, beta)
    return best


def _forecast_skill(series_map: dict[str, np.ndarray], horizons=(1, 3, 5, 10),
                    ab: tuple[float, float] | None = None) -> dict:
    """实证波动预测技巧:corr(在t的预测, [t+1,t+h]已实现波动),三法对比 rv持续/EWMA/GARCH(1,1)。

    诚实立(#177配套)+模型认知(#178):破完90%伪影,正面量化前瞻能力,并问"开源标准GARCH能否胜EWMA"。
    ab=None 走池化网格拟合;ab=(α,β) 用固定参数(验证免拟合生产可行性)。
    """
    ga, gb = ab if ab is not None else _fit_garch(series_map)
    persist = (ga + gb)
    out: dict[int, dict] = {"_garch_ab": (ga, gb)}
    for h in horizons:
        ew_a, rv_a, gc_a, real_a = [], [], [], []
        # h-bar 均值回归几何和:Σ_{k=1..h}(α+β)^{k-1}/h
        gsum = (1.0 - persist ** h) / (1.0 - persist) / h if persist < 0.999 else 1.0
        for lr in series_map.values():
            n = lr.size
            if n < _RV_WIN + h + 5:
                continue
            ew = _ewma_fc_series(lr)
            sig2, vlong = _garch_sig2(lr, ga, gb)
            for t in range(_RV_WIN, n - h):
                if not np.isfinite(ew[t]):
                    continue
                realized = (abs(float(lr[t + 1])) if h == 1
                            else float(np.std(lr[t + 1:t + 1 + h], ddof=0))) * 100.0
                rv_now = float(np.std(lr[t - _RV_WIN + 1:t + 1], ddof=0)) * 100.0
                # GARCH h-bar 预测:σ²_{t+1}=ω+αr_t²+βσ²_t(在t可得),均值回归到 vlong
                var_next = (1.0 - ga - gb) * vlong + ga * lr[t] ** 2 + gb * sig2[t]
                gc = np.sqrt(vlong + (var_next - vlong) * gsum) * 100.0
                ew_a.append(ew[t]); rv_a.append(rv_now); gc_a.append(gc); real_a.append(realized)
        if len(real_a) > 10:
            R = np.array(real_a)
            out[h] = {
                "ewma_corr": float(np.corrcoef(np.array(ew_a), R)[0, 1]),
                "rv_corr": float(np.corrcoef(np.array(rv_a), R)[0, 1]),
                "garch_corr": float(np.corrcoef(np.array(gc_a), R)[0, 1]),
                "n": len(real_a)}
    return out


async def _fetch(coins: list[str], tf: str = _TF, bars: int = _BARS,
                 cli: BitgetREST | None = None) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    own = cli is None
    if own:
        cli = BitgetREST()
        await cli.__aenter__()
    try:
        for c in coins:
            try:
                cs = await cli.klines(f"{c}USDT", tf, bars, coin=c)
                closes = np.array([k.c for k in cs], dtype=float)
                if closes.size >= 120 and np.all(np.isfinite(closes)):
                    closes = np.clip(closes, 1e-12, None)
                    out[c] = np.diff(np.log(closes))
            except Exception as e:  # noqa: BLE001
                print(f"  跳过 {c}/{tf}: {e}")
    finally:
        if own:
            await cli.__aexit__(None, None, None)
    return out


async def _garch_generalization(cli: BitgetREST) -> None:
    """#180:验证已上线固定参数 GARCH(α0.10/β0.85)是否跨周期仍胜 EWMA(#179 仅 15m 验证)。

    对 CANONICAL_TIMEFRAMES 子集逐周期测 GARCH−EWMA 预测技巧增益;稀疏周期数据少→诚实标注边界。
    """
    print("-" * 64)
    print("【#180 GARCH 跨周期泛化(固定 α0.10/β0.85,已上线参数)】GARCH−EWMA corr 增益")
    print("  周期   1bar    5bar    10bar   均值   样本/币")
    for tf in ("15m", "1H", "4H", "1D"):
        sm = await _fetch(_COINS, tf=tf, bars=1000, cli=cli)
        if len(sm) < 5:
            print(f"  {tf:<5} 数据不足({len(sm)}币),跳过"); continue
        fs = _forecast_skill(sm, ab=(0.10, 0.85))
        g = {h: fs[h]["garch_corr"] - fs[h]["ewma_corr"] for h in (1, 5, 10) if h in fs}
        avg = float(np.mean(list(g.values()))) if g else 0.0
        nbar = int(np.mean([v.size for v in sm.values()]))
        print(f"  {tf:<5} {g.get(1, 0):+.3f}  {g.get(5, 0):+.3f}  {g.get(10, 0):+.3f}  "
              f"{avg:+.3f}  {len(sm)}币×{nbar}")


def _agg(series_map: dict[str, np.ndarray], shuffle_seed: int | None = None):
    """聚合所有币:返回 (persist_rate, base_rate, lift, corr)。shuffle_seed!=None 走 null。"""
    tot_pairs = tot_persist = tot_valid = tot_exp = 0
    rv_a, rv_b = [], []
    rng = np.random.default_rng(shuffle_seed) if shuffle_seed is not None else None
    for lr in series_map.values():
        if rng is not None:
            lr = lr.copy()
            rng.shuffle(lr)
        m = _measure(lr)
        if m is None:
            continue
        n_pairs, n_persist, n_valid, n_exp, _ = m
        tot_pairs += n_pairs; tot_persist += n_persist
        tot_valid += n_valid; tot_exp += n_exp
        a, b = _rv_pairs(lr)
        if a.size:
            rv_a.append(a); rv_b.append(b)
    persist = tot_persist / tot_pairs if tot_pairs else 0.0
    base = tot_exp / tot_valid if tot_valid else 0.0
    lift = persist / base if base > 1e-9 else 0.0
    if rv_a:
        A, B = np.concatenate(rv_a), np.concatenate(rv_b)
        corr = float(np.corrcoef(A, B)[0, 1]) if A.size > 2 else 0.0
    else:
        corr = 0.0
    return persist, base, lift, corr


async def main() -> None:
    print(f"取真实 {_TF} K 线 ({_BARS} bar/币, {len(_COINS)} 币)...")
    sm = await _fetch(_COINS)
    print(f"成功 {len(sm)} 币\n")
    if not sm:
        print("无数据,退出"); return

    o_persist, o_base, o_lift, o_corr = _agg(sm)
    # null:多 seed 均值
    nps, nbs, nls, ncs = [], [], [], []
    for s in range(_NULL_SEEDS):
        p, b, l, c = _agg(sm, shuffle_seed=1000 + s)
        nps.append(p); nbs.append(b); nls.append(l); ncs.append(c)
    n_persist, n_lift, n_corr = float(np.mean(nps)), float(np.mean(nls)), float(np.mean(ncs))

    # clean #149 口径
    ac = {1: [], 5: [], 10: [], 20: []}
    for lr in sm.values():
        for L, v in _abs_logret_autocorr(lr).items():
            ac[L].append(v)
    ac_mean = {L: float(np.mean(vs)) for L, vs in ac.items() if vs}

    print("=" * 64)
    print("【扩张持续性 lag-10】observed vs null(打乱 logret 保留窗口重叠)")
    print(f"  P(仍扩张|当前扩张):  observed={o_persist:.1%}  null={n_persist:.1%}  "
          f"真实增益={o_persist - n_persist:+.1%}")
    print(f"  lift(=cond/base):    observed={o_lift:.2f}×  null={n_lift:.2f}×  "
          f"真实增益={o_lift - n_lift:+.2f}×")
    print(f"  corr(rv_t,rv_t+10):  observed={o_corr:.3f}  null={n_corr:.3f}  "
          f"真实增益={o_corr - n_corr:+.3f}")
    print(f"  (base 扩张占比={o_base:.1%})")
    print("-" * 64)
    print("【对照 #149 口径】|logret| 自相关(无滚动窗=真实波动聚集):")
    for L in (1, 5, 10, 20):
        if L in ac_mean:
            print(f"  lag-{L:<2}: {ac_mean[L]:+.3f}")
    print("-" * 64)
    fs = _forecast_skill(sm)
    ga, gb = fs["_garch_ab"]
    print(f"【波动预测技巧随视野(#178)+GARCH对比(#179,模型认知)】拟合 GARCH α={ga} β={gb} (α+β={ga + gb:.2f})")
    print("  视野h   rv持续   EWMA    GARCH   GARCH−EWMA  样本")
    for h in (1, 3, 5, 10):
        if h in fs:
            d = fs[h]
            print(f"  {h:>3}bar  {d['rv_corr']:+.3f}  {d['ewma_corr']:+.3f}  {d['garch_corr']:+.3f}  "
                  f"  {d['garch_corr'] - d['ewma_corr']:+.3f}     n={d['n']}")
    # 固定标准参数(免拟合,可进生产热路径)稳健性验证
    for fab in ((0.10, 0.85), (0.12, 0.80)):
        ff = _forecast_skill(sm, ab=fab)
        gains = [ff[h]["garch_corr"] - ff[h]["ewma_corr"] for h in (1, 3, 5, 10) if h in ff]
        print(f"  固定α={fab[0]}β={fab[1]}(α+β={sum(fab):.2f}): GARCH−EWMA 均 {np.mean(gains):+.3f} "
              f"(各视野 {', '.join(f'{g:+.3f}' for g in gains)})")
    print("=" * 64)
    art = (o_persist - n_persist) < 0.10 and (o_corr - n_corr) < 0.10
    if art:
        print("结论:observed≈null ⇒ 90%/0.73 主要是**滚动窗重叠机械伪影**,非真实前瞻 edge。")
        print(f"      真实波动聚集应以 #149 口径计:|logret| 自相关 lag-1≈{ac_mean.get(1, 0):.2f}。")
    else:
        print("结论:observed 显著高于 null ⇒ 扩张持续性含真实信号(超出窗口伪影部分)。")

    async with BitgetREST() as cli:
        await _garch_generalization(cli)


if __name__ == "__main__":
    asyncio.run(main())
