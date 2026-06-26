"""tests/test_candle_ingest.py — candle_ingest 门面单元测试。

设计原则：
  - 确定性，无真实网络（fake bg 返回固定 Candle 列表）。
  - 使用真实 SQLite 临时库（Store），验证真实落库行为。
  - 直接 import 子模块，测具体行为（不 mock 清洗逻辑）。
  - 用 asyncio.run() 驱动协程（Python 3.10+ 推荐，兼容 3.14）。

覆盖：
  1. backfill：正常落库根数；脏数据被 _clean_candles 剔除。
  2. detect_and_fill_gap：
     a. 库空 → 触发 backfill；
     b. 有缺口 → 触发回填，根数 > 0；
     c. 无缺口（latest 已是最近 bar）→ 返回 0，bg.klines 未被调用。
  3. ingest_ws_closed_bar：正常单根落库 True；脏单根 False。
"""
from __future__ import annotations

import asyncio
import math
import tempfile
import time
from typing import Any

import pytest

from smc_tracker.monitor.candle_ingest import (
    backfill,
    detect_and_fill_gap,
    ingest_ws_closed_bar,
)
from smc_tracker.models import Candle
from smc_tracker.storage.db import Store
from smc_tracker.bitget.rest import GRANULARITY_MS


def _run(coro):
    """跨 Python 版本安全运行协程（asyncio.run 创建新事件循环，无需 get_event_loop）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 辅助：构造合法 Candle
# ---------------------------------------------------------------------------

def _make_candle(
    coin: str = "BTC",
    tf: str = "1H",
    open_ms: int = 1_700_000_000_000,
    o: float = 100.0,
    h: float = 110.0,
    l: float = 90.0,
    c: float = 105.0,
    v: float = 1.0,
) -> Candle:
    gran_ms = GRANULARITY_MS.get(tf, 3_600_000)
    return Candle(
        coin=coin,
        interval=tf,
        open_time_ms=open_ms,
        close_time_ms=open_ms + gran_ms,
        o=o, h=h, l=l, c=c, v=v,
        n=0,
    )


# ---------------------------------------------------------------------------
# Fake BitgetREST（无网络，返回固定 Candle 列表）
# ---------------------------------------------------------------------------

class FakeBG:
    """仿 BitgetREST.klines 接口，记录调用次数和参数。"""

    def __init__(self, candles: list[Candle]) -> None:
        self._candles = candles
        self.call_count: int = 0
        self.last_kwargs: dict[str, Any] = {}

    async def klines(
        self,
        symbol: str,
        granularity: str,
        bars: int = 300,
        coin: str = "",
    ) -> list[Candle]:
        self.call_count += 1
        self.last_kwargs = {"symbol": symbol, "granularity": granularity, "bars": bars, "coin": coin}
        # 返回副本，防止测试间互相影响
        return list(self._candles)


# ---------------------------------------------------------------------------
# 辅助：创建临时 Store（真实 SQLite）
# ---------------------------------------------------------------------------

def _make_store() -> Store:
    tmp = tempfile.mktemp(suffix=".db")
    return Store(path=tmp)


# ---------------------------------------------------------------------------
# 测试 1：backfill 正常落库
# ---------------------------------------------------------------------------

class TestBackfill:
    def test_returns_written_count(self) -> None:
        """backfill 写入 N 根合法 Candle，返回 N。"""
        candles = [
            _make_candle(open_ms=1_700_000_000_000 + i * 3_600_000)
            for i in range(5)
        ]
        bg = FakeBG(candles)
        store = _make_store()

        result = _run(backfill(bg, "BTC", "BTCUSDT", "1H", 5, store))

        assert result == 5
        assert store.count_candles("BTC", "1H") == 5

    def test_dirty_candles_filtered(self) -> None:
        """脏数据（NaN 价格、负价、h<l）被 _clean_candles 过滤，不落库。"""
        nan_val = float("nan")
        dirty_candles = [
            # NaN 价格（open=NaN）
            _make_candle(open_ms=1_700_000_000_000, o=nan_val),
            # 负价（close < 0）
            _make_candle(open_ms=1_700_003_600_000, c=-1.0),
            # h < l（逻辑非法）
            _make_candle(open_ms=1_700_007_200_000, h=90.0, l=110.0),
            # 合法根
            _make_candle(open_ms=1_700_010_800_000),
        ]
        bg = FakeBG(dirty_candles)
        store = _make_store()

        result = _run(backfill(bg, "BTC", "BTCUSDT", "1H", 4, store))

        # 只有 1 根合法
        assert result == 1
        assert store.count_candles("BTC", "1H") == 1

    def test_empty_response_returns_zero(self) -> None:
        """bg.klines 返回空列表 → backfill 返回 0，不写库。"""
        bg = FakeBG([])
        store = _make_store()

        result = _run(backfill(bg, "BTC", "BTCUSDT", "1H", 10, store))

        assert result == 0
        assert store.count_candles("BTC", "1H") == 0


# ---------------------------------------------------------------------------
# 测试 2：detect_and_fill_gap
# ---------------------------------------------------------------------------

class TestDetectAndFillGap:
    def test_empty_db_triggers_backfill(self) -> None:
        """库为空时 → detect_and_fill_gap 触发 backfill，bg.klines 被调用一次。"""
        candles = [_make_candle(open_ms=1_700_000_000_000 + i * 3_600_000) for i in range(3)]
        bg = FakeBG(candles)
        store = _make_store()

        result = _run(detect_and_fill_gap(bg, "BTC", "BTCUSDT", "1H", store))

        assert bg.call_count == 1
        assert result == 3
        assert store.count_candles("BTC", "1H") == 3

    def test_gap_triggers_backfill(self) -> None:
        """DB 有旧数据，存在缺口 → 触发回填，返回根数 > 0。"""
        gran_ms = GRANULARITY_MS["1H"]

        # 手动写一根很旧的 candle（1H 周期，3小时前），制造缺口
        now_ms = int(time.time() * 1000)
        old_open_ms = (now_ms // gran_ms - 3) * gran_ms  # 3 根前（有缺口）

        store = _make_store()
        store.upsert_candles([("BTC", "1H", old_open_ms, 100.0, 110.0, 90.0, 105.0, 1.0)])

        # FakeBG 返回足够覆盖缺口的 candles
        candles_to_return = [
            _make_candle(open_ms=old_open_ms + i * gran_ms) for i in range(4)
        ]
        bg = FakeBG(candles_to_return)

        result = _run(detect_and_fill_gap(bg, "BTC", "BTCUSDT", "1H", store))

        assert bg.call_count == 1, "有缺口时 bg.klines 应被调用一次"
        assert result > 0, "有缺口时应写入根数 > 0"

    def test_no_gap_returns_zero(self) -> None:
        """DB 的 latest 已是最近收盘 bar → 无缺口，返回 0，bg.klines 不被调用。"""
        gran_ms = GRANULARITY_MS["1H"]

        # latest_ms = 上一根收盘 bar 的 open_ms（即 detect_and_fill_gap 认定的 last_closed_open_ms）
        now_ms = int(time.time() * 1000)
        current_bar_open_ms = (now_ms // gran_ms) * gran_ms
        last_closed_open_ms = current_bar_open_ms - gran_ms  # 最近已收盘 bar

        store = _make_store()
        store.upsert_candles([("BTC", "1H", last_closed_open_ms, 100.0, 110.0, 90.0, 105.0, 1.0)])

        bg = FakeBG([])  # 不应被调用

        result = _run(detect_and_fill_gap(bg, "BTC", "BTCUSDT", "1H", store))

        assert result == 0, "无缺口时应返回 0"
        assert bg.call_count == 0, "无缺口时 bg.klines 不应被调用"

    def test_gap_alignment_independent_for_1d(self) -> None:
        """对齐无关性：1D bar 按交易所 UTC+8 边界(非纪元对齐)，gap 检测应锚定 latest_ms 而非纪元。

        Bitget 日线开盘 16:00 UTC(=北京0点)，与 (now//gran)*gran 的纪元对齐(00:00 UTC)差 16h。
        旧实现用纪元对齐算当前 bar，对 1D 错位；新实现锚定真实 latest_ms，与边界无关。
        构造 latest = now - 2.5 天的真实对齐 open（含 16h 偏移）→ 应检测出 1 根已收盘缺口。
        """
        gran_ms = GRANULARITY_MS["1D"]
        now_ms = int(time.time() * 1000)
        # 交易所对齐 open：UTC+8 日界（16:00 UTC 偏移），刻意非纪元对齐
        offset = 16 * 3_600_000
        latest_ms = ((now_ms - offset) // gran_ms) * gran_ms + offset - 2 * gran_ms  # 约 2 天前
        store = _make_store()
        store.upsert_candles([("BTC", "1D", latest_ms, 100.0, 110.0, 90.0, 105.0, 1.0)])
        candles = [_make_candle(open_ms=latest_ms + i * gran_ms) for i in range(4)]
        bg = FakeBG(candles)

        result = _run(detect_and_fill_gap(bg, "BTC", "BTCUSDT", "1D", store))
        # latest 约 2 天前 → 至少 1 根已收盘 bar 待回填（锚定 latest_ms，不受 16h 偏移干扰）
        assert bg.call_count == 1, "1D 有缺口时应触发回填（对齐无关）"
        assert result > 0


# ---------------------------------------------------------------------------
# 测试 3：ingest_ws_closed_bar
# ---------------------------------------------------------------------------

class TestIngestWsClosedBar:
    def test_valid_candle_returns_true(self) -> None:
        """合法 Candle 落库成功，返回 True，DB 行数+1。"""
        store = _make_store()
        candle = _make_candle(open_ms=1_700_000_000_000)

        result = ingest_ws_closed_bar(candle, store)

        assert result is True
        assert store.count_candles("BTC", "1H") == 1

    def test_dirty_candle_returns_false(self) -> None:
        """脏 Candle（h < l）→ 返回 False，不落库。"""
        store = _make_store()
        candle = _make_candle(open_ms=1_700_000_000_000, h=80.0, l=120.0)  # h < l 非法

        result = ingest_ws_closed_bar(candle, store)

        assert result is False
        assert store.count_candles("BTC", "1H") == 0

    def test_nan_price_returns_false(self) -> None:
        """NaN 价格 → 返回 False，不落库。"""
        store = _make_store()
        candle = _make_candle(open_ms=1_700_000_000_000, o=float("nan"))

        result = ingest_ws_closed_bar(candle, store)

        assert result is False
        assert store.count_candles("BTC", "1H") == 0

    def test_idempotent_upsert(self) -> None:
        """同一根 Candle 写两次 → 幂等（行数仍为 1）。"""
        store = _make_store()
        candle = _make_candle(open_ms=1_700_000_000_000)

        ingest_ws_closed_bar(candle, store)
        ingest_ws_closed_bar(candle, store)

        assert store.count_candles("BTC", "1H") == 1
