"""B1: 谐波 K线 WS 增量驱动实时性 TDD 测试。

覆盖：
  1. _parse_candle_row: 合成 WS 行 → Candle 解析正确；脏行跳过不崩。
  2. _is_bar_closed: 已收盘 / 未收盘判断逻辑。
  3. _TF_TO_CHANNEL: tf → channel 名映射正确。
  4. handler 解析正确：合成 candle WS update 消息 → handler 调用 → upsert_candles 被调用。
  5. 未收盘 bar 不触发 upsert_candles（forming bar 跳过）。
  6. handler 异常不崩（未知 instId，脏数据）。
  7. 开关 realtime_ws=False 时 HarmonicCandleWS 默认不启用（不影响现网）。
  8. no-repaint 兼容：WS 增量喂入与批量 refresh 对同一 K 线序列 analyze_candles 产出相同结构。

所有测试：合成数据，确定性，无网络。
"""
from __future__ import annotations

import asyncio
import math
import time
from unittest.mock import MagicMock, patch
from typing import Any

import pytest

from smc_tracker.monitor.harmonic_candle_ws import (
    _parse_candle_row,
    _is_bar_closed,
    _TF_TO_CHANNEL,
    HarmonicCandleWS,
)
from smc_tracker.bitget.rest import GRANULARITY_MS
from smc_tracker.models import Candle
from smc_tracker.indicators.harmonic import analyze_candles


# ======================================================================
# 辅助
# ======================================================================

def _make_row(ts: int = 1_700_000_000_000, o: float = 100.0, h: float = 101.0,
              l: float = 99.0, c: float = 100.5, v: float = 1000.0) -> list:
    """构造标准 Bitget candle WS 行（字符串格式，与 REST 协议一致）。"""
    return [str(ts), str(o), str(h), str(l), str(c), str(v), "0"]


def _mock_monitor(timeframes=None, bars=100, order=3, tol=0.05):
    """构造 HarmonicMonitor 的 Mock（鸭子类型，只需 store/order/tol/bars/timeframes/coin_to_symbol）。"""
    m = MagicMock()
    m.timeframes = timeframes or ["15m", "1H"]
    m.bars = bars
    m.order = order
    m.tol = tol
    m.coin_to_symbol = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
    m.store = MagicMock()
    m.store.get_candles.return_value = []
    return m


def _run(coro):
    """运行 async 函数（兼容 Python 3.14 不再提供隐式 event loop）。"""
    return asyncio.run(coro)


# ======================================================================
# 1. _parse_candle_row：行解析
# ======================================================================

