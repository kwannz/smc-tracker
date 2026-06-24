"""BitgetBBMonitor 单元测试（注入合成数据，不联网）。

覆盖：
  - render 卡片含「布林带多周期」、币名、「压力」/「支撑」、共识档位
  - 价格格式无科学计数（无 'e+'）
  - 空 rows → render 返回 None
  - rows 正确按 |consensus_pct-50| 降序排列
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.bitget_bb_monitor import BitgetBBMonitor


# ---- 合成 rows 工厂 ----

def _make_row(
    coin: str,
    symbol: str,
    price: float,
    consensus_pct: int,
    lean_label: str,
    bull_n: int,
    bear_n: int,
    squeeze_n: int = 0,
) -> dict:
    """构造 render 需要的 row 结构（模拟 refresh 返回值）。"""
    # 模拟每个 TF 的 analyze_tf 结果
    tfs: dict = {}
    total = bull_n + bear_n
    for i in range(bull_n):
        tf = f"bull_tf_{i}"
        tfs[tf] = {
            "upper": price * 1.05,
            "mid":   price * 1.01,
            "lower": price * 0.96,
            "price": price,
            "pct_b": 0.75,
            "bandwidth": 0.08,
            "squeeze": (i < squeeze_n),
            "pos_label": "中轨上偏多",
            "bull": True,
        }
    for i in range(bear_n):
        tf = f"bear_tf_{i}"
        tfs[tf] = {
            "upper": price * 1.04,
            "mid":   price * 1.03,
            "lower": price * 1.01,
            "price": price,
            "pct_b": 0.15,
            "bandwidth": 0.03,
            "squeeze": False,
            "pos_label": "逼近支撑",
            "bull": False,
        }
    agg = {
        "bull_n": bull_n,
        "bear_n": bear_n,
        "total": total,
        "consensus_pct": consensus_pct,
        "lean_label": lean_label,
        "squeeze_n": squeeze_n,
    }
    return {"coin": coin, "symbol": symbol, "price": price, "tfs": tfs, "agg": agg}


# ---- 渲染测试 ----

def test_render_contains_header():
    """卡片首行包含「布林带多周期」。"""
    mon = BitgetBBMonitor(
        coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
        timeframes=["5m", "1H", "4H"],
        bars=500, period=20, k=2.0, top_n=10,
    )
    rows = [
        _make_row("BTC", "BTCUSDT", 62538.4, 71, "偏多", 5, 2),
        _make_row("ETH", "ETHUSDT", 3100.0, 40, "分歧", 2, 3),
    ]
    card = mon.render(rows, now_ms=1_700_000_000_000)
    assert card is not None
    assert "布林带多周期" in card


def test_render_contains_coin_names():
    """卡片包含所有传入的币名。"""
    mon = BitgetBBMonitor(
        coin_to_symbol={}, timeframes=["1H"], bars=500, period=20, k=2.0, top_n=10,
    )
    rows = [
        _make_row("BTC", "BTCUSDT", 62538.4, 80, "偏多", 4, 1),
        _make_row("ETH", "ETHUSDT", 3100.0, 20, "偏空", 1, 4),
        _make_row("SOL", "SOLUSDT", 180.0, 60, "偏多", 3, 2),
    ]
    card = mon.render(rows, now_ms=1_700_000_000_000)
    assert card is not None
    assert "BTC" in card
    assert "ETH" in card
    assert "SOL" in card


def test_render_contains_pressure_support():
    """卡片关键位 section 含「压力」或「支撑」字样。"""
    mon = BitgetBBMonitor(
        coin_to_symbol={}, timeframes=["1H", "4H"], bars=500, period=20, k=2.0, top_n=10,
    )
    rows = [
        _make_row("BTC", "BTCUSDT", 62538.4, 71, "偏多", 5, 2),
    ]
    card = mon.render(rows, now_ms=1_700_000_000_000)
    assert card is not None
    assert "压力" in card or "支撑" in card


def test_render_no_scientific_notation():
    """价格格式无科学计数法（无 'e+' 或 'E+'）。"""
    mon = BitgetBBMonitor(
        coin_to_symbol={}, timeframes=["1H"], bars=500, period=20, k=2.0, top_n=10,
    )
    rows = [
        _make_row("BTC", "BTCUSDT", 62538.4, 71, "偏多", 5, 2),
        _make_row("SHIB", "SHIBUSDT", 0.0000234, 40, "分歧", 2, 2),
    ]
    card = mon.render(rows, now_ms=1_700_000_000_000)
    assert card is not None
    assert "e+" not in card.lower()
    assert "e-" not in card.lower()


def test_render_empty_rows():
    """空 rows → render 返回 None。"""
    mon = BitgetBBMonitor(
        coin_to_symbol={}, timeframes=["1H"], bars=500, period=20, k=2.0, top_n=10,
    )
    assert mon.render([], now_ms=1_700_000_000_000) is None


def test_render_contains_consensus_label():
    """卡片包含共识档位标签（偏多/偏空/净多/净空/分歧）。"""
    mon = BitgetBBMonitor(
        coin_to_symbol={}, timeframes=["1H"], bars=500, period=20, k=2.0, top_n=10,
    )
    rows = [
        _make_row("BTC", "BTCUSDT", 62538.4, 71, "偏多", 5, 2),
    ]
    card = mon.render(rows, now_ms=1_700_000_000_000)
    assert card is not None
    # 共识卡片区
    consensus_labels = ["净多", "偏多", "分歧", "偏空", "净空"]
    assert any(lbl in card for lbl in consensus_labels)


def test_render_squeeze_annotation(monkeypatch):
    """有挤压周期时，卡片中应有挤压相关标注（⚠ 或 squeeze 字样）。"""
    mon = BitgetBBMonitor(
        coin_to_symbol={}, timeframes=["1H", "4H"], bars=500, period=20, k=2.0, top_n=10,
    )
    rows = [
        _make_row("BTC", "BTCUSDT", 62538.4, 80, "偏多", 4, 1, squeeze_n=2),
    ]
    card = mon.render(rows, now_ms=1_700_000_000_000)
    assert card is not None
    assert "挤压" in card or "⚠" in card or "squeeze" in card.lower()


def test_render_sort_by_consensus_strength():
    """rows 已排序（|consensus_pct-50| 降序），render 按给定顺序输出（共识最强排前）。"""
    mon = BitgetBBMonitor(
        coin_to_symbol={}, timeframes=["1H"], bars=500, period=20, k=2.0, top_n=10,
    )
    # BTC 偏差=|90-50|=40, ETH 偏差=|55-50|=5, SOL 偏差=|10-50|=40
    rows_sorted = [
        _make_row("BTC", "BTCUSDT", 62538.4, 90, "净多",  5, 0),  # 40
        _make_row("SOL", "SOLUSDT", 180.0,  10, "净空",  0, 5),   # 40
        _make_row("ETH", "ETHUSDT", 3100.0, 55, "分歧",  3, 2),   # 5
    ]
    card = mon.render(rows_sorted, now_ms=1_700_000_000_000)
    assert card is not None
    # BTC 和 SOL 应出现在 ETH 之前（按顺序渲染）
    idx_btc = card.find("BTC")
    idx_eth = card.find("ETH")
    idx_sol = card.find("SOL")
    assert idx_btc < idx_eth, "BTC（共识强）应在 ETH（共识弱）之前"
    assert idx_sol < idx_eth, "SOL（共识强）应在 ETH（共识弱）之前"


def test_render_price_formatted():
    """BTC 价格显示含千分位小数，不是整数也不是科学计数。"""
    mon = BitgetBBMonitor(
        coin_to_symbol={}, timeframes=["1H"], bars=500, period=20, k=2.0, top_n=10,
    )
    rows = [
        _make_row("BTC", "BTCUSDT", 62538.4, 71, "偏多", 5, 2),
    ]
    card = mon.render(rows, now_ms=1_700_000_000_000)
    assert card is not None
    # 62,538.40 格式
    assert "62,538" in card


# ── TDD: DB 优先 / live 回退 / store=None 三模式 ────────────────────────────────


def _make_candles_bb(n: int, coin: str = "BTC", tf: str = "1H") -> list:
    """构造 n 根合成 Candle（满足 BB period+1 最小窗口要求）。"""
    from smc_tracker.models import Candle
    step_ms = 3600 * 1000  # 1H = 3600000 ms
    base_ms = 1_700_000_000_000
    result: list[Candle] = []
    for i in range(n):
        o = 60000.0 + (i % 7) * 300.0
        h = o + 200.0
        l = o - 200.0
        c = o + 50.0
        result.append(Candle(
            coin=coin, interval=tf,
            open_time_ms=base_ms + i * step_ms,
            close_time_ms=base_ms + (i + 1) * step_ms,
            o=o, h=h, l=l, c=c, v=2.0, n=0,
        ))
    return result


class _FakeStoreBB:
    """FakeStore：满足 get_candles/count_candles/upsert_candles 契约（BB 测试用）。"""

    def __init__(self, candles: list, /) -> None:
        self._candles = candles
        self.upserted: list[tuple] = []

    def get_candles(self, coin: str, tf: str, limit: int = 1000) -> list:
        return self._candles[:limit]

    def count_candles(self, coin: str, tf: str) -> int:
        return len(self._candles)

    def upsert_candles(self, rows) -> None:
        self.upserted.extend(rows)


class TestBitgetBBMonitorDBFetch:
    """BitgetBBMonitor DB 优先 / live 回退 / store=None 三模式 TDD 测试。"""

    def setup_method(self) -> None:
        self.period = 20
        self.bars = 100
        # need_min = period+1 = 21；合成 50 根（足够）
        self._enough_candles = _make_candles_bb(50)

    def _make_monitor(self, store=None) -> BitgetBBMonitor:
        return BitgetBBMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["1H"],
            bars=self.bars,
            period=self.period,
            k=2.0,
            top_n=5,
            store=store,
        )

    def test_store_attribute_exists_default_none(self) -> None:
        """store 参数默认 None，无 store 时向后兼容。"""
        mon = self._make_monitor()
        assert mon.store is None

    def test_store_attribute_stored(self) -> None:
        """store 参数正确存储为属性。"""
        fake = _FakeStoreBB(self._enough_candles)
        mon = self._make_monitor(store=fake)
        assert mon.store is fake

    def test_db_hit_does_not_call_live_klines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB 数据足够时，live bg.klines 调用次数应为 0。"""
        live_calls: list[tuple] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            live_calls.append((symbol, tf))
            return []

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        fake_store = _FakeStoreBB(self._enough_candles)
        mon = self._make_monitor(store=fake_store)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        assert len(live_calls) == 0, (
            f"DB 命中时不应调用 live klines，实际调用 {len(live_calls)} 次: {live_calls}"
        )

    def test_db_insufficient_falls_back_to_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB 不足（< period+1）时，应回退 live klines（调用次数 = 1）。"""
        live_calls: list[tuple] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            live_calls.append((symbol, tf))
            return _make_candles_bb(50)

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        # 只给 5 根（< need_min=21），强制回退
        fake_store = _FakeStoreBB(_make_candles_bb(5))
        mon = self._make_monitor(store=fake_store)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        assert len(live_calls) == 1, (
            f"DB 不足应调用 live klines 1 次，实际 {len(live_calls)} 次"
        )

    def test_db_insufficient_upserts_live_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB 不足回退 live 后，live 数据应被 upsert 回填到 DB（自愈）。"""
        live_data = _make_candles_bb(50)

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return live_data

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        fake_store = _FakeStoreBB(_make_candles_bb(5))
        mon = self._make_monitor(store=fake_store)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        assert len(fake_store.upserted) > 0, (
            "DB 不足回退 live 后，应 upsert 回填数据，但 upsert_candles 未被调用"
        )
        first = fake_store.upserted[0]
        assert len(first) == 8, f"upsert 行应为 8 列 (coin,tf,open_ms,o,h,l,c,v)，实际 {len(first)} 列"

    def test_store_none_calls_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """store=None（纯 live 模式）时，bg.klines 被正常调用（向后兼容）。"""
        live_calls: list[tuple] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            live_calls.append((symbol, tf))
            return []

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        mon = self._make_monitor(store=None)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        assert len(live_calls) == 1, (
            f"store=None 时应调用 live klines 1 次，实际 {len(live_calls)} 次"
        )

    def test_db_hit_upsert_not_called(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB 命中时（足够根数），upsert_candles 不应被调用。"""
        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return []

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        fake_store = _FakeStoreBB(self._enough_candles)
        mon = self._make_monitor(store=fake_store)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        assert len(fake_store.upserted) == 0, (
            f"DB 命中时不应调用 upsert_candles，实际 upserted {len(fake_store.upserted)} 行"
        )
