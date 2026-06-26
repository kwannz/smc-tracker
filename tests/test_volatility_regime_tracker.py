"""波动 regime 突破跟踪器单测（合成数据，确定性）。

TDD：先写测试 → RED → 实现 → GREEN。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.volatility_regime_tracker import VolatilityRegimeTracker


# ---- helpers ----

def _row(coin: str, tf: str, regime: str, vol_ratio: float = 0.5, velocity: float = 0.0) -> dict:
    """构造 VolatilityMonitor.rank() 风格的单条输出行。"""
    return {"coin": coin, "by_tf": {tf: {"regime": regime, "vol_ratio": vol_ratio, "velocity": velocity}}}


# ---- 测试 ----

def test_no_event_first_seen_squeeze():
    """首次见 压缩 → 无事件，但 prev 已记录。"""
    tracker = VolatilityRegimeTracker()
    rows = [_row("BTC", "15m", "压缩", vol_ratio=0.3, velocity=0.1)]
    events = tracker.update(rows, now_ms=1_000_000)
    assert events == []
    # prev 已记录
    assert tracker._prev[("BTC", "15m")] == "压缩"


def test_squeeze_to_expansion_emits():
    """先喂 压缩 再喂 扩张 → 1 个事件，包含正确字段。"""
    tracker = VolatilityRegimeTracker()
    now = 1_000_000
    # 第一帧：压缩
    tracker.update([_row("BTC", "15m", "压缩", vol_ratio=0.3, velocity=0.1)], now_ms=now)
    # 第二帧：扩张，间隔 2 小时（超默认冷却 30 分钟）
    now2 = now + 7_200_000
    events = tracker.update([_row("BTC", "15m", "扩张", vol_ratio=1.8, velocity=2.5)], now_ms=now2)
    assert len(events) == 1
    e = events[0]
    assert e["coin"] == "BTC"
    assert e["tf"] == "15m"
    assert abs(e["vol_ratio"] - 1.8) < 1e-9
    assert abs(e["velocity"] - 2.5) < 1e-9


def test_normal_to_expansion_emits():
    """常态 → 扩张 也应触发（不只压缩才触发）。"""
    tracker = VolatilityRegimeTracker()
    now = 2_000_000
    tracker.update([_row("ETH", "1H", "常态")], now_ms=now)
    now2 = now + 7_200_000
    events = tracker.update([_row("ETH", "1H", "扩张", vol_ratio=2.0, velocity=3.0)], now_ms=now2)
    assert len(events) == 1
    assert events[0]["coin"] == "ETH"


def test_expansion_to_expansion_no_repeat():
    """连续 扩张 帧：prev 已是扩张 → 第二次不报，仅首次。"""
    tracker = VolatilityRegimeTracker()
    now = 3_000_000
    # 首次看到扩张（prev=None → 首见，不报）
    events1 = tracker.update([_row("SOL", "4H", "扩张")], now_ms=now)
    assert events1 == []
    # 第二次仍是扩张
    events2 = tracker.update([_row("SOL", "4H", "扩张")], now_ms=now + 7_200_000)
    assert events2 == []


def test_cooldown_suppresses_then_allows():
    """冷却机制：压缩→扩张 报1次；再次压缩→扩张 但未过冷却 → 0；过冷却 → 1。"""
    cooldown_ms = 1_800_000  # 30 分钟
    tracker = VolatilityRegimeTracker(cooldown_ms=cooldown_ms)
    now = 5_000_000

    # 第一轮：压缩
    tracker.update([_row("BTC", "1H", "压缩")], now_ms=now)
    # 扩张 → 首次触发
    e1 = tracker.update([_row("BTC", "1H", "扩张")], now_ms=now + cooldown_ms + 1)
    assert len(e1) == 1

    # 回到压缩
    tracker.update([_row("BTC", "1H", "压缩")], now_ms=now + cooldown_ms + 2)
    # 再扩张 but 冷却未过
    e2 = tracker.update([_row("BTC", "1H", "扩张")], now_ms=now + cooldown_ms + 3)
    assert len(e2) == 0  # 冷却压制

    # 冷却过后再来一轮
    tracker.update([_row("BTC", "1H", "压缩")], now_ms=now + 2 * cooldown_ms + 10)
    e3 = tracker.update([_row("BTC", "1H", "扩张")], now_ms=now + 3 * cooldown_ms + 20)
    assert len(e3) == 1  # 冷却过了，可以报


def test_multi_coin_tf_independent():
    """多 (coin,tf) 互相独立，不互相串扰。"""
    tracker = VolatilityRegimeTracker()
    now = 6_000_000
    rows = [
        _row("BTC", "15m", "压缩"),
        _row("ETH", "1H", "扩张"),   # 首见扩张 → 不报
    ]
    e1 = tracker.update(rows, now_ms=now)
    assert e1 == []

    now2 = now + 7_200_000
    rows2 = [
        _row("BTC", "15m", "扩张"),  # 压缩→扩张 → 报
        _row("ETH", "1H", "扩张"),   # 已是扩张→扩张 → 不报
    ]
    e2 = tracker.update(rows2, now_ms=now2)
    assert len(e2) == 1
    assert e2[0]["coin"] == "BTC"


def test_render_nonempty_contains_coin():
    """有事件时 render 包含 coin 名称与告警头。"""
    tracker = VolatilityRegimeTracker()
    now = 7_000_000
    tracker.update([_row("DOGE", "15m", "压缩")], now_ms=now)
    events = tracker.update([_row("DOGE", "15m", "扩张", vol_ratio=1.9, velocity=2.1)], now_ms=now + 7_200_000)
    assert events
    rendered = tracker.render(events, now_ms=now + 7_200_000)
    assert "DOGE" in rendered
    # 实质断言(修 P2-10 恒真 OR)：扩张确认头 + 放量行 + 速度数值都在
    assert "扩张确认" in rendered
    assert "放量" in rendered
    assert "2.1" in rendered  # velocity 数值渲染


def test_render_empty_returns_empty_string():
    """无事件时 render 返回空字符串。"""
    tracker = VolatilityRegimeTracker()
    result = tracker.render([], now_ms=1_000_000)
    assert result == ""


def test_first_seen_expansion_no_emit():
    """从未见过的 (coin,tf)，首次就是扩张 → 不报（prev=None 视为 首见，不触发）。"""
    tracker = VolatilityRegimeTracker()
    events = tracker.update([_row("XRP", "4H", "扩张")], now_ms=1_000_000)
    assert events == []