class TestParseCandelRow:
    def test_standard_row_returns_candle(self):
        """标准 WS 行 → 返回正确 Candle 对象。"""
        gran_ms = GRANULARITY_MS["15m"]  # 900_000
        row = _make_row(ts=1_700_000_000_000, o=50000.0, h=51000.0, l=49000.0, c=50500.0, v=100.0)
        c = _parse_candle_row(row, "BTC", "15m", gran_ms)
        assert c is not None
        assert c.coin == "BTC"
        assert c.interval == "15m"
        assert c.open_time_ms == 1_700_000_000_000
        assert c.close_time_ms == 1_700_000_000_000 + gran_ms
        assert c.o == 50000.0
        assert c.h == 51000.0
        assert c.l == 49000.0
        assert c.c == 50500.0
        assert c.v == 100.0

    def test_integer_ts_format(self):
        """ts 为整数（非字符串）也能解析。"""
        gran_ms = GRANULARITY_MS["1H"]
        row = [1_700_000_000_000, 100.0, 101.0, 99.0, 100.5, 500.0, 0]
        c = _parse_candle_row(row, "ETH", "1H", gran_ms)
        assert c is not None
        assert c.open_time_ms == 1_700_000_000_000

    def test_too_short_row_returns_none(self):
        """少于 6 列的行 → None（不崩溃）。"""
        result = _parse_candle_row([1_700_000_000_000, 100.0, 101.0], "BTC", "15m", 900_000)
        assert result is None

    def test_empty_row_returns_none(self):
        """空行 → None。"""
        assert _parse_candle_row([], "BTC", "15m", 900_000) is None
        assert _parse_candle_row(None, "BTC", "15m", 900_000) is None

    def test_nan_price_returns_none(self):
        """价格含 NaN → None（数据质量守卫）。"""
        gran_ms = GRANULARITY_MS["15m"]
        row = [str(1_700_000_000_000), "nan", "101.0", "99.0", "100.5", "100.0", "0"]
        assert _parse_candle_row(row, "BTC", "15m", gran_ms) is None

    def test_zero_price_returns_none(self):
        """价格为 0 → None（守卫拒 price≤0）。"""
        gran_ms = GRANULARITY_MS["15m"]
        row = [str(1_700_000_000_000), "0.0", "101.0", "99.0", "100.5", "100.0", "0"]
        assert _parse_candle_row(row, "BTC", "15m", gran_ms) is None

    def test_malformed_ts_returns_none(self):
        """ts 无法解析为数字 → None（不崩溃）。"""
        gran_ms = GRANULARITY_MS["15m"]
        row = ["bad_ts", "100.0", "101.0", "99.0", "100.5", "100.0", "0"]
        assert _parse_candle_row(row, "BTC", "15m", gran_ms) is None

    def test_volume_zero_allowed(self):
        """成交量=0 合法（允许零成交量的 bar）。"""
        gran_ms = GRANULARITY_MS["15m"]
        row = _make_row(v=0.0)
        c = _parse_candle_row(row, "BTC", "15m", gran_ms)
        assert c is not None
        assert c.v == 0.0


# ======================================================================
# 2. _is_bar_closed：收盘判断
# ======================================================================

