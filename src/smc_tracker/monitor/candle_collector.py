"""Bitget 永续 K 线 DB 采集器（供谐波/布林带多周期计算使用）。

设计原则：
  - 共享单一 BitgetREST session（async with），避免 N 个 coin×tf 各建 TCP 连接。
  - asyncio.Semaphore 限流并发（默认 4），防 429 限流。
  - 单 coin/tf 异常 log.warning 吞掉，不中断其它组合。
  - 复用 bitget.rest.klines（含分页回填 + 429 重试），不重造轮子。
  - 写入通过 store.upsert_candles（INSERT OR REPLACE），跨重启持久，去重安全。
  - collect_batch：增量轮转采集，每次取 batch_size 个币，offset 滚动覆盖全集（661 币分多轮）。
  - _clean_candles：数据质量守卫——拒 NaN/inf、ts 严格递增去重、价格>0、h>=l。
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from ..bitget.rest import BitgetREST
from ..models import Candle
from ..util import to_float as _f

log = logging.getLogger(__name__)


def _clean_candles(candles: list[Candle]) -> list[Candle]:
    """清洗 K 线列表，过滤脏行并去重。

    清洗规则（CLAUDE.md §三-3 数据质量）：
      1. open_ms 严格递增去重：同 ts 保留后者（REST 返回的最新值更可信）
      2. 价格字段（o/h/l/c）非 NaN/inf 且 > 0
      3. 成交量（v）非 NaN/inf（允许 0）
      4. h >= l（价格逻辑合法性）

    Args:
        candles: 原始 Candle 列表（任意顺序）

    Returns:
        清洗后的 Candle 列表，按 open_time_ms 升序。
        脏行被跳过，通过 log.debug 计数告知调用方。
    """
    if not candles:
        return []

    dirty = 0
    # 按 ts 去重（同 ts 保留后者，更新），先按 open_time_ms 升序排序
    by_ts: dict[int, Candle] = {}
    for c in candles:
        by_ts[c.open_time_ms] = c  # 后者覆盖（REST 返回排序通常已升序，最新覆盖旧值）

    result: list[Candle] = []
    for c in sorted(by_ts.values(), key=lambda x: x.open_time_ms):
        # 价格字段有效性检查
        o, h, l, cl = _f(c.o), _f(c.h), _f(c.l), _f(c.c)
        v = _f(c.v) if c.v is not None else 0.0

        # 拒 NaN/inf（_f 已转 NaN/inf 为 0.0）并检查价格 > 0
        if not (o > 0 and h > 0 and l > 0 and cl > 0):
            dirty += 1
            continue
        # 成交量合法性（允许 0，但拒 NaN/inf）
        if not math.isfinite(v):
            dirty += 1
            continue
        # 高低价逻辑合法性
        if h < l:
            dirty += 1
            continue
        result.append(c)

    if dirty:
        log.debug("_clean_candles: 过滤脏行 %d 条（原始 %d 根）", dirty, len(candles))

    return result


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
        数据落库前经 _clean_candles 清洗。
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

    async def collect_batch(self, offset: int, batch_size: int) -> int:
        """增量轮转采集：从 coin_to_symbol 列表第 offset 起取 batch_size 个币（环绕），
        采其所有 timeframes K 线，清洗后 upsert；返回**下一个 offset**（环绕）。

        目的：全集 661 币分多轮覆盖，每轮只采 batch_size 个币，避免单轮爆量请求。

        Args:
            offset:     起始偏移（基于 list(coin_to_symbol.items()) 的索引）
            batch_size: 本轮采集的币数量

        Returns:
            下一个 offset，= (offset + batch_size) % 总币数。
            若 coin_to_symbol 为空则返回 0。
        """
        coins_all = list(self.coin_to_symbol.items())
        total_coins = len(coins_all)
        if total_coins == 0:
            return 0

        # 环绕轮转：从 offset 取 batch_size 个
        start = offset % total_coins
        # 连续取 batch_size 个（环绕处理）
        batch_coins: list[tuple[str, str]] = []
        for i in range(batch_size):
            idx = (start + i) % total_coins
            batch_coins.append(coins_all[idx])

        sema = asyncio.Semaphore(self.sema_limit)

        async with BitgetREST() as bg:
            tasks = [
                self._fetch_one(bg, sema, coin, symbol, tf)
                for coin, symbol in batch_coins
                for tf in self.timeframes
            ]
            await asyncio.gather(*tasks, return_exceptions=False)

        # 返回下一个 offset（环绕）
        return (start + batch_size) % total_coins

    async def _fetch_one(
        self,
        bg: BitgetREST,
        sema: asyncio.Semaphore,
        coin: str,
        symbol: str,
        tf: str,
    ) -> int:
        """采集单个 coin/tf 的 K 线，清洗后写入 DB，返回写入根数。

        异常在此层 log.warning 吞掉（单组合失败不影响整批）。
        """
        async with sema:
            try:
                candles = await bg.klines(symbol, tf, bars=self.bars, coin=coin)
                if not candles:
                    return 0
                # 数据质量守卫：清洗脏行（NaN/inf/负价/h<l/ts重复）
                candles = _clean_candles(candles)
                if not candles:
                    return 0
                rows = [
                    (c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v)
                    for c in candles
                ]
                self.store.upsert_candles(rows)
                return len(rows)
            except Exception as exc:  # noqa: BLE001
                # 用 repr(exc) 而非 str(exc)：无消息异常(TimeoutError()/ClientError() 等)
                # str() 为空会打出 "原因: "(空白，无法诊断)；repr 必含类型名，可诊断(§三-3)。
                log.warning(
                    "candle_collector: coin=%s tf=%s 采集失败，跳过。原因: %r",
                    coin, tf, exc,
                )
                return 0
