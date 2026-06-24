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
from smc_tracker.monitor.candle_collector import BitgetCandleCollector, _clean_candles


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


# ============================================================
# _clean_candles 清洗层测试
# ============================================================

class TestCleanCandles:
    """_clean_candles 数据质量守卫：NaN/inf/负价/h<l/ts去重 过滤契约。"""

    def _make_candle(
        self,
        ts: int = 1_700_000_000_000,
        o: float = 100.0,
        h: float = 101.0,
        l: float = 99.0,
        c: float = 100.5,
        v: float = 1000.0,
        coin: str = "BTC",
        tf: str = "1H",
    ) -> Candle:
        """构造单根 Candle（用于清洗测试）。"""
        return Candle(
            coin=coin,
            interval=tf,
            open_time_ms=ts,
            close_time_ms=ts + 3_600_000,
            o=o, h=h, l=l, c=c, v=v, n=0,
        )

    def test_clean_empty_list(self):
        """空列表输入 → 返回空列表，不崩溃。"""
        assert _clean_candles([]) == []

    def test_clean_valid_candles_pass_through(self):
        """全合法 K 线，全部保留。"""
        candles = [
            self._make_candle(ts=1_000_000 + i * 3_600_000)
            for i in range(5)
        ]
        result = _clean_candles(candles)
        assert len(result) == 5

    def test_clean_negative_price_rejected(self):
        """价格 ≤ 0 的行被过滤。"""
        candles = [
            self._make_candle(ts=1_000_000, o=-1.0),    # o<0
            self._make_candle(ts=2_000_000, h=0.0),     # h=0
            self._make_candle(ts=3_000_000),              # 合法
        ]
        result = _clean_candles(candles)
        assert len(result) == 1
        assert result[0].open_time_ms == 3_000_000

    def test_clean_h_lt_l_rejected(self):
        """h < l 的行被过滤（价格逻辑非法）。"""
        candles = [
            self._make_candle(ts=1_000_000, h=99.0, l=101.0),  # h < l
            self._make_candle(ts=2_000_000),                     # 合法
        ]
        result = _clean_candles(candles)
        assert len(result) == 1
        assert result[0].open_time_ms == 2_000_000

    def test_clean_duplicate_ts_keeps_last(self):
        """同 open_time_ms 的多行：保留后者（覆盖语义，REST 最新值更可信）。"""
        ts = 1_000_000
        c1 = self._make_candle(ts=ts, v=111.0)
        c2 = self._make_candle(ts=ts, v=222.0)  # 后者（更新值）
        result = _clean_candles([c1, c2])
        assert len(result) == 1
        assert result[0].v == pytest.approx(222.0)

    def test_clean_output_ascending_order(self):
        """清洗后输出按 open_time_ms 严格升序。"""
        candles = [
            self._make_candle(ts=3_000_000),
            self._make_candle(ts=1_000_000),
            self._make_candle(ts=2_000_000),
        ]
        result = _clean_candles(candles)
        assert len(result) == 3
        for i in range(1, len(result)):
            assert result[i].open_time_ms > result[i - 1].open_time_ms

    def test_clean_nan_price_rejected(self):
        """NaN 价格（to_float 转 0.0）→ 被过滤（o=NaN 等价 o<=0）。"""
        import math
        c_nan = self._make_candle(ts=1_000_000, o=float("nan"))
        c_ok  = self._make_candle(ts=2_000_000)
        result = _clean_candles([c_nan, c_ok])
        assert len(result) == 1
        assert result[0].open_time_ms == 2_000_000

    def test_clean_all_dirty_returns_empty(self):
        """全脏行 → 返回空列表。"""
        candles = [
            self._make_candle(ts=1_000_000, o=-1.0),
            self._make_candle(ts=2_000_000, h=0.0),
            self._make_candle(ts=3_000_000, h=50.0, l=100.0),  # h < l
        ]
        result = _clean_candles(candles)
        assert result == []

    def test_clean_zero_volume_allowed(self):
        """v=0 的行允许通过（成交量为 0 是合法的低流动性状态）。"""
        candle = self._make_candle(ts=1_000_000, v=0.0)
        result = _clean_candles([candle])
        assert len(result) == 1

    def test_clean_inf_price_rejected(self):
        """inf 价格 → 被 to_float 转 0.0，过滤。"""
        c_inf = self._make_candle(ts=1_000_000, o=float("inf"))
        c_ok  = self._make_candle(ts=2_000_000)
        result = _clean_candles([c_inf, c_ok])
        assert len(result) == 1


# ============================================================
# collect_batch 增量轮转测试
# ============================================================

class _FakeStore:
    """FakeStore：只记录 upsert_candles 调用（用于 collect_batch 测试）。"""
    def __init__(self) -> None:
        self.upserted: list[tuple] = []

    def upsert_candles(self, rows) -> None:
        self.upserted.extend(rows)