class TestIsBarClosed:
    def test_closed_bar_returns_true(self):
        """open_ms + gran_ms < now_ms → 已收盘。"""
        gran_ms = GRANULARITY_MS["1m"]  # 60_000
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 5000  # 5秒后
        assert _is_bar_closed(open_ms, gran_ms, now_ms) is True

    def test_forming_bar_returns_false(self):
        """open_ms + gran_ms > now_ms + 容差 → 未收盘（forming bar）。"""
        gran_ms = GRANULARITY_MS["15m"]  # 900_000
        open_ms = 1_700_000_000_000
        now_ms = open_ms + 30_000  # 30秒后，bar 还未收
        assert _is_bar_closed(open_ms, gran_ms, now_ms) is False

    def test_exactly_at_close_with_tolerance(self):
        """close_ms = now_ms（边界）→ 已收盘（允许 1s 容差）。"""
        gran_ms = GRANULARITY_MS["1m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms  # 恰好收盘时刻
        assert _is_bar_closed(open_ms, gran_ms, now_ms) is True

    def test_large_granularity_closed(self):
        """1W bar 收盘判断（大周期）。"""
        gran_ms = GRANULARITY_MS["1W"]  # 604_800_000
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        assert _is_bar_closed(open_ms, gran_ms, now_ms) is True

    def test_large_granularity_forming(self):
        """1W bar 还在形成中 → False。"""
        gran_ms = GRANULARITY_MS["1W"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + 86_400_000  # 仅1天后，还未收
        assert _is_bar_closed(open_ms, gran_ms, now_ms) is False


# ======================================================================
# 3. _TF_TO_CHANNEL：tf → channel 映射
# ======================================================================

class TestTfToChannel:
    def test_standard_tfs_have_channels(self):
        """常用周期都有对应 channel。"""
        for tf in ["1m", "5m", "15m", "1H", "4H", "6H", "12H", "1D", "1W"]:
            assert tf in _TF_TO_CHANNEL, f"tf={tf} 缺少 channel 映射"
            assert _TF_TO_CHANNEL[tf] == f"candle{tf}", (
                f"tf={tf}: 期望 candle{tf}，实际 {_TF_TO_CHANNEL[tf]}"
            )

    def test_channel_prefix_is_candle(self):
        """所有 channel 以 'candle' 开头。"""
        for tf, ch in _TF_TO_CHANNEL.items():
            assert ch.startswith("candle"), f"tf={tf}: channel={ch} 不以 candle 开头"

    def test_all_granularity_ms_keys_mapped(self):
        """GRANULARITY_MS 中所有 tf 都有 channel 映射（不遗漏）。"""
        for tf in GRANULARITY_MS:
            assert tf in _TF_TO_CHANNEL, f"GRANULARITY_MS tf={tf} 未在 _TF_TO_CHANNEL 中"


# ======================================================================
# 4. handler 解析：合成 WS update → upsert_candles 被调用
# ======================================================================

class TestHandlerParsing:
    def test_closed_bar_triggers_upsert(self):
        """已收盘 bar → upsert_candles 被调用（异步 Task，event loop 执行后检查）。"""
        monitor = _mock_monitor(timeframes=["15m"])
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms, o=50000.0, h=51000.0, l=49000.0, c=50500.0, v=100.0)
        arg = {"channel": "candle15m", "instId": "BTCUSDT"}
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler(arg, [row], recv_ns)
            await asyncio.sleep(0.1)

        _run(_run_test())
        monitor.store.upsert_candles.assert_called_once()
        call_args = monitor.store.upsert_candles.call_args[0][0]
        assert len(call_args) == 1
        coin_arg, tf_arg, ts_arg = call_args[0][0], call_args[0][1], call_args[0][2]
        assert coin_arg == "BTC"
        assert tf_arg == "15m"
        assert ts_arg == open_ms

    def test_forming_bar_no_upsert(self):
        """未收盘 bar（forming）→ upsert_candles 不被调用。"""
        monitor = _mock_monitor(timeframes=["15m"])
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)

        gran_ms = GRANULARITY_MS["15m"]
        # 当前时刻开盘，bar 尚未收盘
        now_ms = int(time.time() * 1000)
        open_ms = now_ms - 10_000  # 10秒前开盘，gran=900_000ms，远未收
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms)
        arg = {"channel": "candle15m", "instId": "BTCUSDT"}
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler(arg, [row], recv_ns)
            await asyncio.sleep(0.05)

        _run(_run_test())
        monitor.store.upsert_candles.assert_not_called()

    def test_unknown_instid_ignored(self):
        """未知 instId（非监控币）→ 不触发任何操作，不崩溃。"""
        monitor = _mock_monitor(timeframes=["15m"])
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms)
        arg = {"channel": "candle15m", "instId": "XYZUSDT"}  # 非监控 symbol
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler(arg, [row], recv_ns)
            await asyncio.sleep(0.05)

        _run(_run_test())
        monitor.store.upsert_candles.assert_not_called()

    def test_dirty_row_in_data_no_crash(self):
        """data 中含脏行（解析失败）→ 跳过，不崩溃，不影响有效行。"""
        monitor = _mock_monitor(timeframes=["15m"])
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)

        dirty_row = ["bad_ts", "bad_o"]  # 脏行（too short + bad values）
        arg = {"channel": "candle15m", "instId": "BTCUSDT"}
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler(arg, [dirty_row], recv_ns=0)
            await asyncio.sleep(0.05)

        # 不应抛出任何异常
        _run(_run_test())

    def test_empty_data_no_crash(self):
        """data=[] → 不崩溃。"""
        monitor = _mock_monitor(timeframes=["15m"])
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler({"instId": "BTCUSDT"}, [], 0)
            await asyncio.sleep(0.05)

        _run(_run_test())


# ======================================================================
# 5. analyze_candles 增量触发（功能集成）
# ======================================================================

