"""信号计算链路延迟基准（确定性，无网络）——实证「低延迟」声称。

逐项计时热路径计算单元在真实规模 K 线缓冲上的耗时，输出 P50/P99/P99.9：
  指标全计算 / TA 全景 / TA 多因子信号 / 暴涨雷达 / 前瞻资金流预测。
运行：PYTHONPATH=src ./.venv/bin/python scripts/bench_latency.py [bars] [iters]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.indicators import analyze as ta_analyze, compute_indicators  # noqa: E402
from smc_tracker.models import Candle  # noqa: E402
from smc_tracker.signals import FlowPredictor, PumpRadar, TASignal  # noqa: E402


def make_candles(bars: int, seed: int = 7) -> list[Candle]:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, bars)))
    out: list[Candle] = []
    t0 = 1_700_000_000_000
    for i in range(bars):
        c = float(close[i])
        o = float(close[i - 1]) if i else c
        hi = max(o, c) * (1 + abs(rng.normal(0, 0.003)))
        lo = min(o, c) * (1 - abs(rng.normal(0, 0.003)))
        out.append(Candle("BTC", "5m", t0 + i * 300_000, t0 + (i + 1) * 300_000,
                          o, hi, lo, c, float(rng.uniform(1e3, 5e3)), int(rng.uniform(50, 500))))
    return out


def bench(label: str, fn, iters: int) -> None:
    fn()                                            # 预热(JIT/缓存)
    samples = np.empty(iters)
    for i in range(iters):
        t = time.perf_counter_ns()
        fn()
        samples[i] = (time.perf_counter_ns() - t) / 1e6   # ms
    p50, p99, p999 = np.percentile(samples, [50, 99, 99.9])
    print(f"  {label:16} P50={p50:.3f}ms  P99={p99:.3f}ms  P99.9={p999:.3f}ms  "
          f"max={samples.max():.3f}ms")


def main() -> None:
    bars = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    iters = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    candles = make_candles(bars)
    now = candles[-1].close_time_ms
    ta = TASignal()
    pr = PumpRadar()
    fp = FlowPredictor()
    for i in range(60):                              # 给前瞻预测器灌历史样本
        fp.push("BTC", float((-1) ** i) * 1e5, now - (60 - i) * 1000)

    print(f"⏱️ 信号计算链路延迟基准（{bars} 根 K 线 · {iters} 次迭代 · 单线程）")
    bench("指标全计算", lambda: compute_indicators(candles), iters)
    bench("TA全景analyze", lambda: ta_analyze(candles, now), iters)
    bench("TA多因子信号", lambda: ta.evaluate(candles, None, now), iters)
    bench("暴涨雷达", lambda: pr.evaluate("BTC", candles, now), iters)
    bench("前瞻资金流预测", lambda: fp.predict("BTC", now, 0.3, 0.05), iters)
    print("\n（纯计算，非阻塞 asyncio 热路径；端到端「接收→处理」延迟由 app 运行时埋点统计）")


if __name__ == "__main__":
    main()
