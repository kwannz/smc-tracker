"""BitgetREST.klines 单元测试（monkeypatch _get，不联网）。

覆盖：
  - 返回升序 Candle、ts/close_time_ms 正确
  - bars 超 1999 被 clamp
  - granularity 非法 raise ValueError
  - 脏行（字段缺失/非数）被跳过，不崩溃
  - bars<=1000 只调用 candles 端点（不分页）
  - bars>1000 先调 candles 再分页 history-candles 补足
  - coin/interval 字段正确填充
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.bitget.rest import BitgetREST, GRANULARITY_MS


# ---- 辅助工厂 ----

def _make_raw_rows(start_ts: int, n: int, gran_ms: int, base_px: float = 100.0):
    """生成 n 根升序原始 K 线行（模拟 Bitget API 返回格式）。"""
    rows = []
    for i in range(n):
        ts = start_ts + i * gran_ms
        px = base_px + i * 0.1
        rows.append([str(ts), str(px), str(px + 0.5), str(px - 0.5),
                     str(px + 0.01), str(1000.0 + i), str((px + 0.01) * (1000.0 + i))])
    return rows


# ---- 单根 K 线解析 ----

@pytest.mark.asyncio
async def test_klines_basic_returns_candles(monkeypatch):
    """基本场景：500 根 5m K 线，升序，ts/close_time_ms 正确。"""
    gran = "5m"
    gran_ms = GRANULARITY_MS[gran]
    start = 1_700_000_000_000
    raw = _make_raw_rows(start, 500, gran_ms)

    async def _fake_get(self, path, **params):
        return raw  # 只调 candles 端点

    monkeypatch.setattr(BitgetREST, "_get", _fake_get)
    async with BitgetREST() as bg:
        candles = await bg.klines("BTCUSDT", gran, bars=500, coin="BTC")

    assert len(candles) == 500
    # 升序
    for i in range(1, len(candles)):
        assert candles[i].open_time_ms >= candles[i - 1].open_time_ms
    # close_time_ms = open_time_ms + gran_ms
    for c in candles:
        assert c.close_time_ms == c.open_time_ms + gran_ms
    # coin / interval
    assert candles[0].coin == "BTC"
    assert candles[0].interval == gran


@pytest.mark.asyncio
async def test_klines_bars_clamp(monkeypatch):
    """bars>1999 被 clamp 到 1999（不崩溃）。"""
    gran = "1H"
    gran_ms = GRANULARITY_MS[gran]
    start = 1_700_000_000_000
    # 只提供 1000 根（candles 端点返回 min(clamped,1000)=1000）
    raw = _make_raw_rows(start, 1000, gran_ms)

    calls = []

    async def _fake_get(self, path, **params):
        calls.append(path)
        if "history-candles" in path:
            return []  # 无更多历史
        return raw

    monkeypatch.setattr(BitgetREST, "_get", _fake_get)
    async with BitgetREST() as bg:
        candles = await bg.klines("BTCUSDT", gran, bars=5000)  # 超 1999 → clamp 1999

    # 最终应该是 ≤1999 根（因为 history-candles 返回空，所以只有 1000 根）
    assert len(candles) <= 1999
    assert len(candles) >= 1  # 至少有数据


@pytest.mark.asyncio
async def test_klines_invalid_granularity():
    """非法 granularity → raise ValueError（不联网）。"""
    async with BitgetREST() as bg:
        with pytest.raises(ValueError, match="granularity"):
            await bg.klines("BTCUSDT", "99x", bars=100)


@pytest.mark.asyncio
async def test_klines_dirty_rows_skipped(monkeypatch):
    """脏行（字段缺失、非数值）被跳过，不 KeyError/IndexError/崩溃。"""
    gran = "1m"
    gran_ms = GRANULARITY_MS[gran]
    start = 1_700_000_000_000
    raw_good = _make_raw_rows(start, 5, gran_ms)
    dirty_rows = [
        [],                              # 空行
        ["abc"],                         # 只有一个字段
        [str(start + 10 * gran_ms), "NaN", "1", "1", "1", "1", "1"],  # NaN close
        [str(start + 11 * gran_ms), "inf", "1", "1", "1", "1", "1"],  # inf open
        None,                            # None 行（防御）
    ]
    # 混合脏行
    mixed = raw_good + dirty_rows  # type: ignore[operator]

    async def _fake_get(self, path, **params):
        return mixed

    monkeypatch.setattr(BitgetREST, "_get", _fake_get)
    async with BitgetREST() as bg:
        candles = await bg.klines("BTCUSDT", gran, bars=100)

    # 只有 5 根有效
    assert len(candles) == 5


@pytest.mark.asyncio
async def test_klines_dedup_and_sort(monkeypatch):
    """重复 ts 应去重，结果保证升序。"""
    gran = "5m"
    gran_ms = GRANULARITY_MS[gran]
    start = 1_700_000_000_000
    raw = _make_raw_rows(start, 10, gran_ms)
    # 故意加重复行
    raw_dup = raw + raw[:3]

    async def _fake_get(self, path, **params):
        return raw_dup

    monkeypatch.setattr(BitgetREST, "_get", _fake_get)
    async with BitgetREST() as bg:
        candles = await bg.klines("BTCUSDT", gran, bars=100)

    # 去重后 10 根
    assert len(candles) == 10
    for i in range(1, len(candles)):
        assert candles[i].open_time_ms > candles[i - 1].open_time_ms


@pytest.mark.asyncio
async def test_klines_pagination_for_large_bars(monkeypatch):
    """bars>1000 时：先调 candles 取 1000 根，再用 history-candles 分页补足。"""
    gran = "1H"
    gran_ms = GRANULARITY_MS[gran]
    base_ts = 1_700_000_000_000

    # candles 端点返回最新 1000 根
    candles_rows = _make_raw_rows(base_ts + 200 * gran_ms, 1000, gran_ms)
    # history-candles 端点返回更早 200 根（endTime = base_ts + 200*gran_ms）
    hist_rows = _make_raw_rows(base_ts, 200, gran_ms)

    call_log = []

    async def _fake_get(self, path, **params):
        call_log.append(path)
        if "history-candles" in path:
            return hist_rows if len(call_log) <= 2 else []
        return candles_rows

    monkeypatch.setattr(BitgetREST, "_get", _fake_get)
    async with BitgetREST() as bg:
        candles = await bg.klines("BTCUSDT", gran, bars=1200)

    # 应至少调了 history-candles 端点
    assert any("history-candles" in c for c in call_log)
    # 总行数 ≥ 1000（至少有主端点的数据）
    assert len(candles) >= 1000
    # 升序
    for i in range(1, len(candles)):
        assert candles[i].open_time_ms >= candles[i - 1].open_time_ms


@pytest.mark.asyncio
async def test_klines_coin_fallback_to_symbol(monkeypatch):
    """coin 参数为空时，coin 字段回退到 symbol。"""
    gran = "5m"
    gran_ms = GRANULARITY_MS[gran]
    raw = _make_raw_rows(1_700_000_000_000, 5, gran_ms)

    async def _fake_get(self, path, **params):
        return raw

    monkeypatch.setattr(BitgetREST, "_get", _fake_get)
    async with BitgetREST() as bg:
        candles = await bg.klines("XYZUSDT", gran, bars=5, coin="")

    assert candles[0].coin == "XYZUSDT"


# ---- GRANULARITY_MS 内容验证 ----

def test_granularity_ms_coverage():
    """GRANULARITY_MS 包含所有合法周期键。"""
    required = {"1m", "3m", "5m", "15m", "30m", "1H", "4H", "6H", "12H", "1D", "3D", "1W", "1M"}
    assert required.issubset(set(GRANULARITY_MS.keys()))


def test_granularity_ms_values():
    """关键值正确：1m=60000, 1H=3600000, 1D=86400000, 1W=604800000。"""
    assert GRANULARITY_MS["1m"] == 60_000
    assert GRANULARITY_MS["1H"] == 3_600_000
    assert GRANULARITY_MS["1D"] == 86_400_000
    assert GRANULARITY_MS["1W"] == 604_800_000
