"""app._on_meme_trade 回调单测：MemeTradeMonitor 的 on_trade 传 dict record，
回调必须按 dict 取键，不能当对象取属性（否则每次大单 meme 成交都 AttributeError）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_on_meme_trade_accepts_dict_record():
    """on_trade 回调入参是 dict（meme_trade_monitor.py:28 签名 + :140 传 rec dict）。

    _on_meme_trade 不使用 self，故 unbound 调用即可测；含字面下划线 taker 截断。
    回归 bug：原实现用 t.coin / t.taker_side 属性访问 dict → AttributeError 被 try/except
    吞掉，控制台 meme 告警永久不打印（功能静默失效）。
    """
    from smc_tracker.app import TradingSystem
    rec = {"coin": "PEPE", "taker_side": "B", "notional": 50_000.0,
           "taker": "0xabcdef1234567890"}
    # self 未被该方法引用，传 None 即可；不应抛 AttributeError
    TradingSystem._on_meme_trade(None, rec)


def test_on_meme_trade_sell_side():
    """taker_side != 'B' → 卖；同样按 dict 取键不报错。"""
    from smc_tracker.app import TradingSystem
    rec = {"coin": "WIF", "taker_side": "A", "notional": 12_345.0,
           "taker": "0x1234567890abcdef"}
    TradingSystem._on_meme_trade(None, rec)
