"""candle_collector 冷启动优先采集未覆盖币 单元测试（合成数据，不联网）。

覆盖：
  uncovered_symbols：
    - 空 DB → 返回全部 coin（全部未覆盖）
    - 部分 coin 已覆盖 → 只返回缺数据的 coin
    - 全部 coin 已覆盖 → 返回空列表
    - store 有 conn（SQLite 快速路径）正确区分已覆盖/未覆盖
    - store 无 conn 但有 count_candles（duck-type 慢速路径）
    - store 无 conn 也无 count_candles（最后兜底，返回全部）
  collect_symbols：
    - 指定子集采集，正确落库
    - 空列表输入 → 返回 0，不崩溃
    - 单 coin 异常被吞掉，其余继续
  sema_limit 默认值：
    - 新建 collector 时默认 sema_limit=8
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


class _FakeStore:
    """FakeStore：记录 upsert_candles 调用，实现 count_candles duck-type。无 conn。"""

    def __init__(self) -> None:
        self.upserted: list[tuple] = []

    def upsert_candles(self, rows: list[tuple]) -> None:
        self.upserted.extend(rows)

    def count_candles(self, coin: str, tf: str) -> int:
        """逐条计算，用于 duck-type 慢速路径测试。"""
        return sum(1 for r in self.upserted if r[0] == coin and r[1] == tf)


class _NoMethodStore:
    """只有 upsert_candles，无 conn 也无 count_candles（最后兜底场景）。"""

    def upsert_candles(self, rows: list[tuple]) -> None:
        pass


def _make_collector(
    coin_to_symbol: dict[str, str] | None = None,
    store: Any = None,
    sema_limit: int = 8,
) -> BitgetCandleCollector:
    """创建测试用 collector。"""
    return BitgetCandleCollector(
        coin_to_symbol=coin_to_symbol or {"BTC": "BTCUSDT"},
        timeframes=["1H"],
        bars=10,
        store=store or _FakeStore(),
        sema_limit=sema_limit,
    )


# ============================================================
# sema_limit 默认值测试
# ============================================================

class TestSemaLimit:
    """新建 collector 默认 sema_limit=8（Bitget 实证安全并发）。"""

    def test_default_sema_limit_is_8(self):
        """不传 sema_limit 时默认值为 8。"""
        collector = BitgetCandleCollector(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["1H"],
            bars=10,
            store=_FakeStore(),
        )
        assert collector.sema_limit == 8

    def test_explicit_sema_limit_respected(self):
        """显式传入 sema_limit=4 时保留用户指定值。"""
        collector = _make_collector(sema_limit=4)
        assert collector.sema_limit == 4


# ============================================================
# uncovered_symbols — SQLite 快速路径（有 conn）
# ============================================================

class TestUncoveredSymbolsSqlite:
    """uncovered_symbols：store 有 conn（SQLite）场景。"""

    def test_empty_db_returns_all(self, tmp_path):
        """DB 空时所有 coin 均未覆盖，返回完整列表。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")
        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
        collector = _make_collector(coin_to_symbol=coins, store=store)

        result = collector.uncovered_symbols("1H")
        assert len(result) == 3
        returned_coins = {c for c, _ in result}
        assert returned_coins == {"BTC", "ETH", "SOL"}

    def test_partial_coverage_returns_only_missing(self, tmp_path):
        """BTC 和 ETH 已有数据，SOL 没有 → 只返回 SOL。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")

        # 写入 BTC 和 ETH
        for coin in ("BTC", "ETH"):
            rows = [
                (coin, "1H", 1_700_000_000_000 + i * 3_600_000,
                 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000.0)
                for i in range(3)
            ]
            store.upsert_candles(rows)

        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
        collector = _make_collector(coin_to_symbol=coins, store=store)

        result = collector.uncovered_symbols("1H")
        assert len(result) == 1
        assert result[0][0] == "SOL"
        assert result[0][1] == "SOLUSDT"

    def test_full_coverage_returns_empty(self, tmp_path):
        """所有 coin 均有数据 → 返回空列表（稳态时不再新增）。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")

        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
        for coin in coins:
            rows = [
                (coin, "1H", 1_700_000_000_000 + i * 3_600_000,
                 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000.0)
                for i in range(2)
            ]
            store.upsert_candles(rows)

        collector = _make_collector(coin_to_symbol=coins, store=store)
        result = collector.uncovered_symbols("1H")
        assert result == []

    def test_tf_isolation(self, tmp_path):
        """查 4H 的未覆盖：BTC 只有 1H 数据 → 4H 视为未覆盖，返回 BTC。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")

        # 只写 1H
        store.upsert_candles([
            ("BTC", "1H", 1_700_000_000_000, 100.0, 101.0, 99.0, 100.5, 1000.0),
        ])

        coins = {"BTC": "BTCUSDT"}
        collector = _make_collector(coin_to_symbol=coins, store=store)

        assert collector.uncovered_symbols("1H") == []   # 1H 已覆盖
        assert len(collector.uncovered_symbols("4H")) == 1  # 4H 未覆盖

    def test_already_covered_not_in_result(self, tmp_path):
        """已覆盖的 coin 不出现在返回列表中（负向验证）。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")

        store.upsert_candles([
            ("BTC", "1H", 1_700_000_000_000, 100.0, 101.0, 99.0, 100.5, 1000.0),
        ])
        coins = {"BTC": "BTCUSDT", "SOL": "SOLUSDT"}
        collector = _make_collector(coin_to_symbol=coins, store=store)

        result = collector.uncovered_symbols("1H")
        returned_coins = {c for c, _ in result}
        assert "BTC" not in returned_coins, "BTC 已覆盖，不应出现在未覆盖列表"
        assert "SOL" in returned_coins


