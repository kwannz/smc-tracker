"""StructureFeed 单测：验证「开盘时间变化=上一根收盘」的驱动逻辑（无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.smc.feed import StructureFeed, candle_from_ws


def _ws(t, o, h, l, c, coin="BTC"):
    return {"s": coin, "i": "1m", "t": t, "T": t + 59999,
            "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": "1", "n": 1}


def test_candle_from_ws():
    c = candle_from_ws(_ws(1000, 10, 12, 9, 11))
    assert c.coin == "BTC" and c.open_time_ms == 1000
    assert c.o == 10 and c.h == 12 and c.l == 9 and c.c == 11


def test_close_detection_feeds_prev():
    fed = []
    feed = StructureFeed(lookback=2, on_event=lambda coin, e: None)
    # 同一根 t=1000 多次刷新，不应推进结构
    feed.on_candle_ws(_ws(1000, 10, 12, 9, 11))
    feed.on_candle_ws(_ws(1000, 10, 15, 9, 14))   # 仍是同一根，更新高点
    assert feed.structure("BTC") is None or len(feed.structure("BTC").swings) == 0
    # t 变为 2000 → 上一根(t=1000, 最终 h=15,c=14)收盘被喂入
    feed.on_candle_ws(_ws(2000, 14, 16, 13, 15))
    ms = feed.structure("BTC")
    assert ms is not None
    # 已喂入恰好 1 根已收盘 K 线（i 从 -1 起，喂 1 根后为 0；最终 h=15,l=9）
    assert ms._i == 0
    assert ms._highs[-1] == 15.0 and ms._lows[-1] == 9.0


def test_feed_produces_bos():
    """喂入一段确定性上升后突破，应在收盘驱动下产生 BOS。"""
    events = []
    feed = StructureFeed(lookback=2, on_event=lambda coin, e: events.append(e))
    # 复用 structure 测试的构造（每根用不同 t 触发上一根收盘）
    bars = [(12,10,11),(13,11,12),(11,8,9),(14,10,13),(16,12,15),(20,16,19),
            (18,14,15),(17,13,14),(16,11,12),(19,15,18),(22,18,21),(23,19,22)]
    for i, (h, l, c) in enumerate(bars):
        feed.on_candle_ws(_ws(1000 + i * 60000, c, h, l, c))
    # 末尾再发一根新 t，确保最后一根（idx10 收21 突破 swing high 20）被收盘喂入
    feed.on_candle_ws(_ws(1000 + 99 * 60000, 22, 24, 20, 21))
    types = [(e.type, e.direction) for e in events]
    assert ("BOS", "bull") in types, types


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
