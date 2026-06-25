"""谐波形态端到端验证脚本（S5 真实数据验证）。

目标：5 高流动性币 (BTC/ETH/SOL/BNB/XRP) × (15m,1H)
  1. BitgetREST.klines 真实拉取（15m≥800根、1H≥500根）
  2. _clean_candles 清洗
  3. analyze_candles(order=5, tol=0.05) 谐波分析
  4. 打印每 (coin,tf) 的 K线根数/枢轴数/completed/forming + 示例 setup

asyncio + 单一 BitgetREST session + Semaphore(3) 限流。
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

# 直接子模块导入（任务要求）
from smc_tracker.indicators.harmonic import analyze_candles
from smc_tracker.monitor.candle_collector import _clean_candles
from smc_tracker.bitget.rest import BitgetREST

# 谐波分析中 pivots_from_structure 依赖 MarketStructure.swings（append-only）
# 直接在 analyze_candles 内部调用，无需额外导入

# ---- 配置 ----
COINS: list[dict[str, str]] = [
    {"coin": "BTC", "symbol": "BTCUSDT"},
    {"coin": "ETH", "symbol": "ETHUSDT"},
    {"coin": "SOL", "symbol": "SOLUSDT"},
    {"coin": "BNB", "symbol": "BNBUSDT"},
    {"coin": "XRP", "symbol": "XRPUSDT"},
]

# tf → 目标 K 线根数（任务：15m≥800，1H≥500）
TF_BARS: dict[str, int] = {
    "15m": 850,
    "1H":  550,
}

SEMAPHORE_LIMIT = 3  # 限流并发
HARMONIC_ORDER = 5   # 枢轴邻域
HARMONIC_TOL   = 0.05  # 比率容差 5%


async def fetch_one(
    bg: BitgetREST,
    sema: asyncio.Semaphore,
    coin: str,
    symbol: str,
    tf: str,
    bars: int,
) -> dict[str, Any]:
    """拉取单个 (coin, tf) K 线并运行谐波分析，返回结果字典。"""
    async with sema:
        try:
            raw_candles = await bg.klines(symbol, tf, bars=bars, coin=coin)
        except Exception as exc:
            return {
                "coin": coin,
                "tf": tf,
                "error": f"klines 拉取失败: {exc!r}",
                "raw_count": 0,
                "clean_count": 0,
                "pivot_count": 0,
                "completed": [],
                "forming": [],
                "price": 0.0,
            }

        raw_count = len(raw_candles)

        # 清洗数据质量守卫
        cleaned = _clean_candles(raw_candles)
        clean_count = len(cleaned)

        if clean_count < 10:
            return {
                "coin": coin,
                "tf": tf,
                "error": f"清洗后数据不足（{clean_count} 根），无法分析",
                "raw_count": raw_count,
                "clean_count": clean_count,
                "pivot_count": 0,
                "completed": [],
                "forming": [],
                "price": 0.0,
            }

        # 谐波分析
        result = analyze_candles(cleaned, order=HARMONIC_ORDER, tol=HARMONIC_TOL)

        # 枢轴数：analyze_candles 不直接返回，复现以获取计数
        from smc_tracker.indicators.harmonic import pivots_from_structure
        pivots = pivots_from_structure(cleaned, order=HARMONIC_ORDER)
        pivot_count = len(pivots)

        return {
            "coin": coin,
            "tf": tf,
            "error": None,
            "raw_count": raw_count,
            "clean_count": clean_count,
            "pivot_count": pivot_count,
            "completed": result.get("completed", []),
            "forming": result.get("forming", []),
            "price": result.get("price", 0.0),
        }


def fmt_setup(setup: dict) -> str:
    """格式化一个 setup（completed 或 forming）为可读字符串。"""
    pat = setup.get("pattern", "?")
    direction = setup.get("direction", "?")
    prz = setup.get("prz", (0.0, 0.0))
    conf = setup.get("confidence", 0.0)
    completed = setup.get("completed", False)
    status = "completed" if completed else "forming"
    prz_str = f"PRZ=[{prz[0]:.4f},{prz[1]:.4f}]"
    return (
        f"  {status} | {pat} {direction.upper()} | "
        f"{prz_str} | conf={conf:.2f}"
    )


async def main() -> None:
    """主流程：并发拉取 5币×2周期，打印每组分析结果。"""
    print("=" * 70)
    print("谐波形态端到端验证（S5）")
    print(f"币种: {[c['coin'] for c in COINS]}")
    print(f"周期: {list(TF_BARS.keys())}")
    print(f"order={HARMONIC_ORDER}, tol={HARMONIC_TOL}")
    print("=" * 70)

    sema = asyncio.Semaphore(SEMAPHORE_LIMIT)
    tasks: list[asyncio.Task] = []

    async with BitgetREST() as bg:
        for coin_cfg in COINS:
            for tf, bars in TF_BARS.items():
                tasks.append(
                    asyncio.create_task(
                        fetch_one(
                            bg, sema,
                            coin_cfg["coin"],
                            coin_cfg["symbol"],
                            tf,
                            bars,
                        )
                    )
                )

        results = await asyncio.gather(*tasks)

    # ---- 打印结果 ----
    any_error = False
    for res in results:
        coin = res["coin"]
        tf   = res["tf"]
        print(f"\n[{coin}/{tf}]")

        if res["error"]:
            print(f"  ERROR: {res['error']}")
            any_error = True
            continue

        print(f"  K线: raw={res['raw_count']} | 清洗后={res['clean_count']}")
        print(f"  枢轴: {res['pivot_count']}")
        n_comp = len(res["completed"])
        n_form = len(res["forming"])
        print(f"  completed={n_comp}, forming={n_form}")
        print(f"  现价: {res['price']:.4f}")

        # 示例 setup（最多各 1 个）
        if res["completed"]:
            print("  [示例 completed]")
            print(fmt_setup(res["completed"][0]))
        if res["forming"]:
            print("  [示例 forming]")
            print(fmt_setup(res["forming"][0]))

    # ---- 汇总 ----
    print("\n" + "=" * 70)
    print("汇总")
    print(f"{'Coin/TF':<12} {'raw':>6} {'clean':>6} {'pivots':>8} {'comp':>6} {'form':>6}")
    print("-" * 50)
    for res in results:
        tag = f"{res['coin']}/{res['tf']}"
        if res["error"]:
            print(f"{tag:<12} ERROR: {res['error'][:40]}")
        else:
            print(
                f"{tag:<12} {res['raw_count']:>6} {res['clean_count']:>6} "
                f"{res['pivot_count']:>8} {len(res['completed']):>6} {len(res['forming']):>6}"
            )
    print("=" * 70)

    if any_error:
        print("\n警告: 部分 (coin,tf) 出现错误，见上方详情。")
        sys.exit(1)
    else:
        print("\n全部 (coin,tf) 拉取并分析完成。")


if __name__ == "__main__":
    asyncio.run(main())
