"""Bitget 永续多周期布林带压力/支撑监控器。

拉取多币种 × 多周期 K 线，计算布林带 %B 位置，输出多空共识 + 关键压力/支撑位卡片。
设计原则：
  - asyncio.Semaphore(≤8) 限流并发（防 Bitget 限流）
  - 单币单周期异常只 log.warning，不中断整体
  - render 纯函数（接受 rows，不直接 I/O），便于测试
  - 价格全部 util.fmt_px（非科学计数法）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..bitget.rest import BitgetREST
from ..indicators.bollinger_bands import analyze_tf, aggregate_coin
from ..util import fmt_px, fmt_ts

log = logging.getLogger("bb_monitor")

_SEMA_LIMIT = 4  # 最大并发 Bitget 请求数（降并发避 429 限流；大周期回填会放大请求量）


class BitgetBBMonitor:
    """多币种 × 多周期布林带压力/支撑监控器。

    Attributes:
        coin_to_symbol: {coin: bitget_symbol}，如 {"BTC": "BTCUSDT"}
        timeframes:     需要计算的 granularity 列表，如 ["5m","15m","30m","1H","4H","1D","1W"]
        bars:           每个周期拉取根数
        period:         布林带均线周期（默认 20）
        k:              标准差倍数（默认 2.0）
        top_n:          最多监控前 N 个币
        rest_base:      Bitget REST API base URL
    """
    __slots__ = (
        "coin_to_symbol", "timeframes", "bars", "period", "k", "top_n", "rest_base"
    )

    def __init__(
        self,
        coin_to_symbol: dict[str, str],
        timeframes: list[str],
        bars: int,
        period: int,
        k: float,
        top_n: int,
        rest_base: str = "https://api.bitget.com",
    ) -> None:
        self.coin_to_symbol = coin_to_symbol
        self.timeframes = timeframes
        self.bars = bars
        self.period = period
        self.k = k
        self.top_n = top_n
        self.rest_base = rest_base

    async def refresh(self, now_ms: int) -> list[dict]:
        """并发拉取所有币种 × 周期 K 线，计算布林带，返回汇总行。

        Args:
            now_ms: 当前时间戳（毫秒），用于日志/标注

        Returns:
            list[dict] 每条: {coin, symbol, price, tfs:{tf:analyze_tf|None}, agg:aggregate_coin}
            按 |consensus_pct-50| 降序（共识最强排前）。
        """
        coins = list(self.coin_to_symbol.items())[:self.top_n]
        sema = asyncio.Semaphore(_SEMA_LIMIT)

        async def _fetch_tf(
            bg: BitgetREST, symbol: str, coin: str, tf: str
        ) -> tuple[str, str, str, Any]:
            """拉单币单周期 K 线并计算 analyze_tf，返回 (coin, symbol, tf, result|None)。"""
            async with sema:
                try:
                    candles = await bg.klines(symbol, tf, bars=self.bars, coin=coin)
                    result = analyze_tf(candles, period=self.period, k=self.k)
                    return (coin, symbol, tf, result)
                except Exception as exc:  # noqa: BLE001
                    log.warning("BB 数据拉取失败 %s/%s: %s", coin, tf, exc)
                    return (coin, symbol, tf, None)

        # 共享单一 BitgetREST session（T1：避免每 币×周期 新建会话的 N+1 握手/限流放大）
        async with BitgetREST(base=self.rest_base) as bg:
            tasks = [
                _fetch_tf(bg, symbol, coin, tf)
                for coin, symbol in coins
                for tf in self.timeframes
            ]
            results_raw = await asyncio.gather(*tasks)

        # 按币汇聚
        by_coin: dict[str, dict] = {}
        for coin, symbol, tf, result in results_raw:
            if coin not in by_coin:
                by_coin[coin] = {
                    "coin": coin,
                    "symbol": symbol,
                    "price": 0.0,
                    "tfs": {},
                }
            by_coin[coin]["tfs"][tf] = result
            # 价格取任意有效周期末值
            if result is not None and by_coin[coin]["price"] == 0.0:
                by_coin[coin]["price"] = result["price"]

        # aggregate_coin + 组装 rows
        rows: list[dict] = []
        for row in by_coin.values():
            agg = aggregate_coin(row["tfs"])
            row["agg"] = agg
            rows.append(row)

        # 按共识强度降序排列（|consensus_pct-50| 越大=共识越强）
        rows.sort(key=lambda r: abs(r["agg"]["consensus_pct"] - 50), reverse=True)
        return rows

    def render(self, rows: list[dict], now_ms: int) -> str | None:
        """渲染布林带多周期压力/支撑卡片。

        Args:
            rows:   refresh() 返回值
            now_ms: 当前时间戳（毫秒）

        Returns:
            格式化卡片字符串；rows 为空时返回 None。
        """
        if not rows:
            return None

        ts = fmt_ts(now_ms)
        n_coins = len(rows)
        n_tfs = len(self.timeframes)

        lines: list[str] = [
            f"📐 Bitget 布林带多周期 压力/支撑 [{ts}] (数据源: Bitget永续 · TA-Lib BBANDS)",
            f"近窗 {n_coins}币 × {n_tfs}周期 研判（上轨=压力 下轨=支撑，%B=带内位置；越偏=趋势越强）",
        ]

        # ---- 多周期共识区 ----
        lines.append("【📊 多周期共识】")
        for row in rows:
            coin  = row["coin"]
            price = row["price"]
            agg   = row["agg"]
            bull_n  = agg["bull_n"]
            bear_n  = agg["bear_n"]
            pct     = agg["consensus_pct"]
            lean    = agg["lean_label"]
            sqz_n   = agg["squeeze_n"]

            squeeze_note = f"  挤压{sqz_n}周期⚠" if sqz_n > 0 else ""
            line = (
                f"  • {coin:<8} 🟢多{bull_n} 🔴空{bear_n} → {lean} {pct}%"
                f"  现价 {fmt_px(price)}{squeeze_note}"
            )
            lines.append(line)

        # ---- 关键压力/支撑区（每币挑 pct_b 最极端周期）----
        lines.append("【🎯 关键压力/支撑】(每币挑 %B 最极端周期，给具体压力/支撑位)")
        for row in rows:
            coin = row["coin"]
            tfs  = row["tfs"]
            # 找 pct_b 最极端的周期（最接近 0 或 1）
            best_tf: str | None = None
            best_extreme: float = -1.0
            best_result: dict | None = None
            for tf, result in tfs.items():
                if result is None:
                    continue
                pct_b = result["pct_b"]
                # 极端度 = max(pct_b, 1-pct_b) 越大越极端
                extreme = max(pct_b, 1.0 - pct_b)
                if extreme > best_extreme:
                    best_extreme = extreme
                    best_tf = tf
                    best_result = result
            if best_result is None or best_tf is None:
                continue

            upper     = best_result["upper"]
            lower     = best_result["lower"]
            pct_b     = best_result["pct_b"]
            pos_label = best_result["pos_label"]
            # 方向标记
            direction = "多" if best_result["bull"] else "空"
            # pct_b 格式
            pct_b_str = f"{pct_b:.2f}"
            line = (
                f"  • {coin} {best_tf} {pos_label}"
                f"  压力{fmt_px(upper)} / 支撑{fmt_px(lower)}"
                f"  %B{pct_b_str}"
            )
            lines.append(line)

        return "\n".join(lines)
