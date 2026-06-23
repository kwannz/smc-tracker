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
