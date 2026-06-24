"""BitgetCandleCollector + bitget_candles DB 表单元测试（合成数据，不联网）。

覆盖：
  DB 层：
    - upsert_candles + get_candles 往返正确
    - INSERT OR REPLACE 去重（同 coin/tf/open_ms 覆盖，v 更新）
    - get_candles 升序返回 + 返回 Candle 对象（.o/.h/.l/.c/.v/.open_time_ms/.close_time_ms）
    - count_candles 正确
    - 空输入 → []（upsert/get 安全）
  Collector 层：
    - monkeypatch BitgetREST.klines 返回合成 Candle → collect_once 调用 upsert、返回根数
    - 单 coin 异常 → 吞掉不崩，其它 coin 继续
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage.db import Store
from smc_tracker.bitget.rest import BitgetREST, GRANULARITY_MS
from smc_tracker.models import Candle
from smc_tracker.monitor.candle_collector import BitgetCandleCollector


# ============================================================
# 辅助工厂
# ============================================================

def _make_candles(
    coin: str,
    tf: str,
    n: int,
    start_ms: int = 1_700_000_000_000,
) -> list[Candle]:
    """生成 n 根合成 Candle（升序，价格递增 0.1）。"""
    gran_ms = GRANULARITY_MS[tf]
    out: list[Candle] = []
    for i in range(n):
        ts = start_ms + i * gran_ms
        px = 100.0 + i * 0.1
        out.append(Candle(
            coin=coin,
            interval=tf,
            open_time_ms=ts,
            close_time_ms=ts + gran_ms,
            o=px,
            h=px + 0.5,
            l=px - 0.3,
            c=px + 0.1,
            v=1000.0 + i,
            n=0,
        ))
    return out


def _tmp_store(tmp_path: Path) -> Store:
    """创建临时 SQLite Store，测试结束后自动删除。"""
    return Store(path=tmp_path / "test.db")


# ============================================================
# DB 层测试
# ============================================================

class TestUpsertAndGet:
    """upsert_candles / get_candles / count_candles 基本契约。"""

    def test_roundtrip(self, tmp_path):
        """写入 5 根，读回 5 根，字段正确。"""
        store = _tmp_store(tmp_path)
        candles = _make_candles("BTC", "1H", 5)
        rows = [
            (c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v)
            for c in candles
        ]
        store.upsert_candles(rows)

        result = store.get_candles("BTC", "1H", limit=10)
        assert len(result) == 5
        # 所有返回元素是 Candle
        for c in result:
            assert isinstance(c, Candle)

        # 第一根字段对齐（升序，所以 result[0] 是最旧的）
        first = result[0]
        assert first.coin == "BTC"
        assert first.interval == "1H"
        assert first.open_time_ms == candles[0].open_time_ms
        assert first.o == pytest.approx(candles[0].o)
        assert first.h == pytest.approx(candles[0].h)
        assert first.l == pytest.approx(candles[0].l)
        assert first.c == pytest.approx(candles[0].c)
        assert first.v == pytest.approx(candles[0].v)

    def test_close_time_ms_correct(self, tmp_path):
        """close_time_ms = open_ms + GRANULARITY_MS[tf]。"""
        store = _tmp_store(tmp_path)
        tf = "5m"
        gran_ms = GRANULARITY_MS[tf]
        candles = _make_candles("ETH", tf, 3)
        rows = [(c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v) for c in candles]
        store.upsert_candles(rows)

        result = store.get_candles("ETH", tf, limit=10)
        for c in result:
            assert c.close_time_ms == c.open_time_ms + gran_ms

    def test_ascending_order(self, tmp_path):
        """get_candles 返回结果严格升序（open_time_ms）。"""
        store = _tmp_store(tmp_path)
        candles = _make_candles("SOL", "15m", 10)
        # 故意乱序插入
        shuffled = candles[5:] + candles[:5]
        rows = [(c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v) for c in shuffled]
        store.upsert_candles(rows)

        result = store.get_candles("SOL", "15m", limit=20)
        for i in range(1, len(result)):
            assert result[i].open_time_ms > result[i - 1].open_time_ms

    def test_insert_or_replace_dedup(self, tmp_path):
        """同 coin/tf/open_ms 再写 → INSERT OR REPLACE 覆盖（v 更新）。"""
        store = _tmp_store(tmp_path)
        tf = "1H"
        gran_ms = GRANULARITY_MS[tf]
        ts = 1_700_000_000_000

        # 第一次写
        store.upsert_candles([("BTC", tf, ts, 100.0, 101.0, 99.0, 100.5, 999.0)])
        # 第二次写同 primary key，只改 v
        store.upsert_candles([("BTC", tf, ts, 100.0, 101.0, 99.0, 100.5, 12345.0)])

        result = store.get_candles("BTC", tf, limit=10)
        assert len(result) == 1  # 只有 1 行，不是 2 行
        assert result[0].v == pytest.approx(12345.0)  # v 已更新

    def test_count_candles(self, tmp_path):
        """count_candles 返回正确行数。"""
        store = _tmp_store(tmp_path)
        candles = _make_candles("DOGE", "4H", 7)
        rows = [(c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v) for c in candles]
        store.upsert_candles(rows)

        assert store.count_candles("DOGE", "4H") == 7
        assert store.count_candles("DOGE", "1H") == 0  # 不同 tf
        assert store.count_candles("BTC", "4H") == 0   # 不同 coin

    def test_get_candles_empty(self, tmp_path):
        """不存在的 coin/tf → get_candles 返回 []。"""
        store = _tmp_store(tmp_path)
        result = store.get_candles("NONEXIST", "1m", limit=100)
        assert result == []

    def test_upsert_empty_rows_safe(self, tmp_path):
        """空 rows 输入 upsert_candles 安全返回，不抛异常。"""
        store = _tmp_store(tmp_path)
        store.upsert_candles([])  # 不应 raise
        assert store.count_candles("BTC", "1H") == 0

    def test_get_candles_limit(self, tmp_path):
        """limit 参数有效截断结果（升序，取最新的 limit 根后再升序）。"""
        store = _tmp_store(tmp_path)
        candles = _make_candles("BTC", "1m", 20)
        rows = [(c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v) for c in candles]
        store.upsert_candles(rows)

        result = store.get_candles("BTC", "1m", limit=5)
        assert len(result) == 5
        # 应是最新的 5 根（升序，最后一根 open_time_ms 最大）
        assert result[-1].open_time_ms == candles[-1].open_time_ms

    def test_multi_coin_isolation(self, tmp_path):
        """不同 coin 数据互不干扰。"""
        store = _tmp_store(tmp_path)
        rows_btc = [(c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v)
                    for c in _make_candles("BTC", "1H", 3)]
        rows_eth = [(c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v)
                    for c in _make_candles("ETH", "1H", 5)]
        store.upsert_candles(rows_btc)
        store.upsert_candles(rows_eth)

        assert store.count_candles("BTC", "1H") == 3
        assert store.count_candles("ETH", "1H") == 5
        # BTC 结果不含 ETH 数据
        btc_result = store.get_candles("BTC", "1H", limit=100)
        assert all(c.coin == "BTC" for c in btc_result)


# ============================================================
# Collector 层测试
# ============================================================

class TestBitgetCandleCollector:
    """BitgetCandleCollector.collect_once 行为契约。"""

    @pytest.mark.asyncio
    async def test_collect_once_calls_upsert(self, tmp_path, monkeypatch):
        """collect_once → 调用 bg.klines 并落库，返回总根数。"""
        store = _tmp_store(tmp_path)
        synthetic_candles = _make_candles("BTC", "1H", 10)

        async def fake_klines(self, symbol, granularity, bars=1000, coin=""):
            return synthetic_candles

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        collector = BitgetCandleCollector(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["1H"],
            bars=10,
            store=store,
        )
        total = await collector.collect_once()

        # 返回总写入根数
        assert total == 10
        # DB 里有数据
        assert store.count_candles("BTC", "1H") == 10

    @pytest.mark.asyncio
    async def test_collect_once_multi_coin_tf(self, tmp_path, monkeypatch):
        """多 coin × tf 组合，返回所有根数之和。"""
        store = _tmp_store(tmp_path)

        async def fake_klines(self, symbol, granularity, bars=1000, coin=""):
            return _make_candles(coin, granularity, 5)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        collector = BitgetCandleCollector(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
            timeframes=["1H", "4H"],
            bars=5,
            store=store,
        )
        total = await collector.collect_once()

        # 2 coins × 2 tfs × 5 candles = 20
        assert total == 20
        assert store.count_candles("BTC", "1H") == 5
        assert store.count_candles("ETH", "4H") == 5

    @pytest.mark.asyncio
    async def test_collect_once_exception_swallowed(self, tmp_path, monkeypatch):
        """单 coin/tf 抛异常 → log.warning 吞掉，其它 coin/tf 继续，不崩溃。"""
        store = _tmp_store(tmp_path)
        call_count = 0

        async def fake_klines(self, symbol, granularity, bars=1000, coin=""):
            nonlocal call_count
            call_count += 1
            # BTC 第一次调用时模拟异常
            if symbol == "BTCUSDT":
                raise RuntimeError("模拟 Bitget API 超时")
            return _make_candles(coin, granularity, 3)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        collector = BitgetCandleCollector(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
            timeframes=["1H"],
            bars=3,
            store=store,
        )
        # 不应抛异常
        total = await collector.collect_once()

        # BTC 失败，ETH 成功：总 3 根
        assert total == 3
        assert store.count_candles("BTC", "1H") == 0   # 失败，未写入
        assert store.count_candles("ETH", "1H") == 3   # 成功

    @pytest.mark.asyncio
    async def test_collect_once_empty_candles(self, tmp_path, monkeypatch):
        """klines 返回空列表 → collect_once 返回 0，不崩溃。"""
        store = _tmp_store(tmp_path)

        async def fake_klines(self, symbol, granularity, bars=1000, coin=""):
            return []

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        collector = BitgetCandleCollector(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["1H"],
            bars=100,
            store=store,
        )
        total = await collector.collect_once()
        assert total == 0

    @pytest.mark.asyncio
    async def test_collect_once_dedup_on_rerun(self, tmp_path, monkeypatch):
        """第二次 collect_once 重复插入同 K 线 → INSERT OR REPLACE 不膨胀行数。"""
        store = _tmp_store(tmp_path)
        synthetic = _make_candles("SOL", "15m", 8)

        async def fake_klines(self, symbol, granularity, bars=1000, coin=""):
            return synthetic

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        collector = BitgetCandleCollector(
            coin_to_symbol={"SOL": "SOLUSDT"},
            timeframes=["15m"],
            bars=8,
            store=store,
        )
        await collector.collect_once()
        await collector.collect_once()  # 重复运行

        # 行数不应翻倍
        assert store.count_candles("SOL", "15m") == 8