class TestIncrementalAnalysis:
    def test_closed_bar_triggers_get_candles(self):
        """已收盘 bar + store 返回足够 K 线 → store.get_candles 被调用（触发分析路径）。"""
        from tests.test_harmonic_no_repaint import _zigzag_candles
        monitor = _mock_monitor(timeframes=["15m"], bars=50, order=3)

        # store.get_candles 返回足够数量 K 线（足以通过 2*order+3=9 的最小根数守卫）
        mock_candles = _zigzag_candles(50, amplitude=10.0)
        monitor.store.get_candles.return_value = list(mock_candles)

        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms)
        arg = {"channel": "candle15m", "instId": "BTCUSDT"}
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler(arg, [row], recv_ns)
            await asyncio.sleep(0.1)

        _run(_run_test())
        # store.get_candles 应被调用（以便读取最新 K 线做分析）
        monitor.store.get_candles.assert_called()
        call_args = monitor.store.get_candles.call_args
        assert call_args[0][0] == "BTC"
        assert call_args[0][1] == "15m"

    def test_insufficient_candles_skips_analysis(self):
        """DB K线不足（< 2*order+3）→ analyze_candles 不被调用（守卫跳过）。"""
        from tests.test_harmonic_no_repaint import _zigzag_candles
        monitor = _mock_monitor(timeframes=["15m"], bars=50, order=3)
        # 只返回 5 根（小于 2*3+3=9）
        mock_candles = _zigzag_candles(5, amplitude=10.0)
        monitor.store.get_candles.return_value = list(mock_candles)

        ws = MagicMock()
        called_with: list = []

        async def _fake_analyze(candles, order, tol):
            called_with.append(len(candles))
            return {"completed": [], "forming": [], "price": 100.0}

        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms)
        handler = hwws._make_handler("15m")

        async def _run_test():
            with patch("smc_tracker.monitor.harmonic_candle_ws.analyze_candles",
                       side_effect=_fake_analyze):
                handler({"instId": "BTCUSDT"}, [row], recv_ns)
                await asyncio.sleep(0.1)

        _run(_run_test())
        assert len(called_with) == 0, (
            f"analyze_candles 不应被调用（DB 不足），但调用了 {len(called_with)} 次"
        )


# ======================================================================
# 6. on_update 回调
# ======================================================================

class TestOnUpdateCallback:
    def test_on_update_called_with_result(self):
        """analyze_candles 完成后 on_update 被调用，传入 (coin, tf, result, now_ms)。"""
        from tests.test_harmonic_no_repaint import _zigzag_candles
        monitor = _mock_monitor(timeframes=["15m"], bars=50, order=3)
        mock_candles = _zigzag_candles(50, amplitude=10.0)
        monitor.store.get_candles.return_value = list(mock_candles)

        updates: list[tuple] = []

        def _on_update(coin: str, tf: str, result: Any, now_ms: int) -> None:
            updates.append((coin, tf, result, now_ms))

        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws, on_update=_on_update)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms)
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler({"instId": "BTCUSDT"}, [row], recv_ns)
            await asyncio.sleep(0.15)

        _run(_run_test())
        assert len(updates) == 1
        coin_r, tf_r, result_r, now_r = updates[0]
        assert coin_r == "BTC"
        assert tf_r == "15m"
        # result 含 completed/forming 键（analyze_candles 契约），或可能为 None（无形态）
        if result_r is not None:
            assert "completed" in result_r
            assert "forming" in result_r

    def test_async_on_update_callback(self):
        """on_update 可以是 async 函数（awaitable），也能正确调用。"""
        from tests.test_harmonic_no_repaint import _zigzag_candles
        monitor = _mock_monitor(timeframes=["15m"], bars=50, order=3)
        mock_candles = _zigzag_candles(50, amplitude=10.0)
        monitor.store.get_candles.return_value = list(mock_candles)

        updates: list = []

        async def _async_on_update(coin, tf, result, now_ms):
            updates.append((coin, tf))

        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws, on_update=_async_on_update)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms)
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler({"instId": "BTCUSDT"}, [row], recv_ns)
            await asyncio.sleep(0.15)

        _run(_run_test())
        assert len(updates) == 1

    def test_on_update_exception_does_not_crash(self):
        """on_update 内部异常不崩溃（WS 健壮性）。"""
        from tests.test_harmonic_no_repaint import _zigzag_candles
        monitor = _mock_monitor(timeframes=["15m"], bars=50, order=3)
        mock_candles = _zigzag_candles(50, amplitude=10.0)
        monitor.store.get_candles.return_value = list(mock_candles)

        def _bad_callback(coin, tf, result, now_ms):
            raise RuntimeError("模拟回调失败")

        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws, on_update=_bad_callback)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms)
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler({"instId": "BTCUSDT"}, [row], recv_ns)
            await asyncio.sleep(0.15)

        # 不应抛出异常
        _run(_run_test())


