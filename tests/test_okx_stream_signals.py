"""OKX streaming 信号展示单测（fmt_flow_signals + detect_divergences 纯函数，不联网）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_fmt_flow_signals_long_short():
    """有信号 → 含 coin + 方向。"""
    from smc_tracker.okx.stream import fmt_flow_signals
    out = fmt_flow_signals([{"coin": "BTC", "direction": "long", "net_flow": 600000}])
    assert "BTC" in out
    assert "long" in out
    assert "600,000" in out


def test_fmt_flow_signals_empty_returns_none_text():
    """空列表 → "无"。"""
    from smc_tracker.okx.stream import fmt_flow_signals
    assert fmt_flow_signals([]) == "无"


def test_fmt_flow_signals_multiple_joined():
    """多信号 → 用 / 分隔。"""
    from smc_tracker.okx.stream import fmt_flow_signals
    out = fmt_flow_signals([
        {"coin": "BTC", "direction": "long", "net_flow": 600000},
        {"coin": "ETH", "direction": "short", "net_flow": 700000}])
    assert "BTC" in out and "ETH" in out and "/" in out


def test_detect_divergences_picks_bearish():
    """多头拥挤(funding>0) + taker 净卖 → 背离 bearish。"""
    from smc_tracker.okx.stream import detect_divergences
    latest = {"BTC-USDT-SWAP": {"coin": "BTC", "funding": 0.0005}}
    out = detect_divergences(latest, {"BTC": -500_000})
    assert len(out) == 1
    assert out[0][0] == "BTC"
    assert out[0][1]["direction"] == "bearish"


def test_detect_divergences_none_when_aligned():
    """同向(多头拥挤+净买)→ 无背离。"""
    from smc_tracker.okx.stream import detect_divergences
    latest = {"BTC-USDT-SWAP": {"coin": "BTC", "funding": 0.0005}}
    assert detect_divergences(latest, {"BTC": 500_000}) == []