class TestCollectBatch:
    """BitgetCandleCollector.collect_batch 增量轮转采集契约。"""

    def _make_collector(
        self,
        coin_to_symbol: dict,
        store: _FakeStore | None = None,
    ) -> BitgetCandleCollector:
        return BitgetCandleCollector(
            coin_to_symbol=coin_to_symbol,
            timeframes=["1H"],
            bars=5,
            store=store or _FakeStore(),
        )

    @pytest.mark.asyncio
    async def test_collect_batch_basic_offset_advance(self, monkeypatch):
        """collect_batch(offset=0, batch_size=2)：采集 coins[0:2]，返回 offset=2。"""
        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
        fetched_coins: list[str] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            fetched_coins.append(coin)
            return _make_candles(coin, tf, 5)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        store = _FakeStore()
        collector = self._make_collector(coins, store)
        next_offset = await collector.collect_batch(0, 2)

        assert next_offset == 2
        assert len(set(fetched_coins)) == 2  # 只采了 2 个币

    @pytest.mark.asyncio
    async def test_collect_batch_wraparound(self, monkeypatch):
        """offset 接近末尾时环绕到列表头部。"""
        coins = {"A": "AUSDT", "B": "BUSDT", "C": "CUSDT"}
        fetched_coins: list[str] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            fetched_coins.append(coin)
            return _make_candles(coin, tf, 3)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        store = _FakeStore()
        collector = self._make_collector(coins, store)
        # offset=2, batch_size=2 → 取 coins[2] + coins[0]（环绕）
        next_offset = await collector.collect_batch(2, 2)

        # 环绕后 next_offset = (2+2)%3 = 1
        assert next_offset == 1
        # 采集了 2 个币（coins[2] + coins[0] 环绕）
        assert len(set(fetched_coins)) == 2

    @pytest.mark.asyncio
    async def test_collect_batch_empty_coins_returns_zero(self, monkeypatch):
        """coin_to_symbol 为空时，返回 0，不崩溃。"""
        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return []

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        store = _FakeStore()
        collector = self._make_collector({}, store)
        next_offset = await collector.collect_batch(0, 10)
        assert next_offset == 0

    @pytest.mark.asyncio
    async def test_collect_batch_upsert_called(self, monkeypatch):
        """collect_batch 落库前调用 store.upsert_candles（数据已写入）。"""
        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return _make_candles(coin, tf, 4)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        store = _FakeStore()
        collector = self._make_collector(coins, store)
        await collector.collect_batch(0, 2)

        assert len(store.upserted) > 0  # 已有数据落库

    @pytest.mark.asyncio
    async def test_collect_batch_exception_swallowed(self, monkeypatch):
        """单 coin 异常被吞掉，其余 coin 继续，不崩溃。"""
        coins = {"FAIL": "FAILUSDT", "ETH": "ETHUSDT"}
        fetched: list[str] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            fetched.append(coin)
            if coin == "FAIL":
                raise RuntimeError("模拟异常")
            return _make_candles(coin, tf, 3)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        store = _FakeStore()
        collector = self._make_collector(coins, store)
        # 不应抛异常
        next_offset = await collector.collect_batch(0, 2)
        assert next_offset >= 0  # 完成，返回有效 offset

    @pytest.mark.asyncio
    async def test_collect_batch_clean_dirty_rows(self, monkeypatch):
        """脏行（负价）经 _clean_candles 过滤后不进 DB。"""
        from smc_tracker.bitget.rest import GRANULARITY_MS as _GM
        gran_ms = _GM["1H"]

        def _make_mixed() -> list[Candle]:
            """返回混合行：1 根脏（o=-1）+ 3 根合法。"""
            base_ms = 1_700_000_000_000
            out = []
            # 脏行
            out.append(Candle(
                coin="BTC", interval="1H",
                open_time_ms=base_ms,
                close_time_ms=base_ms + gran_ms,
                o=-1.0, h=101.0, l=99.0, c=100.0, v=1000.0, n=0,
            ))
            # 合法行
            for i in range(1, 4):
                ts = base_ms + i * gran_ms
                out.append(Candle(
                    coin="BTC", interval="1H",
                    open_time_ms=ts,
                    close_time_ms=ts + gran_ms,
                    o=100.0 + i, h=102.0 + i, l=98.0 + i, c=101.0 + i,
                    v=1000.0 + i, n=0,
                ))
            return out

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return _make_mixed()

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        store = _FakeStore()
        collector = self._make_collector({"BTC": "BTCUSDT"}, store)
        await collector.collect_batch(0, 1)

        # 脏行（o=-1）被过滤，仅 3 合法行落库
        assert len(store.upserted) == 3

    @pytest.mark.asyncio
    async def test_collect_once_also_uses_clean(self, monkeypatch):
        """collect_once 也经过 _clean_candles：脏行不落库。"""
        from smc_tracker.bitget.rest import GRANULARITY_MS as _GM
        gran_ms = _GM["1H"]
        base_ms = 1_700_000_000_000

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return [
                Candle(coin=coin, interval=tf,
                       open_time_ms=base_ms, close_time_ms=base_ms + gran_ms,
                       o=0.0, h=101.0, l=99.0, c=100.0, v=1000.0, n=0),  # 脏（o=0）
                Candle(coin=coin, interval=tf,
                       open_time_ms=base_ms + gran_ms, close_time_ms=base_ms + 2 * gran_ms,
                       o=100.0, h=101.0, l=99.0, c=100.5, v=1000.0, n=0),  # 合法
            ]

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        from pathlib import Path
        import tempfile
        from smc_tracker.storage.db import Store as _Store
        tmp = Path(tempfile.mkdtemp()) / "t.db"
        store = _Store(tmp)
        collector = BitgetCandleCollector(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["1H"],
            bars=10,
            store=store,
        )
        total = await collector.collect_once()
        # 只有 1 根合法
        assert total == 1
        assert store.count_candles("BTC", "1H") == 1
