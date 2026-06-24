"""Bitget 永续 K 线 DB 采集器（供谐波/布林带多周期计算使用）。

设计原则：
  - 共享单一 BitgetREST session（async with），避免 N 个 coin×tf 各建 TCP 连接。
  - asyncio.Semaphore 限流并发（默认 4），防 429 限流。
  - 单 coin/tf 异常 log.warning 吞掉，不中断其它组合。
  - 复用 bitget.rest.klines（含分页回填 + 429 重试），不重造轮子。
  - 写入通过 store.upsert_candles（INSERT OR REPLACE），跨重启持久，去重安全。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..bitget.rest import BitgetREST

log = logging.getLogger(__name__)


class BitgetCandleCollector:
    """批量采集 Bitget 永续 K 线并持久化到 SQLite。

    Attributes:
        coin_to_symbol: {coin 标签: Bitget symbol}，如 {"BTC": "BTCUSDT"}。
        timeframes:     要采集的 K 线周期列表，需在 GRANULARITY_MS 中。
        bars:           每个 coin/tf 拉取根数（传给 BitgetREST.klines）。
        store:          实现了 upsert_candles() 的存储对象（Store 实例或 duck-type）。
        sema_limit:     并发 semaphore 上限（防 429）。
    """

    __slots__ = ("coin_to_symbol", "timeframes", "bars", "store", "sema_limit")

    def __init__(
        self,
        coin_to_symbol: dict[str, str],
        timeframes: list[str],
        bars: int,
        store: Any,
        sema_limit: int = 4,
    ) -> None:
        self.coin_to_symbol = coin_to_symbol
        self.timeframes = timeframes
        self.bars = bars
        self.store = store
        self.sema_limit = sema_limit

    async def collect_once(self) -> int:
        """采集所有 coin×tf 的 K 线并写入 DB，返回总写入根数。

        对所有 (coin, tf) 组合并发（Semaphore 限流），共享单一
        BitgetREST session，单组合异常 log.warning 吞掉不中断。
        """
        sema = asyncio.Semaphore(self.sema_limit)
        total = 0

        async with BitgetREST() as bg:
            tasks = []
            for coin, symbol in self.coin_to_symbol.items():
                for tf in self.timeframes:
                    tasks.append(self._fetch_one(bg, sema, coin, symbol, tf))

            results = await asyncio.gather(*tasks, return_exceptions=False)
            for n in results:
                total += n

        return total

    async def _fetch_one(
        self,
        bg: BitgetREST,
        sema: asyncio.Semaphore,
        coin: str,
        symbol: str,
        tf: str,
    ) -> int:
        """采集单个 coin/tf 的 K 线，写入 DB，返回写入根数。

        异常在此层 log.warning 吞掉（单组合失败不影响整批）。
        """
        async with sema:
            try:
                candles = await bg.klines(symbol, tf, bars=self.bars, coin=coin)
                if not candles:
                    return 0
                rows = [
                    (c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v)
                    for c in candles
                ]
                self.store.upsert_candles(rows)
                return len(rows)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "candle_collector: coin=%s tf=%s 采集失败，跳过。原因: %s",
                    coin, tf, exc,
                )
                return 0
