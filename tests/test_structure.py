"""SMC 市场结构引擎单元测试。

合成 K 线，构造确定性的「先 BOS(bull) 后 CHoCH(bear)」序列并断言。
纯计算、无网络。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle
from smc_tracker.smc import MarketStructure, StructureEvent, Swing, analyze


def _candle(i: int, h: float, l: float, c: float) -> Candle:
    """用 (h, l, c) 造一根占位 K 线；o=close，v/n=0，时间戳递增。"""
    return Candle(
        coin="TEST",
        interval="1m",
        open_time_ms=i * 60_000,
        close_time_ms=i * 60_000 + 59_999,
        o=c,
        h=h,
        l=l,
        c=c,
        v=0.0,
        n=0,
    )


def _candles(rows: list[tuple[float, float, float]]) -> list[Candle]:
    return [_candle(i, h, l, c) for i, (h, l, c) in enumerate(rows)]


# 参考构造（lookback=2）：先 BOS(bull) 后 CHoCH(bear)
BOS_THEN_CHOCH = [
    (12, 10, 11), (13, 11, 12), (11, 8, 9), (14, 10, 13), (16, 12, 15),
    (20, 16, 19), (18, 14, 15), (17, 13, 14), (16, 11, 12), (19, 15, 18),
    (22, 18, 21), (23, 19, 22), (25, 21, 24), (24, 20, 21), (22, 16, 17),
    (20, 10, 9),
]


def test_bos_then_choch_sequence():
    """idx10 收21 破 swing high(idx5=20) → BOS bull（trend 初始 None）；
    idx15 收9 破 swing low(idx8=11) → CHoCH bear（trend 已 bull）。"""
    candles = _candles(BOS_THEN_CHOCH)
    events = analyze(candles, lookback=2)

    assert len(events) == 2, [
        (e.type, e.direction, e.level, e.break_index) for e in events
    ]

    bos, choch = events

    # 第一个事件：BOS bull，破 idx5 的 swing high=20，在 idx10 收盘突破
    assert bos.type == "BOS"
    assert bos.direction == "bull"
    assert bos.level == 20
    assert bos.swing_index == 5
    assert bos.break_index == 10

    # 第二个事件：CHoCH bear，破 idx8 的 swing low=11，在 idx15 收盘跌破
    assert choch.type == "CHoCH"
    assert choch.direction == "bear"
    assert choch.level == 11
    assert choch.swing_index == 8
    assert choch.break_index == 15


def test_bos_then_choch_swings_and_trend():
    """逐根喂入，校验摆动点检测数量与最终趋势状态。"""
    ms = MarketStructure(lookback=2)
    all_events: list[StructureEvent] = []
    for c in _candles(BOS_THEN_CHOCH):
        all_events.extend(ms.update(c))

    # 共确认 4 个 swing：low@2=8, high@5=20, low@8=11, high@12=25
    assert len(ms.swings) == 4
    expected = [
        ("low", 2, 8),
        ("high", 5, 20),
        ("low", 8, 11),
        ("high", 12, 25),
    ]
    got = [(s.kind, s.index, s.price) for s in ms.swings]
    assert got == expected, got

    # 全部 swing 都是 Swing 实例
    assert all(isinstance(s, Swing) for s in ms.swings)

    # 最后跌破 → 趋势转为 bear；ref_low 被消费
    assert ms.trend == "bear"
    assert ms.ref_low is None
    assert len(all_events) == 2


def test_monotonic_uptrend_only_bos():
    """单调上升序列：每次向上突破前一 swing high 都是 BOS bull，绝无 CHoCH。"""
    rows: list[tuple[float, float, float]] = []
    # 造一段持续抬高的高低点：先上、小回、再创新高，重复多次
    base = 10.0
    for k in range(6):
        # 上冲
        rows.append((base + 4, base + 1, base + 3))
        rows.append((base + 6, base + 3, base + 5))
        # 小回（不破前低，给出 swing high）
        rows.append((base + 5, base + 2, base + 4))
        rows.append((base + 4, base + 1, base + 3))
        base += 6  # 抬高基准

    events = analyze(_candles(rows), lookback=2)

    # 至少出现一次 BOS；且全程没有任何 CHoCH，方向全 bull
    assert len(events) >= 1
    assert all(e.type == "BOS" for e in events), [
        (e.type, e.direction, e.break_index) for e in events
    ]
    assert all(e.direction == "bull" for e in events)


def test_no_break_no_events():
    """价格在窄幅区间内震荡、收盘从不突破任何已确认 swing → 无事件。"""
    # 高低点恒定，收盘始终落在区间内部，不可能突破
    rows = [(10.0, 8.0, 9.0)] * 12
    ms = MarketStructure(lookback=3)
    events: list[StructureEvent] = []
    for c in _candles(rows):
        events.extend(ms.update(c))

    assert events == []
    assert ms.trend is None
    assert ms.ref_high is None and ms.ref_low is None


def test_lookback_lag_no_premature_swing():
    """swing 确认滞后 lookback 根：喂入不足 2*lb+1 根时不应确认任何 swing。"""
    lb = 3
    ms = MarketStructure(lookback=lb)
    # 仅喂入 2*lb 根（= 6），中心索引 c=i-lb 最大为 2，窗口需到 c+lb=5，
    # 而第 6 根（idx5）才刚够确认 c=2；故喂 6 根后最多确认 idx2 一个。
    rows = [
        (10, 5, 7), (12, 6, 11), (15, 9, 14), (11, 4, 5), (10, 6, 8), (9, 5, 7),
    ]
    candles = _candles(rows)
    for c in candles[:lb]:  # 前 lb 根：c<0，绝不确认
        assert ms.update(c) == []
    assert ms.swings == []


def test_empty_input():
    """空输入：analyze 返回空列表，引擎状态保持初始。"""
    assert analyze([], lookback=2) == []
    ms = MarketStructure()
    assert ms.lookback == 3
    assert ms.swings == [] and ms.trend is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
