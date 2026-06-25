"""dashboard_vol 波动面板纯逻辑单测（tmp db + 合成 K 线，无 HTTP）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.dashboard_vol import volatility_state, pick_coins, render_volatility_page
from smc_tracker.storage import Store


def _store():
    return Store(Path(tempfile.mkdtemp()) / "t.db")


def _seed(s, coin, tf, fn, n=60):
    rows = [(coin, tf, i * 900_000, fn(i), fn(i) * 1.002, fn(i) * 0.998, fn(i), 1.0)
            for i in range(n)]
    s.upsert_candles(rows)


def test_volatility_state_structure():
    s = _store()
    _seed(s, "BTC", "15m", lambda i: 100.0 + i)
    st = volatility_state(s, {"BTC": "BTCUSDT"}, ["15m"], now_ms=0)
    assert st["tfs"] == ["15m"]
    assert st["coins"][0]["coin"] == "BTC"
    assert "by_tf" in st["coins"][0] and "15m" in st["coins"][0]["by_tf"]
    assert "velocity" in st["coins"][0]["by_tf"]["15m"]


def test_pick_coins_prefers_monitored():
    s = _store()
    s.add_monitored_coins([("ETH", "ETHUSDT", 1, "")])
    _seed(s, "BTC", "15m", lambda i: 100.0 + i)  # DB 有 BTC 但清单是 ETH
    assert pick_coins(s) == {"ETH": "ETHUSDT"}


def test_pick_coins_fallback_to_db():
    s = _store()
    _seed(s, "BTC", "15m", lambda i: 100.0 + i)
    assert pick_coins(s) == {"BTC": "BTCUSDT"}  # 清单空 → DB 已采币


def test_render_page_self_contained():
    html = render_volatility_page()
    assert "/api/volatility" in html
    assert "http://" not in html and "https://" not in html  # 无外链
