#!/usr/bin/env python3
"""谐波回测效率基准（#204/#205 优化后量化:bars/秒、各规模耗时）。

合成带摆动结构的 K 线(隔离纯计算,无网络),计时 harmonic_backtest 于 500/1000/2000/3000 bar,
报告耗时/吞吐/交易数;对比 require_sfg on/off(SFG 加 10 因子序列成本)。

用法:PYTHONPATH=src ./.venv/bin/python scripts/bench_backtest.py
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.backtest import harmonic_backtest  # noqa: E402
from smc_tracker.models import Candle  # noqa: E402


def _candles(n: int) -> list[Candle]:
    """n 根带多尺度摆动的合成 K 线(触发谐波形态;确定性,无随机)。"""
    cs = []
    px = 100.0
    for i in range(n):
        # 叠加多周期正弦 → 制造 XABCD 级摆动结构
        drift = 0.02 * math.sin(i / 7.0) + 0.012 * math.sin(i / 23.0) + 0.006 * math.sin(i / 3.0)
        px *= math.exp(drift)
        h, l = px * 1.008, px * 0.992
        cs.append(Candle("X", "1H", i * 3_600_000, (i + 1) * 3_600_000, px, h, l, px, 1.0, 0))
    return cs


def main() -> None:
    print("谐波回测效率基准（#204 bound + #205 skip_knn 后；合成数据纯计算）")
    print("=" * 66)
    print(f"  {'规模':>6}{'SFG':>6}{'耗时':>9}{'吞吐':>12}{'交易':>6}")
    for n in (500, 1000, 2000, 3000):
        cs = _candles(n)
        for sfg in (False, True):
            t0 = time.perf_counter()
            res = harmonic_backtest("X", "1H", cs, require_sfg=sfg)
            dt = time.perf_counter() - t0
            ntr = len([t for t in res.trades])
            print(f"  {n:>6}{('on' if sfg else 'off'):>6}{dt:>8.2f}s"
                  f"{n / dt:>9.0f} bar/s{ntr:>6}")
    print("-" * 66)
    print("  读法:bar/s 越高越好;若 2000-3000 bar 仍秒级=可真实规模回测;线性≈O(n)优化已生效。")
    print("=" * 66)


if __name__ == "__main__":
    main()