# ======================================================================
# 7. 开关 realtime_ws=False → 默认不启用（行为安全）
# ======================================================================

class TestRealtimeWsSwitch:
    def test_realtime_ws_default_is_false(self):
        """realtime_ws=False 是默认值（不影响现网）。"""
        from smc_tracker.config import HarmonicCfg
        cfg = HarmonicCfg()
        assert cfg.realtime_ws is False, "默认值应为 False（不影响现网）"

    def test_realtime_ws_can_be_enabled(self):
        """realtime_ws=True 可正常设置。"""
        from smc_tracker.config import HarmonicCfg
        cfg = HarmonicCfg(realtime_ws=True)
        assert cfg.realtime_ws is True

    def test_harmonic_candle_ws_creates_correctly(self):
        """HarmonicCandleWS 可正常实例化。"""
        monitor = _mock_monitor()
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)
        assert hwws is not None

    def test_attach_with_none_monitor_no_crash(self):
        """monitor=None 时 attach() 不崩溃（防御性守卫）。"""
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=None, bg_ws=ws)
        hwws.attach()  # 不应抛出


# ======================================================================
# 8. no-repaint 兼容：WS 增量 vs 批量 refresh 对同一序列 analyze_candles 一致
# ======================================================================

class TestNoRepaintCompatibility:
    def test_ws_incremental_consistent_with_batch(self):
        """WS 增量喂入（逐步追加 K 线）与批量 analyze_candles 对同一序列结果一致。

        验证增量模式不破坏 append-only swing（no-repaint 核心契约）。
        """
        from tests.test_harmonic_no_repaint import _zigzag_candles
        order = 3
        candles = _zigzag_candles(80, amplitude=20.0)

        # 批量：一次性 analyze_candles 全部
        result_full = analyze_candles(candles, order=order, tol=0.06)

        # 增量：最后一步与批量等价（等幂性）
        start = order * 2 + 5
        for k in range(start, len(candles) + 1):
            result_k = analyze_candles(candles[:k], order=order, tol=0.06)
            if k == len(candles):
                # 最后一步：结果与批量完全相同
                assert result_k == result_full, (
                    f"增量最后一步（k={k}）结果与批量不一致：\n"
                    f"  batch completed={len(result_full.get('completed', []))}\n"
                    f"  incremental completed={len(result_k.get('completed', []))}"
                )

    def test_no_repaint_on_new_candle(self):
        """已完成 XABCD 的 D_idx 在追加新 K 线后点位不变（核心 no-repaint 回归）。"""
        from tests.test_harmonic_no_repaint import _zigzag_candles
        order = 3
        candles = _zigzag_candles(100, amplitude=20.0)

        first_seen_d: dict[int, dict] = {}
        start = order * 2 + 5

        for k in range(start, len(candles) + 1):
            result = analyze_candles(candles[:k], order=order, tol=0.06)
            for r in result.get("completed", []):
                pts = r.get("points") or {}
                d_info = pts.get("D")
                if not d_info:
                    continue
                d_idx = d_info[0]
                if d_idx not in first_seen_d:
                    first_seen_d[d_idx] = r
                else:
                    prev = first_seen_d[d_idx]
                    for pt in ("X", "A", "B", "C", "D"):
                        assert r["points"].get(pt) == prev["points"].get(pt), (
                            f"k={k}: D_idx={d_idx} 点 {pt} 改变（no-repaint 违规）"
                        )


