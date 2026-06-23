"""Bitget 布林带多周期 端到端冒烟测试（真实数据，非 mock）。

用法：
  PYTHONPATH=src ./.venv/bin/python scripts/verify_bb_board.py

拉取 BTC+ETH 各 7 周期真实 K 线，走完 analyze_tf → aggregate_coin → render 全链路，
打印真实卡片，证明端到端通。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.bitget.rest import BitgetREST, GRANULARITY_MS
from smc_tracker.indicators.bollinger_bands import analyze_tf, aggregate_coin, _HAS_TALIB
from smc_tracker.monitor.bitget_bb_monitor import BitgetBBMonitor

COINS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
}
TIMEFRAMES = ["5m", "15m", "30m", "1H", "4H", "1D", "1W"]
BARS = 300   # 冒烟用 300 根（减少等待）
PERIOD = 20


async def main() -> None:
    print(f"=== 布林带多周期端到端冒烟测试（TA-Lib available: {_HAS_TALIB}）===")
    print(f"使用 GRANULARITY_MS 中的 {len(GRANULARITY_MS)} 个周期")
    print()

    all_rows: list[dict] = []

    for coin, symbol in COINS.items():
        print(f"--- 拉取 {coin} ({symbol}) ---")
        tfs: dict = {}
        for tf in TIMEFRAMES:
            async with BitgetREST() as bg:
                candles = await bg.klines(symbol, tf, bars=BARS, coin=coin)
            result = analyze_tf(candles, period=PERIOD)
            tfs[tf] = result
            if result:
                price = result["price"]
                pct_b = result["pct_b"]
                upper = result["upper"]
                lower = result["lower"]
                label = result["pos_label"]
                squeeze = "⚠挤压" if result["squeeze"] else ""
                print(f"  {tf:5s}: price={price:.4f}  %B={pct_b:.3f}  "
                      f"上轨={upper:.4f} 下轨={lower:.4f}  {label}{squeeze}")
            else:
                print(f"  {tf:5s}: K线不足（{len(candles)} 根 < {PERIOD+1}）")
        agg = aggregate_coin(tfs)
        print(f"  汇总: 多{agg['bull_n']} 空{agg['bear_n']} → {agg['lean_label']} {agg['consensus_pct']}%  "
              f"挤压{agg['squeeze_n']}周期")
        print()
        # 组装 row
        price = next((v["price"] for v in tfs.values() if v), 0.0)
        all_rows.append({"coin": coin, "symbol": symbol, "price": price, "tfs": tfs, "agg": agg})

    # 按共识强度排序
    all_rows.sort(key=lambda r: abs(r["agg"]["consensus_pct"] - 50), reverse=True)

    # 渲染卡片
    monitor = BitgetBBMonitor(
        coin_to_symbol=COINS,
        timeframes=TIMEFRAMES,
        bars=BARS,
        period=PERIOD,
        k=2.0,
        top_n=10,
    )
    now_ms = int(time.time() * 1000)
    card = monitor.render(all_rows, now_ms)

    print("=" * 70)
    print("【完整推送卡片】")
    print("=" * 70)
    print(card)
    print("=" * 70)

    # 验证无科学计数
    if card:
        assert "e+" not in card.lower(), "卡片含科学计数 e+"
        assert "e-" not in card.lower(), "卡片含科学计数 e-"
        assert "布林带多周期" in card, "卡片缺少标题"
        assert "BTC" in card, "卡片缺少 BTC"
        assert "ETH" in card, "卡片缺少 ETH"
        assert "压力" in card or "支撑" in card, "卡片缺少压力/支撑"
        print()
        print("所有校验通过：无科学计数、含关键字段")


if __name__ == "__main__":
    asyncio.run(main())
