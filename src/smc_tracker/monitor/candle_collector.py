"""Bitget 永续 K 线 DB 采集器（供谐波/布林带多周期计算使用）。

设计原则：
  - 共享单一 BitgetREST session（async with），避免 N 个 coin×tf 各建 TCP 连接。
  - asyncio.Semaphore 限流并发（默认 4），防 429 限流。
  - 单 coin/tf 异常 log.warning 吞掉，不中断其它组合。
  - 复用 bitget.rest.klines（含分页回填 + 429 重试），不重造轮子。
  - 写入通过 store.upsert_candles（INSERT OR REPLACE），跨重启持久，去重安全。
  - collect_batch：增量轮转采集，每次取 batch_size 个币，offset 滚动覆盖全集（661 币分多轮）。
  - _clean_candles：数据质量守卫——拒 NaN/inf、ts 严格递增去重、价格>0、h>=l。
  - 超时重试：单 coin/tf TimeoutError 时以 retry_bars 减小分页页数重试 1 次（指数退避不无限重试）。
  - 冷启动检测：covered_coin_count 供调用方判断 DB 覆盖度，加速批量填满阶段。
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

# 超时重试：单次最大延迟（秒），防止重试本身阻塞轮转
_RETRY_DELAY_S: float = 1.0


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
        retry_bars:     超时时降级重试的 bars 数（减少分页页数，默认 100）。
                        100 根在单次 candles 端点即可覆盖（无需历史分页），大幅降低超时风险。
    """

    __slots__ = ("coin_to_symbol", "timeframes", "bars", "store", "sema_limit", "retry_bars")

    def __init__(
        self,
        coin_to_symbol: dict[str, str],
        timeframes: list[str],
        bars: int,
        store: Any,
        sema_limit: int = 4,
        retry_bars: int = 100,
    ) -> None:
        self.coin_to_symbol = coin_to_symbol
        self.timeframes = timeframes
        self.bars = bars
        self.store = store
        self.sema_limit = sema_limit
        # retry_bars 必须 <= bars，否则与原 bars 无区别；确保缩减
        self.retry_bars = min(retry_bars, bars) if bars > 0 else retry_bars

    def covered_coin_count(self, tf: str) -> int:
        """查询 DB 中指定 tf 已有数据的去重 coin 数（冷启动检测）。

        鸭子类型：若 store 有 SQLite conn（Store 实例），直接执行聚合查询（O(1)）；
        否则降级逐 coin 调用 count_candles（兜底，慢但正确）。
        任何异常静默返回 0（不阻塞主路径）。

        Args:
            tf: K 线周期，如 "1H"

        Returns:
            已有 >=1 根 K 线的去重 coin 数量；异常时返回 0。
        """
        try:
            conn = getattr(self.store, "conn", None)
            if conn is not None:
                # 快速路径：SQLite 聚合
                row = conn.execute(
                    "SELECT COUNT(DISTINCT coin) FROM bitget_candles WHERE tf=?",
                    (tf,),
                ).fetchone()
                return row[0] if row else 0
            # 慢速路径：逐 coin 查询（duck-type 兜底）
            count_fn = getattr(self.store, "count_candles", None)
            if count_fn is None:
                return 0
            return sum(
                1 for coin in self.coin_to_symbol if count_fn(coin, tf) > 0
            )
        except Exception:  # noqa: BLE001
            return 0

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

        超时处理策略（防 TimeoutError 永卡死长尾币）：
          - 首次 TimeoutError：等待 _RETRY_DELAY_S 后以 retry_bars 重试一次。
            retry_bars << bars，减少历史分页页数（如 100 根无需 history-candles 分页，
            可在 candles 单次端点完成，避免多次 HTTP 往返超时）。
          - 重试仍失败：log.warning 记录，跳过此 coin/tf，下轮再试。
            诚实跳过，不假装成功（CLAUDE.md §三-3：诚实标注）。
          - 非超时异常：直接 log.warning 吞掉，维持原有行为。
        """
        async with sema:
            try:
                candles = await bg.klines(symbol, tf, bars=self.bars, coin=coin)
            except asyncio.TimeoutError:
                # 首次超时：降级 bars 重试一次（减少分页，提高单次成功率）
                log.debug(
                    "candle_collector: coin=%s tf=%s 首次超时，降级 bars=%d 重试",
                    coin, tf, self.retry_bars,
                )
                await asyncio.sleep(_RETRY_DELAY_S)
                try:
                    candles = await bg.klines(symbol, tf, bars=self.retry_bars, coin=coin)
                except Exception as retry_exc:  # noqa: BLE001
                    log.warning(
                        "candle_collector: coin=%s tf=%s 重试仍失败，跳过。原因: %r",
                        coin, tf, retry_exc,
                    )
                    return 0
            except Exception as exc:  # noqa: BLE001
                # 非超时异常：与原有行为一致，直接吞掉 log.warning
                log.warning(
                    "candle_collector: coin=%s tf=%s 采集失败，跳过。原因: %r",
                    coin, tf, exc,
                )
                return 0

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