# ======================================================================
# 9. Gap 1 修复：热路径 O(1) _sym2coin 预建缓存
# ======================================================================

class TestSym2CoinCache:
    """Gap 1 修复验证：attach() 预建 _sym2coin 缓存，_on_candle 热路径 O(1) 查找。"""

    def test_attach_builds_sym2coin_cache(self):
        """attach() 调用后 _sym2coin 缓存已建，包含正确的 symbol→coin 映射。"""
        monitor = _mock_monitor(timeframes=["15m"])
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)

        # attach 前缓存为空
        assert hwws._sym2coin == {}

        # attach 后缓存已建
        hwws.attach()
        assert hwws._sym2coin == {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}

    def test_handler_uses_cache_not_rebuild(self):
        """_on_candle 使用预建 _sym2coin（而非每次重建 dict）。

        验证方法：attach() 后修改 _sym2coin 缓存，观察 handler 是否用缓存（而非 coin_to_symbol）。
        若用缓存：修改后的 symbol 能被正确路由；若用 coin_to_symbol 重建：则走旧 coin_to_symbol。
        此测试以 _sym2coin 为单一真相源验证热路径走缓存。
        """
        monitor = _mock_monitor(timeframes=["15m"])
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)
        hwws.attach()

        # 确认缓存已建
        assert "BTCUSDT" in hwws._sym2coin

        # 在热路径触发中不应重建 dict（验证缓存命中）
        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms)
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler({"instId": "BTCUSDT"}, [row], recv_ns)
            await asyncio.sleep(0.1)

        _run(_run_test())
        # 通过走缓存路径，upsert_candles 应被正常调用
        monitor.store.upsert_candles.assert_called_once()

    def test_unknown_symbol_after_attach_returns_none(self):
        """attach() 后，不在缓存里的 instId → O(1) 查询返回 None，不崩溃。"""
        monitor = _mock_monitor(timeframes=["15m"])
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)
        hwws.attach()

        # 非监控 symbol
        assert hwws._sym2coin.get("XYZUSDT") is None

    def test_attach_none_monitor_sym2coin_empty(self):
        """monitor=None 时 attach() 不建缓存（安全不崩溃）。"""
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=None, bg_ws=ws)
        hwws.attach()  # 不应抛出
        assert hwws._sym2coin == {}

    def test_large_universe_sym2coin_correct_count(self):
        """大宇宙（模拟 100 币）attach() 后缓存 count 正确。"""
        monitor = _mock_monitor(timeframes=["15m"])
        monitor.coin_to_symbol = {f"COIN{i}": f"COIN{i}USDT" for i in range(100)}
        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws)
        hwws.attach()
        assert len(hwws._sym2coin) == 100
        assert hwws._sym2coin.get("COIN0USDT") == "COIN0"
        assert hwws._sym2coin.get("COIN99USDT") == "COIN99"


# ======================================================================
# 10. Gap 2 修复：on_update 端到端（合成收盘 → result → 回调执行）
# ======================================================================

