"""每币种成交量监控单元测试。

合成 K 线，覆盖 relative_volume / volume_spike / volume_trend /
volume_profile（POC）/ VolumeMonitor 触发。纯计算、无网络。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle
from smc_tracker.indicators.volume import (
    VolumeMonitor,
    relative_volume,
    volume_profile,
    volume_spike,
    volume_trend,
)


def _candle(i: int, v: float, h: float = 1.0, l: float = 1.0, c: float = 1.0) -> Candle:
    """造一根占位 K 线：成交量 v，可选 OHLC。"""
    return Candle(
        coin="X",
        interval="1m",
        open_time_ms=i * 60000,
        close_time_ms=i * 60000 + 59999,
        o=c,
        h=h,
        l=l,
        c=c,
        v=v,
        n=0,
    )


def test_relative_volume() -> None:
    # 前 20 根均量 = 100，最新一根 250 → RVOL = 2.5
    candles = [_candle(i, 100.0) for i in range(20)]
    candles.append(_candle(20, 250.0))
    rvol = relative_volume(candles, n=20)
    assert abs(rvol - 2.5) < 1e-9


def test_relative_volume_insufficient() -> None:
    # 数据不足（仅 1 根）→ 0.0
    assert relative_volume([_candle(0, 100.0)], n=20) == 0.0


def test_volume_spike_triggered() -> None:
    # 造一根放量：均量 100，最新 300 → ratio 3.0 >= mult 2.0
    candles = [_candle(i, 100.0) for i in range(20)]
    candles.append(_candle(20, 300.0))
    res = volume_spike(candles, n=20, mult=2.0)
    assert res["spike"] is True
    assert abs(res["ratio"] - 3.0) < 1e-9


def test_volume_spike_not_triggered() -> None:
    # 平稳量能：最新 120 < 2×100 → 不触发
    candles = [_candle(i, 100.0) for i in range(20)]
    candles.append(_candle(20, 120.0))
    res = volume_spike(candles, n=20, mult=2.0)
    assert res["spike"] is False
    assert abs(res["ratio"] - 1.2) < 1e-9


def test_volume_trend_rising() -> None:
    # 递增量 → rising
    candles = [_candle(i, 100.0 + i * 10.0) for i in range(20)]
    assert volume_trend(candles, n=20) == "rising"


def test_volume_trend_falling() -> None:
    # 递减量 → falling
    candles = [_candle(i, 300.0 - i * 10.0) for i in range(20)]
    assert volume_trend(candles, n=20) == "falling"


def test_volume_trend_flat() -> None:
    # 恒定量 → flat
    candles = [_candle(i, 100.0) for i in range(20)]
    assert volume_trend(candles, n=20) == "flat"


def test_volume_profile_poc() -> None:
    # 大部分成交量集中在价格 10 附近 → POC 应落在该价区
    candles = []
    for i in range(10):
        # 低量分散在不同高价
        candles.append(_candle(i, 5.0, h=20.0 + i, l=19.0 + i, c=19.5 + i))
    for i in range(5):
        # 高量集中在价格 ~10
        candles.append(_candle(100 + i, 200.0, h=10.5, l=9.5, c=10.0))
    prof = volume_profile(candles, bins=10)
    assert prof["poc"] is not None
    # POC 价格应接近 10（集中放量区），远离高价 20+
    assert 8.0 <= prof["poc"] <= 12.0
    # levels 数量等于 bins
    assert len(prof["levels"]) == 10
    # 最大成交量箱即 POC
    max_level = max(prof["levels"], key=lambda x: x[1])
    assert abs(max_level[0] - prof["poc"]) < 1e-9


def test_volume_profile_empty() -> None:
    prof = volume_profile([], bins=10)
    assert prof["levels"] == []
    assert prof["poc"] is None


def test_volume_monitor_triggers() -> None:
    mon = VolumeMonitor(window=20, spike_mult=3.0)
    # 喂入 10 根均量 100，均不触发（前几根缺基准 / 比值=1）
    fired = None
    for i in range(10):
        out = mon.update("BTC", _candle(i, 100.0))
        assert out is None
    # 喂入一根放量 400 → 400/100 = 4.0 >= 3.0，触发
    fired = mon.update("BTC", _candle(10, 400.0))
    assert fired is not None
    assert fired["coin"] == "BTC"
    assert fired["vol"] == 400.0
    assert fired["ratio"] >= 3.0


def test_volume_monitor_per_coin_isolation() -> None:
    # 不同币种窗口互不干扰
    mon = VolumeMonitor(window=20, spike_mult=3.0)
    mon.update("BTC", _candle(0, 100.0))
    # ETH 首根无历史 → 即便量大也不触发
    out_eth_first = mon.update("ETH", _candle(0, 9999.0))
    assert out_eth_first is None
    # ETH 第二根相对自身基准 9999 不放量
    out_eth_second = mon.update("ETH", _candle(1, 100.0))
    assert out_eth_second is None
    # BTC 放量仍按自身基准 100 触发
    out_btc = mon.update("BTC", _candle(1, 500.0))
    assert out_btc is not None
    assert out_btc["coin"] == "BTC"


def test_volume_monitor_first_candle_no_trigger() -> None:
    # 首根无历史基准 → 不触发
    mon = VolumeMonitor(window=20, spike_mult=3.0)
    assert mon.update("DOGE", _candle(0, 1000.0)) is None
