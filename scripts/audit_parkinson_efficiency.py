#!/usr/bin/env python3
"""审计 #197 自己的 build:Parkinson(高低幅)在**真实加密**上真比 close-to-close rv 更能预测未来波动吗?

CLAUDE.md §一-3(先实证):Parkinson 5× 效率建立在 GBM/无跳空假设上,但加密有**肥尾+插针(flash wick)**——
一根插针让 (ln H/L)² 爆炸→Parkinson 可能被 wick 虚高/加噪,抵消效率。校准测试只证 GBM 无偏,真实数据须另验。

方法:真实 N 币 15m,滚动窗 W=20 算 rv(close-to-close σ) 与 pk(Parkinson),各与**未来 h-bar 已实现波动**
(close-to-close,同一 target,公平)相关。pk corr > rv corr ⇒ Parkinson 更优(效率兑现);并查偏置 mean(pk/rv)。

用法:PYTHONPATH=src ./.venv/bin/python scripts/audit_parkinson_efficiency.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.bitget import BitgetREST  # noqa: E402
from smc_tracker.monitor.volatility_monitor import parkinson_vol  # noqa: E402

_W = 20
_TF = "15m"
_BARS = 1500
_HZ = (1, 5, 10)
_COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "TRX",
          "DOT", "LTC", "BCH", "NEAR", "APT", "ARB", "OP", "SUI", "INJ", "TIA"]
_PK_FACTOR = 1.0 / (4.0 * np.log(2.0))
_GA, _GB = 0.10, 0.85          # GARCH 固定参数(同生产)


def _garch_fc(innov: np.ndarray, vlong: float, a: float = _GA, b: float = _GB) -> np.ndarray:
    """GARCH(1,1) 一步预测 σ% 序列,吃 innovation 序列(标准=r²,range=PK²)。

    σ²_{t+1}=ω+α·innov[t]+β·σ²_t,ω=(1-α-β)·vlong。fc[t]=在 t 对下一 bar 的预测。
    """
    omega = (1.0 - a - b) * vlong
    n = innov.size
    fc = np.empty(n)
    sig2 = vlong
    for t in range(n):
        sig2 = omega + a * innov[t] + b * sig2
        fc[t] = sig2
    return np.sqrt(np.maximum(fc, 0.0)) * 100.0


async def _fetch(coins):
    out = {}
    async with BitgetREST() as cli:
        for c in coins:
            try:
                cs = await cli.klines(f"{c}USDT", _TF, _BARS, coin=c)
                if len(cs) >= _W + max(_HZ) + 50:
                    out[c] = (np.array([k.c for k in cs], float),
                              np.array([k.h for k in cs], float),
                              np.array([k.l for k in cs], float))
            except Exception as e:  # noqa: BLE001
                print(f"  跳过 {c}: {e}")
    return out


def main_sync(cm):
    # 池化所有币的 (rv_t, pk_t, future_realized_h) + GARCH 标准/range
    cols = {h: {"rv": [], "pk": [], "gstd": [], "grng": [], "real": []} for h in _HZ}
    pk_over_rv = []
    for c, (close, hi, lo) in cm.items():
        if np.any(close <= 0) or np.any(lo <= 0):
            continue
        logret = np.diff(np.log(np.clip(close, 1e-12, None)))     # len N-1
        lr2 = np.log(np.clip(hi, 1e-12, None) / np.clip(lo, 1e-12, None)) ** 2  # 每 bar (ln H/L)²,len N
        n = logret.size
        if n < _W + max(_HZ) + 1:
            continue
        # 滚动 rv(close-to-close σ%): 末 W logret 的 std,索引 t 对应 logret[t-W+1:t+1]
        rv = sliding_window_view(logret, _W).std(axis=1, ddof=0) * 100.0   # len n-W+1,t=W-1..n-1
        # 滚动 pk(Parkinson σ%): 用对应 K 线的 lr2(对齐 logret:logret[i]=close[i+1]/close[i],配 bar i+1)
        pk = np.sqrt(sliding_window_view(lr2[1:], _W).mean(axis=1) * _PK_FACTOR) * 100.0  # 对齐 rv
        m = min(rv.size, pk.size)
        rv, pk = rv[:m], pk[:m]
        # 偏置比
        valid = (rv > 1e-9)
        pk_over_rv.extend((pk[valid] / rv[valid]).tolist())
        # GARCH 标准(吃 r²)vs range(吃 PK²)——对齐 logret(len n)
        vlong = float(np.var(logret, ddof=0))
        std_innov = logret * logret                         # r²
        rng_innov = lr2[1:] * _PK_FACTOR                    # PK² 每 bar,对齐 logret
        g_std = _garch_fc(std_innov, vlong)                 # len n,fc[t]=对 bar t+1 预测
        g_rng = _garch_fc(rng_innov, vlong)
        # future realized(close-to-close over next h),i 对应 rv 的索引,logret 位置 t=W-1+i
        for h in _HZ:
            for i in range(m):
                t = _W - 1 + i           # logret 末位
                if t + h >= n:
                    break
                fr = float(np.std(logret[t + 1:t + 1 + h], ddof=0)) * 100.0 if h > 1 \
                    else abs(float(logret[t + 1])) * 100.0
                cols[h]["rv"].append(rv[i]); cols[h]["pk"].append(pk[i])
                cols[h]["gstd"].append(g_std[t]); cols[h]["grng"].append(g_rng[t])
                cols[h]["real"].append(fr)

    print("=" * 64)
    print(f"#198 Parkinson vs close-to-close rv 预测未来波动(真实 {len(cm)} 币 15m)")
    print(f"  偏置 mean(pk/rv)={np.mean(pk_over_rv):.3f} 中位={np.median(pk_over_rv):.3f}  "
          f"(≈1 无偏;>1.1 被 wick 虚高)")
    print("  视野h   rv_corr   pk_corr   pk−rv   样本")
    better = 0
    for h in _HZ:
        R = np.array(cols[h]["real"])
        if R.size > 50:
            rc = float(np.corrcoef(cols[h]["rv"], R)[0, 1])
            pc = float(np.corrcoef(cols[h]["pk"], R)[0, 1])
            better += pc > rc
            print(f"  {h:>3}bar  {rc:+.3f}   {pc:+.3f}   {pc - rc:+.3f}   n={R.size}")
    print("-" * 64)
    print("#199 range-GARCH(吃 PK²)vs 标准 GARCH(吃 r²)预测未来波动:")
    print("  视野h  标准GARCH  range-GARCH  range−标准  样本")
    g_better = 0
    for h in _HZ:
        R = np.array(cols[h]["real"])
        if R.size > 50:
            sc = float(np.corrcoef(cols[h]["gstd"], R)[0, 1])
            rc = float(np.corrcoef(cols[h]["grng"], R)[0, 1])
            g_better += rc > sc
            print(f"  {h:>3}bar  {sc:+.3f}     {rc:+.3f}      {rc - sc:+.3f}    n={R.size}")
    print("-" * 64)
    bias = np.median(pk_over_rv)
    if better >= 2 and 0.9 <= bias <= 1.15:
        print("结论:Parkinson 预测未来波动**多数视野胜 rv 且基本无偏**⇒5× 效率在真实加密兑现,#197 build 有据。")
    elif bias > 1.15:
        print(f"结论:Parkinson 系统性偏高(pk/rv 中位 {bias:.2f})——**被插针/肥尾虚高**;效率优势被 wick 抵消,docstring 须标此局限。")
    else:
        print("结论:Parkinson 未明显胜 rv——真实加密微观结构抵消理论效率;作可比备选量而非主测,诚实标注。")
    print("=" * 64)


async def main():
    print(f"取真实 {_TF} K 线 ({len(_COINS)} 币)...")
    cm = await _fetch(_COINS)
    print(f"成功 {len(cm)} 币\n")
    if cm:
        main_sync(cm)


if __name__ == "__main__":
    asyncio.run(main())