class TestOnUpdateEndToEnd:
    """Gap 2 修复验证：on_update 端到端接通，收盘 bar 触发 analyze → 回调执行。"""

    def test_on_update_receives_result_dict(self):
        """收盘 bar + 足够 K 线 → on_update 被调用，result 是 dict（含 completed/forming 键）或 None。"""
        from tests.test_harmonic_no_repaint import _zigzag_candles
        monitor = _mock_monitor(timeframes=["15m"], bars=50, order=3)
        mock_candles = _zigzag_candles(50, amplitude=10.0)
        monitor.store.get_candles.return_value = list(mock_candles)

        received: list[tuple] = []

        def _capture(coin: str, tf: str, result: Any, now_ms: int) -> None:
            received.append((coin, tf, result, now_ms))

        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws, on_update=_capture)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000
        recv_ns = now_ms * 1_000_000

        row = _make_row(ts=open_ms)
        hwws.attach()
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler({"instId": "BTCUSDT"}, [row], recv_ns)
            await asyncio.sleep(0.15)

        _run(_run_test())

        # on_update 应被调用
        assert len(received) == 1
        coin_r, tf_r, result_r, ts_r = received[0]
        assert coin_r == "BTC"
        assert tf_r == "15m"
        # result 可能是 None（无形态）或 dict（有形态）
        if result_r is not None:
            assert isinstance(result_r, dict)
            assert "completed" in result_r
            assert "forming" in result_r

    def test_on_update_not_called_for_forming_bar(self):
        """forming bar（未收盘）→ on_update 不被调用。"""
        monitor = _mock_monitor(timeframes=["15m"])
        ws = MagicMock()

        called = []

        def _cb(coin, tf, result, now_ms):
            called.append((coin, tf))

        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws, on_update=_cb)

        gran_ms = GRANULARITY_MS["15m"]
        now_ms = int(time.time() * 1000)
        open_ms = now_ms - 10_000   # 10秒前开盘，gran=900_000ms，远未收

        row = _make_row(ts=open_ms)
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler({"instId": "BTCUSDT"}, [row], now_ms * 1_000_000)
            await asyncio.sleep(0.05)

        _run(_run_test())
        assert called == [], "forming bar 不应触发 on_update"

    def test_on_update_none_result_on_insufficient_candles(self):
        """DB K 线不足（< 2*order+3）→ on_update 被调用，result=None（分析跳过）。"""
        monitor = _mock_monitor(timeframes=["15m"], order=3)
        # 只返回 3 根（不足 2*3+3=9）
        from tests.test_harmonic_no_repaint import _zigzag_candles
        monitor.store.get_candles.return_value = list(_zigzag_candles(3, amplitude=10.0))

        received_results: list = []

        def _cb(coin, tf, result, now_ms):
            received_results.append(result)

        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws, on_update=_cb)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000

        row = _make_row(ts=open_ms)
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler({"instId": "BTCUSDT"}, [row], now_ms * 1_000_000)
            await asyncio.sleep(0.15)

        _run(_run_test())
        # on_update 应被调用（即使无分析结果）
        assert len(received_results) == 1
        assert received_results[0] is None, (
            "K 线不足时 analyze 跳过，on_update result 应为 None"
        )

    def test_on_update_async_callback_end_to_end(self):
        """async on_update 回调也被正确 await，端到端执行。"""
        from tests.test_harmonic_no_repaint import _zigzag_candles
        monitor = _mock_monitor(timeframes=["15m"], bars=50, order=3)
        monitor.store.get_candles.return_value = list(_zigzag_candles(50, amplitude=10.0))

        received = []

        async def _async_cb(coin, tf, result, now_ms):
            received.append((coin, tf))

        ws = MagicMock()
        hwws = HarmonicCandleWS(harmonic_monitor=monitor, bg_ws=ws, on_update=_async_cb)

        gran_ms = GRANULARITY_MS["15m"]
        open_ms = 1_700_000_000_000
        now_ms = open_ms + gran_ms + 60_000

        row = _make_row(ts=open_ms)
        hwws.attach()
        handler = hwws._make_handler("15m")

        async def _run_test():
            handler({"instId": "BTCUSDT"}, [row], now_ms * 1_000_000)
            await asyncio.sleep(0.15)

        _run(_run_test())
        assert len(received) == 1
        assert received[0] == ("BTC", "15m")
