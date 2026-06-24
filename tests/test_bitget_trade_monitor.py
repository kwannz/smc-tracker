"""Bitget 逐笔成交监控（taker 资金流 → flow_score）单测。

补 R2 唯一未接数据源（flow_score）：订阅 Bitget public `trade` channel，累积 per-coin
taker 净流向（买 +、卖 −）喂 FlowPredictor，flow_score = tanh(资金流加速度)。

防御性解析（第一性原理：先稳健处理两种可能格式，再上线实证）：
- dict 行 {"price","size","side":"buy"/"sell","ts"}
- list 行 [ts, price, size, side]
"""
from __future__ import annotations

from smc_tracker.monitor.bitget_trade_monitor import (
    BitgetTradeMonitor, parse_trade_delta,
)


# ── parse_trade_delta：签名净流向（含格式稳健性） ──
def test_parse_dict_buy_positive():
    assert parse_trade_delta([{"price": "100", "size": "2", "side": "buy"}]) == 200.0


def test_parse_dict_sell_negative():
    assert parse_trade_delta([{"price": "100", "size": "2", "side": "sell"}]) == -200.0


def test_parse_list_format():
    # [ts, price, size, side]
    assert parse_trade_delta([["1700000000000", "100", "2", "buy"]]) == 200.0


def test_parse_mixed_net():
    d = [{"price": "100", "size": "1", "side": "buy"},
         {"price": "100", "size": "3", "side": "sell"}]
    assert parse_trade_delta(d) == -200.0


def test_parse_empty_zero():
    assert parse_trade_delta([]) == 0.0


def test_parse_malformed_skipped():
    """脏行不崩，跳过（util.to_float 拒脏值）。"""
    assert parse_trade_delta([{"price": "x", "size": "y", "side": "buy"}]) == 0.0


# ── flow_score：资金流加速度 ──
def test_flow_score_none_without_samples():
    m = BitgetTradeMonitor(sym2coin={})
    assert m.flow_score("BTC", now_ms=1000) is None


def test_flow_score_positive_on_accelerating_buys():
    """近半窗买盘远大于前半窗 → 加速流入 → flow_score > 0。"""
    m = BitgetTradeMonitor(sym2coin={"BTCUSDT": "BTC"})
    now = 1_000_000_000
    m.record("BTC", 1000.0, now - 400_000)     # 前半窗小买
    m.record("BTC", 50000.0, now - 100_000)    # 近半窗大买
    score = m.flow_score("BTC", now_ms=now)
    assert score is not None and score > 0.0


def test_flow_score_negative_on_accelerating_sells():
    """近半窗卖盘加速 → flow_score < 0。"""
    m = BitgetTradeMonitor(sym2coin={"BTCUSDT": "BTC"})
    now = 1_000_000_000
    m.record("BTC", -1000.0, now - 400_000)
    m.record("BTC", -50000.0, now - 100_000)
    assert m.flow_score("BTC", now_ms=now) < 0.0


def test_flow_score_dead_zone_cuts_noise():
    """M1：噪声级净流加速度（|score|<死区）→ 0.0，不把噪声当前瞻信号。"""
    m = BitgetTradeMonitor(sym2coin={"BTCUSDT": "BTC"})
    now = 1_000_000_000
    m.record("BTC", 90.0, now - 400_000)    # 前半窗
    m.record("BTC", 100.0, now - 100_000)   # 近半窗（仅微增 → accel 极小）
    assert m.flow_score("BTC", now_ms=now) == 0.0


def test_flow_score_clamped_unit():
    """flow_score ∈ [-1,1]。"""
    m = BitgetTradeMonitor(sym2coin={"BTCUSDT": "BTC"})
    now = 1_000_000_000
    m.record("BTC", 1e12, now - 100_000)
    s = m.flow_score("BTC", now_ms=now)
    assert -1.0 <= s <= 1.0


def test_on_trade_accumulates_via_handler():
    """on_trade(arg,data,recv_ns) 解析 instId→coin 并累积。"""
    m = BitgetTradeMonitor(sym2coin={"BTCUSDT": "BTC"})
    now = 1_000_000_000
    arg = {"channel": "trade", "instId": "BTCUSDT"}
    m.on_trade(arg, [{"price": "100", "size": "10", "side": "buy"}], recv_ns=0, now_ms=now - 100_000)
    assert m.flow_score("BTC", now_ms=now) is not None  # 有样本


def test_last_price_tracked():
    """on_trade 记录最新成交价（供 forming 逼近检测用实时 Bitget 价）。"""
    m = BitgetTradeMonitor(sym2coin={"BTCUSDT": "BTC"})
    m.on_trade({"instId": "BTCUSDT"},
               [{"price": "62000", "size": "1", "side": "buy"},
                {"price": "62100", "size": "1", "side": "sell"}], recv_ns=0, now_ms=1000)
    assert m.last_price("BTC") == 62100.0  # 最后一笔


def test_last_price_none_for_unknown():
    m = BitgetTradeMonitor(sym2coin={})
    assert m.last_price("BTC") is None
