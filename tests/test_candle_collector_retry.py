"""candle_collector 超时重试 + 冷启动加速 单元测试（合成数据，不联网）。

覆盖：
  重试逻辑：
    - TimeoutError → 以 retry_bars 重试一次，重试成功后落库
    - TimeoutError → 重试仍失败 → 跳过，不无限重试（返回 0）
    - 非超时异常 → 直接跳过（与原有行为一致）
    - 首次成功 → 不触发重试路径（正常落库）
  冷启动检测：
    - covered_coin_count：DB 有数据时返回正确去重 coin 数
    - covered_coin_count：DB 为空时返回 0
    - covered_coin_count：duck-type 兜底（无 conn 属性时逐 coin 查询）
    - covered_coin_count：store 无任何方法时静默返回 0（不崩溃）
  retry_bars 缩减保证：
    - retry_bars 实际使用值 = min(retry_bars, bars)
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
from smc_tracker.monitor.candle_collector import (
    BitgetCandleCollector,
    _RETRY_DELAY_S,
)


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
    """FakeStore：记录 upsert_candles 调用，不操作真实 DB。"""

    def __init__(self) -> None:
        self.upserted: list[tuple] = []

    def upsert_candles(self, rows: list[tuple]) -> None:
        self.upserted.extend(rows)

    def count_candles(self, coin: str, tf: str) -> int:
        """逐条统计（供 covered_coin_count duck-type 兜底测试）。"""
        return sum(1 for r in self.upserted if r[0] == coin and r[1] == tf)


def _make_collector(
    coin_to_symbol: dict[str, str] | None = None,
    store: Any = None,
    bars: int = 500,
    retry_bars: int = 100,
    sema_limit: int = 4,
) -> BitgetCandleCollector:
    """创建测试用 BitgetCandleCollector，默认使用 _FakeStore。"""
    return BitgetCandleCollector(
        coin_to_symbol=coin_to_symbol or {"BTC": "BTCUSDT"},
        timeframes=["1H"],
        bars=bars,
        store=store or _FakeStore(),
        sema_limit=sema_limit,
        retry_bars=retry_bars,
    )


# ============================================================
# 重试逻辑测试
# ============================================================

class TestRetryOnTimeout:
    """_fetch_one 超时重试：首次 TimeoutError → 降级 bars 重试一次。"""

    @pytest.mark.asyncio
    async def test_timeout_then_success_on_retry(self, monkeypatch):
        """首次 TimeoutError，重试以 retry_bars 成功 → 落库，返回 > 0。"""
        store = _FakeStore()
        collector = _make_collector(store=store, bars=500, retry_bars=100)
        call_count = 0
        used_bars: list[int] = []

        async def fake_klines(self_bg, symbol, granularity, bars=1000, coin=""):
            nonlocal call_count
            call_count += 1
            used_bars.append(bars)
            if call_count == 1:
                raise asyncio.TimeoutError()
            return _make_candles(coin, granularity, 5)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)
        # 跳过真实 sleep（加速测试）：Python 3.14 移除了 asyncio.coroutine，
        # 直接传 async def 替换即可。
        async def _noop_sleep(_: float) -> None:
            pass
        monkeypatch.setattr(
            "smc_tracker.monitor.candle_collector.asyncio.sleep",
            _noop_sleep,
        )

        sema = asyncio.Semaphore(4)
        async with BitgetREST() as bg:
            n = await collector._fetch_one(bg, sema, "BTC", "BTCUSDT", "1H")

        assert n == 5, f"期望 5 根落库，实际 {n}"
        assert call_count == 2, f"期望 2 次调用（首次超时+重试），实际 {call_count}"
        assert used_bars[1] == 100, f"重试应使用 retry_bars=100，实际 {used_bars[1]}"
        assert len(store.upserted) == 5

    @pytest.mark.asyncio
    async def test_timeout_retry_also_fails_returns_zero(self, monkeypatch):
        """首次 + 重试均超时 → 跳过（返回 0），不无限重试。"""
        store = _FakeStore()
        collector = _make_collector(store=store, bars=500, retry_bars=100)
        call_count = 0

        async def fake_klines(self_bg, symbol, granularity, bars=1000, coin=""):
            nonlocal call_count
            call_count += 1
            raise asyncio.TimeoutError()

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        async def _noop_sleep2(_: float) -> None:
            pass
        monkeypatch.setattr(
            "smc_tracker.monitor.candle_collector.asyncio.sleep",
            _noop_sleep2,
        )

        sema = asyncio.Semaphore(4)
        async with BitgetREST() as bg:
            n = await collector._fetch_one(bg, sema, "BTC", "BTCUSDT", "1H")

        assert n == 0, "双重失败应返回 0"
        assert call_count == 2, f"严格 2 次调用（首次超时+重试1次），实际 {call_count}"
        assert len(store.upserted) == 0, "双重失败不应落库任何数据"

    @pytest.mark.asyncio
    async def test_non_timeout_exception_skips_retry(self, monkeypatch):
        """非超时异常（RuntimeError）→ 直接跳过，不触发重试，返回 0。"""
        store = _FakeStore()
        collector = _make_collector(store=store)
        call_count = 0

        async def fake_klines(self_bg, symbol, granularity, bars=1000, coin=""):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("模拟 API 错误")

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        sema = asyncio.Semaphore(4)
        async with BitgetREST() as bg:
            n = await collector._fetch_one(bg, sema, "BTC", "BTCUSDT", "1H")

        assert n == 0
        assert call_count == 1, f"非超时异常只应调用 1 次，实际 {call_count}"
        assert len(store.upserted) == 0

    @pytest.mark.asyncio
    async def test_success_on_first_call_no_retry(self, monkeypatch):
        """首次成功 → 不触发重试，正常落库，call_count=1。"""
        store = _FakeStore()
        collector = _make_collector(store=store, bars=500, retry_bars=100)
        call_count = 0

        async def fake_klines(self_bg, symbol, granularity, bars=1000, coin=""):
            nonlocal call_count
            call_count += 1
            return _make_candles(coin, granularity, 8)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        sema = asyncio.Semaphore(4)
        async with BitgetREST() as bg:
            n = await collector._fetch_one(bg, sema, "BTC", "BTCUSDT", "1H")

        assert n == 8
        assert call_count == 1, f"首次成功不应重试，实际调用 {call_count} 次"
        assert len(store.upserted) == 8

    @pytest.mark.asyncio
    async def test_timeout_retry_uses_retry_bars_not_original(self, monkeypatch):
        """重试时传入的 bars = retry_bars（100），不是原 bars（500）。"""
        store = _FakeStore()
        collector = _make_collector(store=store, bars=500, retry_bars=100)
        retry_bars_used: list[int] = []

        async def fake_klines(self_bg, symbol, granularity, bars=1000, coin=""):
            if not retry_bars_used:
                raise asyncio.TimeoutError()
            retry_bars_used.append(bars)
            return _make_candles(coin, granularity, 3)

        # 需要记录第二次调用的 bars
        call_seq: list[int] = []

        async def fake_klines2(self_bg, symbol, granularity, bars=1000, coin=""):
            call_seq.append(bars)
            if len(call_seq) == 1:
                raise asyncio.TimeoutError()
            return _make_candles(coin, granularity, 3)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines2)

        async def _noop_sleep3(_: float) -> None:
            pass
        monkeypatch.setattr(
            "smc_tracker.monitor.candle_collector.asyncio.sleep",
            _noop_sleep3,
        )

        sema = asyncio.Semaphore(4)
        async with BitgetREST() as bg:
            n = await collector._fetch_one(bg, sema, "BTC", "BTCUSDT", "1H")

        assert len(call_seq) == 2
        assert call_seq[0] == 500, f"首次调用应用 bars=500，实际 {call_seq[0]}"
        assert call_seq[1] == 100, f"重试应用 retry_bars=100，实际 {call_seq[1]}"
        assert n == 3

    @pytest.mark.asyncio
    async def test_collect_batch_with_mixed_timeout_and_success(self, monkeypatch):
        """collect_batch 多 coin：部分超时（最终重试成功）、部分正常 → 批次完整完成，不崩溃。"""
        coins = {"TSLA": "TSLAUSDT", "BTC": "BTCUSDT", "ETH": "ETHUSDT"}
        store = _FakeStore()
        collector = BitgetCandleCollector(
            coin_to_symbol=coins,
            timeframes=["1H"],
            bars=500,
            store=store,
            sema_limit=4,
            retry_bars=100,
        )
        call_log: dict[str, int] = {}

        async def fake_klines(self_bg, symbol, granularity, bars=1000, coin=""):
            call_log[coin] = call_log.get(coin, 0) + 1
            if coin == "TSLA" and call_log[coin] == 1:
                raise asyncio.TimeoutError()
            # 重试或其它 coin → 正常返回
            return _make_candles(coin, granularity, 4)

        monkeypatch.setattr(BitgetREST, "klines", fake_klines)

        async def _noop_sleep4(_: float) -> None:
            pass
        monkeypatch.setattr(
            "smc_tracker.monitor.candle_collector.asyncio.sleep",
            _noop_sleep4,
        )

        next_offset = await collector.collect_batch(0, 3)

        assert next_offset >= 0  # 正常完成
        # TSLA 经历超时→重试→成功，BTC/ETH 直接成功
        assert call_log.get("TSLA", 0) == 2, "TSLA 应被调用 2 次（首次超时+重试）"
        assert call_log.get("BTC", 0) == 1, "BTC 应被调用 1 次（直接成功）"
        assert call_log.get("ETH", 0) == 1, "ETH 应被调用 1 次（直接成功）"
        # 全部数据应已落库（TSLA 重试成功）
        assert len(store.upserted) == 12  # 3 coins × 4 candles


# ============================================================
# 冷启动检测测试
# ============================================================

class TestCoveredCoinCount:
    """covered_coin_count：DB 覆盖度检测（冷启动 vs 稳态）。"""

    def test_covered_coin_count_empty_db(self, tmp_path):
        """DB 为空 → covered_coin_count 返回 0。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")
        collector = _make_collector(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
            store=store,
        )
        assert collector.covered_coin_count("1H") == 0

    def test_covered_coin_count_with_data(self, tmp_path):
        """部分 coin 有数据 → 返回有数据的去重 coin 数。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")

        # 写入 BTC 和 ETH 的 1H K 线，SOL 没有
        btc_rows = [
            ("BTC", "1H", 1_700_000_000_000 + i * 3_600_000,
             100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000.0)
            for i in range(3)
        ]
        eth_rows = [
            ("ETH", "1H", 1_700_000_000_000 + i * 3_600_000,
             50.0 + i, 51.0 + i, 49.0 + i, 50.5 + i, 500.0)
            for i in range(2)
        ]
        store.upsert_candles(btc_rows)
        store.upsert_candles(eth_rows)

        collector = _make_collector(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"},
            store=store,
        )
        # BTC + ETH 有数据，SOL 没有 → 2
        assert collector.covered_coin_count("1H") == 2

    def test_covered_coin_count_tf_isolation(self, tmp_path):
        """不同 tf 数据互不干扰：查 4H 时，1H 数据不计入。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")

        # 只写 1H 数据
        rows = [
            ("BTC", "1H", 1_700_000_000_000 + i * 3_600_000,
             100.0, 101.0, 99.0, 100.5, 1000.0)
            for i in range(3)
        ]
        store.upsert_candles(rows)

        collector = _make_collector(
            coin_to_symbol={"BTC": "BTCUSDT"},
            store=store,
        )
        assert collector.covered_coin_count("1H") == 1   # 1H 有数据
        assert collector.covered_coin_count("4H") == 0   # 4H 无数据

    def test_covered_coin_count_duck_type_fallback(self):
        """无 conn 属性的 store → duck-type 兜底（count_candles 逐 coin 查询）。"""
        store = _FakeStore()
        # 手动写入一些合成数据（模拟已落库）
        store.upsert_candles([
            ("BTC", "1H", 1_700_000_000_000, 100.0, 101.0, 99.0, 100.5, 1000.0),
            ("ETH", "1H", 1_700_000_000_000, 50.0, 51.0, 49.0, 50.5, 500.0),
        ])
        collector = _make_collector(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"},
            store=store,
        )
        # BTC + ETH 各有 1 条数据，SOL 没有 → 2
        count = collector.covered_coin_count("1H")
        assert count == 2

    def test_covered_coin_count_no_method_returns_zero(self):
        """store 无 conn 也无 count_candles → 静默返回 0（不崩溃）。"""

        class _EmptyStore:
            """空 store，没有任何相关方法。"""
            def upsert_candles(self, rows):
                pass

        store = _EmptyStore()
        collector = _make_collector(store=store)
        # 不应抛异常，静默返回 0
        count = collector.covered_coin_count("1H")
        assert count == 0

    def test_covered_coin_count_all_coins_covered(self, tmp_path):
        """所有 coin 均有数据 → 返回总 coin 数（满覆盖，稳态条件）。"""
        from smc_tracker.storage.db import Store
        store = Store(path=tmp_path / "test.db")
        coins = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

        for coin in coins:
            rows = [
                (coin, "1H", 1_700_000_000_000 + i * 3_600_000,
                 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000.0)
                for i in range(2)
            ]
            store.upsert_candles(rows)

        collector = _make_collector(coin_to_symbol=coins, store=store)
        assert collector.covered_coin_count("1H") == 3  # 所有 3 coin 均有数据


# ============================================================
# retry_bars 缩减保证
# ============================================================

class TestRetryBarsClamp:
    """retry_bars 实际值 = min(retry_bars, bars)，不超过原 bars。"""

    def test_retry_bars_clamped_when_larger_than_bars(self):
        """retry_bars > bars → 被 clamp 到 bars（无缩减意义时不超出）。"""
        collector = _make_collector(bars=50, retry_bars=200)
        assert collector.retry_bars == 50  # min(200, 50) = 50

    def test_retry_bars_kept_when_smaller(self):
        """retry_bars < bars → 保持原值（正常缩减场景）。"""
        collector = _make_collector(bars=500, retry_bars=100)
        assert collector.retry_bars == 100

    def test_retry_bars_equal_to_bars(self):
        """retry_bars == bars → 无缩减（保留，重试时同参数）。"""
        collector = _make_collector(bars=100, retry_bars=100)
        assert collector.retry_bars == 100