# ============================================================
# uncovered_symbols — duck-type 兜底路径
# ============================================================

class TestUncoveredSymbolsDuckType:
    """uncovered_symbols：无 conn 的 duck-type 兜底场景。"""

    def test_count_candles_fallback(self):
        """store 有 count_candles 但无 conn → 逐 coin 查询，结果正确。"""
        store = _FakeStore()
        # 手动写入 BTC 数据（模拟 count_candles 可查到）
        store.upsert_candles([
            ("BTC", "1H", 1_700_000_000_000, 100.0, 101.0, 99.0, 100.5, 1000.0),
        ])
        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
        collector = _make_collector(coin_to_symbol=coins, store=store)

        result = collector.uncovered_symbols("1H")
        assert len(result) == 1
        assert result[0][0] == "ETH"

    def test_count_candles_empty_returns_all(self):
        """store 有 count_candles 但无任何数据 → 返回全部 coin。"""
        store = _FakeStore()
        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
        collector = _make_collector(coin_to_symbol=coins, store=store)

        result = collector.uncovered_symbols("1H")
        assert len(result) == 3

    def test_no_conn_no_count_candles_fallback_all(self):
        """store 无 conn 也无 count_candles → 最后兜底，返回全部（不崩溃）。"""
        store = _NoMethodStore()
        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
        collector = _make_collector(coin_to_symbol=coins, store=store)

        result = collector.uncovered_symbols("1H")
        # 兜底：返回全部，不崩溃
        assert len(result) == 2
        returned_coins = {c for c, _ in result}
        assert returned_coins == {"BTC", "ETH"}

    def test_empty_coin_to_symbol_returns_empty(self):
        """coin_to_symbol 为空时，无论 store 如何，返回空列表。"""
        store = _FakeStore()
        # 直接构造 collector，绕过 _make_collector 的 {} 兜底
        collector = BitgetCandleCollector(
            coin_to_symbol={},
            timeframes=["1H"],
            bars=10,
            store=store,
        )
        result = collector.uncovered_symbols("1H")
        assert result == []


# ============================================================
# collect_symbols 采集指定子集
# ============================================================

