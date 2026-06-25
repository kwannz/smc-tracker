"""bitget_candles 滚动保留单测：每 (coin,tf) 保留最新 max_bars 根，删更旧。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store


def _store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "t.db")


def _rows(coin, tf, n, start=0):
    return [(coin, tf, (start + i) * 60_000, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(n)]


def test_prune_keeps_newest_n():
    s = _store()
    s.upsert_candles(_rows("BTC", "15m", 3005))
    deleted = s.prune_candles_to(3000)
    assert deleted == 5
    cs = s.get_candles("BTC", "15m", limit=10000)
    assert len(cs) == 3000
    # 保留的是最新 3000（open_ms 最大的）：最旧应为第 5 根(idx 5 → open_ms 5*60000)
    assert cs[0].open_time_ms == 5 * 60_000
    assert cs[-1].open_time_ms == 3004 * 60_000


def test_prune_per_coin_tf_independent():
    s = _store()
    s.upsert_candles(_rows("BTC", "15m", 3001))
    s.upsert_candles(_rows("ETH", "15m", 10))
    s.upsert_candles(_rows("BTC", "1H", 10))
    deleted = s.prune_candles_to(3000)
    assert deleted == 1  # 仅 BTC/15m 超额 1
    assert len(s.get_candles("BTC", "15m", 10000)) == 3000
    assert len(s.get_candles("ETH", "15m", 10000)) == 10
    assert len(s.get_candles("BTC", "1H", 10000)) == 10


def test_prune_under_cap_noop():
    s = _store()
    s.upsert_candles(_rows("BTC", "15m", 100))
    assert s.prune_candles_to(3000) == 0
    assert len(s.get_candles("BTC", "15m", 10000)) == 100
