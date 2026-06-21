"""TASignal 纯 TA 复合信号单测（合成 K 线，无网络）。

构造两类行情：
  1) 明显上升趋势(150 根，含小幅回调以产生递增的摆动高/低) → 期望 long 信号。
  2) 窄幅震荡(150 根，正弦小波动无趋势) → 期望 None 或弱/中性（不出方向性信号）。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle
from smc_tracker.indicators.knn import KNNPredictor
from smc_tracker.signals.ta_signal import TASignal


def _candle(i: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(coin="TEST", interval="1H",
                  open_time_ms=i * 3_600_000,
                  close_time_ms=i * 3_600_000 + 3_599_999,
                  o=o, h=h, l=l, c=c, v=100.0 + i, n=10)


def _bar_hl(i: int, h: float, l: float) -> Candle:
    """造一根只关心高低点的 K 线（o/c 取区间中点）。"""
    mid = (h + l) / 2.0
    return _candle(i, mid, h, l, mid)


def _uptrend(n: int = 150) -> list[Candle]:
    """稳健上升：孤立谷/峰交替 + 每个极值左右各 3 根平台填充（仿 patterns 测试）。

    结构（循环）：谷(low 深跌) → 3 平台 → 峰(high 尖冲) → 3 平台，每轮基准 level
    抬升 2*step。每个极值被同级平台四面包围 → lookback=3 可确认为分形摆动点；
    峰与谷逐级抬升 → 更高高 + 更高低（道氏上升趋势）。close 净上行使指标看多。
    """
    out: list[Candle] = []
    i = 0
    step = 1.5
    level = 0.0
    for _ in range(3):                       # 起始平台
        out.append(_bar_hl(i, level + 1, level)); i += 1
    while i < n + 10:
        out.append(_bar_hl(i, level + 1, level - 4)); i += 1   # 孤立谷
        for _ in range(3):
            out.append(_bar_hl(i, level + 1, level)); i += 1
        level += step
        out.append(_bar_hl(i, level + 5, level)); i += 1       # 孤立峰
        for _ in range(3):
            out.append(_bar_hl(i, level + 1, level)); i += 1
        level += step
    return out[:n]


def _choppy(n: int = 150) -> list[Candle]:
    """窄幅震荡：围绕固定中枢的小正弦，无净趋势。"""
    out: list[Candle] = []
    mid = 100.0
    for i in range(n):
        wobble = math.sin(i * 0.5) * 2.0
        o = mid + wobble
        c = mid + math.sin(i * 0.5 + 0.2) * 2.0
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        out.append(_candle(i, o, h, l, c))
    return out


def test_uptrend_emits_long():
    candles = _uptrend(150)
    sig = TASignal(threshold=0.3).evaluate(candles, now_ms=0)
    assert sig is not None, "上升趋势应出信号"
    assert sig["direction"] == "long"
    assert sig["score"] > 0.3
    assert sig.get("coin") == "TEST"
    assert isinstance(sig["reasons"], list) and sig["reasons"]
    assert "bias" in sig["components"] and "dow" in sig["components"]
    # 道氏因子应为上升
    assert sig["components"]["dow"] > 0


def test_uptrend_long_with_knn():
    """带 KNN 训练后仍应出 long（KNN 因子参与且不翻转方向）。"""
    candles = _uptrend(150)
    knn = KNNPredictor(k=10, horizon=5)
    knn.fit(candles)
    sig = TASignal(threshold=0.3).evaluate(candles, knn=knn, now_ms=0)
    assert sig is not None
    assert sig["direction"] == "long"
    assert "knn" in sig["components"]


def test_choppy_no_signal():
    candles = _choppy(150)
    sig = TASignal(threshold=0.3).evaluate(candles, now_ms=0)
    # 震荡无趋势：不出方向性信号
    assert sig is None


def test_insufficient_candles_returns_none():
    candles = _uptrend(10)
    assert TASignal().evaluate(candles) is None


def test_fmt_handles_none_and_signal():
    ta = TASignal(threshold=0.3)
    assert "无信号" in ta.fmt(None)
    sig = ta.evaluate(_uptrend(150), now_ms=0)
    text = ta.fmt(sig)
    assert "做多" in text and "TEST" in text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