class TestCollectSymbols:
    """collect_symbols：指定子集采集正确落库。"""

    @pytest.mark.asyncio
    async def test_collect_symbols_basic(self, monkeypatch):
        """指定 2 个 coin，每个 coin 采集 1 个 tf，落库正确。"""
        store = _FakeStore()
        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
        collector = _make_collector(coin_to_symbol=coins, store=store)

        fetched: list[str] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            fetched.append(coin)
            return _make_candles(coin, tf, 5)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        # 只采 BTC 和 ETH（SOL 跳过）
        subset = [("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")]
        n = await collector.collect_symbols(subset)

        assert n == 10  # 2 coins × 5 candles
        assert set(fetched) == {"BTC", "ETH"}
        assert "SOL" not in fetched  # SOL 未被采集

    @pytest.mark.asyncio
    async def test_collect_symbols_empty_subset(self, monkeypatch):
        """空子集 → 立即返回 0，不调用任何 API。"""
        store = _FakeStore()
        collector = _make_collector(store=store)

        called = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            called.append(coin)
            return []

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        n = await collector.collect_symbols([])
        assert n == 0
        assert called == []  # 未调用任何 API

    @pytest.mark.asyncio
    async def test_collect_symbols_exception_swallowed(self, monkeypatch):
        """子集中单 coin 异常被吞掉，其余 coin 继续，不崩溃。"""
        store = _FakeStore()
        coins = {"FAIL": "FAILUSDT", "ETH": "ETHUSDT"}
        collector = _make_collector(coin_to_symbol=coins, store=store)

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            if coin == "FAIL":
                raise RuntimeError("模拟采集失败")
            return _make_candles(coin, tf, 4)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        # 不应抛异常
        n = await collector.collect_symbols([("FAIL", "FAILUSDT"), ("ETH", "ETHUSDT")])
        assert n == 4  # 只有 ETH 成功

    @pytest.mark.asyncio
    async def test_collect_symbols_uses_all_timeframes(self, monkeypatch):
        """每个 coin 会采集所有 timeframes（不只采 probe_tf）。"""
        store = _FakeStore()
        collector = BitgetCandleCollector(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["1H", "4H", "1D"],
            bars=3,
            store=store,
        )

        fetched_tfs: list[str] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            fetched_tfs.append(tf)
            return _make_candles(coin, tf, 3)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        await collector.collect_symbols([("BTC", "BTCUSDT")])

        # 应采集了 3 个 tf
        assert set(fetched_tfs) == {"1H", "4H", "1D"}
        assert len(fetched_tfs) == 3

    @pytest.mark.asyncio
    async def test_collect_symbols_upsert_called(self, monkeypatch):
        """collect_symbols 采集后调用 store.upsert_candles 写入数据。"""
        store = _FakeStore()
        collector = _make_collector(store=store)

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return _make_candles(coin, tf, 6)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        await collector.collect_symbols([("BTC", "BTCUSDT")])
        assert len(store.upserted) == 6  # 6 根已落库


# ============================================================
# 冷启动端到端：uncovered_symbols → collect_symbols 正确驱动新增覆盖
# ============================================================

class TestColdStartEndToEnd:
    """端到端验证：uncovered_symbols + collect_symbols 保证每批新增覆盖。"""

    @pytest.mark.asyncio
    async def test_cold_start_increases_coverage(self, tmp_path, monkeypatch):
        """冷启动时，collect_symbols(uncovered_symbols(tf)) 每批都新增覆盖，
        不浪费在已覆盖的 coin 上。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")

        # 初始：只有 BTC 有 1H 数据（模拟 41% 覆盖）
        store.upsert_candles([
            ("BTC", "1H", 1_700_000_000_000, 100.0, 101.0, 99.0, 100.5, 1000.0),
        ])

        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
        collector = BitgetCandleCollector(
            coin_to_symbol=coins,
            timeframes=["1H"],
            bars=3,
            store=store,
        )

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return _make_candles(coin, tf, 3)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        # 查询未覆盖币
        uncovered = collector.uncovered_symbols("1H")
        assert len(uncovered) == 2  # ETH 和 SOL 未覆盖
        uncovered_coins = {c for c, _ in uncovered}
        assert "BTC" not in uncovered_coins  # BTC 已覆盖，不在列表

        # 采集未覆盖币
        n = await collector.collect_symbols(uncovered)
        assert n == 6  # 2 coins × 3 candles

        # 覆盖度提升
        new_covered = collector.covered_coin_count("1H")
        assert new_covered == 3  # 全覆盖

        # 再查未覆盖：全部已覆盖，返回空列表
        uncovered_after = collector.uncovered_symbols("1H")
        assert uncovered_after == []

    @pytest.mark.asyncio
    async def test_uncovered_symbols_not_containing_already_covered(
        self, tmp_path, monkeypatch
    ):
        """uncovered_symbols 返回结果中绝对不包含已覆盖 coin（关键不变式）。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")

        # 写入多个 coin 的数据
        for coin in ("BTC", "ETH"):
            rows = [
                (coin, "1H", 1_700_000_000_000 + i * 3_600_000,
                 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000.0)
                for i in range(2)
            ]
            store.upsert_candles(rows)

        coins = {
            "BTC": "BTCUSDT", "ETH": "ETHUSDT",
            "SOL": "SOLUSDT", "DOGE": "DOGEUSDT",
        }
        collector = _make_collector(coin_to_symbol=coins, store=store)

        result = collector.uncovered_symbols("1H")
        returned_coins = {c for c, _ in result}

        # 已覆盖的不应出现
        assert "BTC" not in returned_coins
        assert "ETH" not in returned_coins
        # 未覆盖的应出现
        assert "SOL" in returned_coins
        assert "DOGE" in returned_coins
