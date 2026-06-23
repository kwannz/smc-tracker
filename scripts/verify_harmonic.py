"""谐波形态真实数据冒烟验证。

用法：PYTHONPATH=src ./.venv/bin/python scripts/verify_harmonic.py

实拉 BTC/ETH 多周期真实 Bitget 永续 K 线 → analyze_candles → render 卡片。
若近窗无完整形态，至少打印 forming（前瞻PRZ）或枢轴数，证明管线通。
"""
from __future__ import annotations

import asyncio
import time

# ---- 导入 ----
from smc_tracker.bitget.rest import BitgetREST
from smc_tracker.indicators.harmonic import analyze_candles, find_pivots
from smc_tracker.monitor.harmonic_monitor import HarmonicMonitor


async def main() -> None:
    coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
    timeframes = ["1H", "4H", "1D"]
    bars = 500

    print("=" * 60)
    print("谐波形态真实数据冒烟验证")
    print("=" * 60)

    now_ms = int(time.time() * 1000)

    async with BitgetREST() as bg:
        for coin, symbol in coins.items():
            for tf in timeframes:
                try:
                    candles = await bg.klines(symbol, tf, bars=bars, coin=coin)
                    print(f"\n[{coin}/{tf}] 拉取 K 线 {len(candles)} 根，"
                          f"最新收盘价={candles[-1].c:.4f}")

                    # 枢轴数
                    pivots = find_pivots(candles, order=3)
                    print(f"  枢轴数: {len(pivots)}", end="")
                    if pivots:
                        types = "".join(p[2] for p in pivots[-8:])
                        print(f"  最近8个: {types}", end="")
                    print()

                    # 完整分析
                    result = analyze_candles(candles, order=3, tol=0.05)
                    n_c = len(result["completed"])
                    n_f = len(result["forming"])
                    print(f"  completed={n_c}  forming={n_f}  "
                          f"price={result['price']:.4f}")

                    if result["completed"]:
                        for h in result["completed"][:3]:
                            print(f"    [完整] {h['pattern']} {h['direction']}"
                                  f" PRZ {h['prz'][0]:.2f}–{h['prz'][1]:.2f}"
                                  f" conf={h['confidence']:.2f}")

                    if result["forming"]:
                        for h in result["forming"][:3]:
                            print(f"    [成形] {h['pattern']} {h['direction']}"
                                  f" PRZ {h['prz'][0]:.2f}–{h['prz'][1]:.2f}"
                                  f" conf={h['confidence']:.2f}")

                except Exception as exc:
                    print(f"  [错误] {coin}/{tf}: {exc}")

    print("\n" + "=" * 60)
    print("HarmonicMonitor.render() 真实渲染")
    print("=" * 60)

    monitor = HarmonicMonitor(
        coin_to_symbol=coins,
        timeframes=timeframes,
        bars=bars,
        order=3,
        tol=0.05,
        top_n=2,
    )

    rows = await monitor.refresh(now_ms)
    print(f"\nrefresh() 返回 {len(rows)} 行（有形态）")
    card = monitor.render(rows, now_ms)
    if card:
        print("\n" + card)
    else:
        print("\n（本次窗口无完整/成形形态，管线正常运行，只是近期 K 线无匹配谐波结构）")
        print("  注：谐波形态需要精确 5 波枢轴结构，并非每个时间窗都会出现，这是正常现象。")

    print("\n冒烟验证完成")


if __name__ == "__main__":
    asyncio.run(main())
