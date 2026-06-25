"""Bitget K线 WS 增量驱动的谐波形态实时更新器。

设计目标：将谐波系统从「周期 refresh DB 缓存（非实时）」升级为「K 线 WS 收盘驱动的增量实时」。

核心流程：
  1. 订阅监控币种 × 周期的 Bitget candle WS channel（candle{tf}，如 candle15m/candle1H）。
  2. 收线（action=update + bar 已收盘）→ asyncio.to_thread 写 DB（不阻塞 event loop）。
  3. 落库后 schedule 该 (coin, tf) 的谐波增量 analyze_candles（复用现有函数，no-repaint）。
  4. 分析结果写 harmonic_setups（notify 回调可选，供 app 推送/落库）。

热路径纪律（CLAUDE.md §三-4）：
  - WS handler (_on_candle) 本身非阻塞：只解析 + 放入 _pending_set（set 去重，O(1)）。
  - 落库 + 分析走独立 asyncio.Task（create_task），非 WS 回调同步执行。
  - 未收盘 bar 不触发（bar_open_ms + gran_ms > recv_ns/1e6 即 forming bar，跳过）。

开关：HarmonicCfg.realtime_ws=False 默认，不影响现网（periodic refresh 保留作全量兜底）。

谐波实时性：收盘线即更新 setup（K 线级别），非 tick 级（forming tick 逼近是 B3）。
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Awaitable
from typing import Any

from ..bitget.rest import GRANULARITY_MS
from ..bitget.ws_client import BitgetSub, BitgetWSClient
from ..indicators.harmonic import analyze_candles
from ..models import Candle
from ..util import to_float as _f
from .candle_ingest import ingest_ws_closed_bar  # S3/A2：统一 WS 收盘 bar 落库路径

log = logging.getLogger("harmonic_candle_ws")

# WS channel 名 = "candle" + REST granularity（Bitget 协议实证，ws_client.py 注释）
# REST tf → WS channel 前缀保持一致（1m/5m/15m/1H/4H/6H/12H/1D/1W）
_TF_TO_CHANNEL: dict[str, str] = {
    tf: f"candle{tf}" for tf in GRANULARITY_MS
}

# 大周期（1D/1W/3D）收盘远超典型 update 频率，WS 推送可能极稀疏，正常
_BIG_TF = frozenset({"1D", "3D", "1W", "1M"})

# 每个 (coin, tf) 的分析任务最大并发数（防止密集更新堆积任务）
_MAX_CONCURRENT_ANALYSIS = 8


def _parse_candle_row(
    row: Any,
    coin: str,
    tf: str,
    gran_ms: int,
) -> Candle | None:
    """解析 Bitget candle WS 单行数据 → Candle（失败返回 None）。

    行格式（实证）：[ts_open_ms, o, h, l, c, baseVol, quoteVol]
    ts 字段为字符串或整数（与 REST 格式一致）。
    """
    try:
        if not row or len(row) < 6:
            return None
        ts = int(float(row[0]))
        o = _f(row[1])
        h = _f(row[2])
        l = _f(row[3])
        c = _f(row[4])
        v = _f(row[5])
        # 数据质量守卫（CLAUDE.md §三-3）：任意价格非有限 or <=0 → 跳过
        if any(x is None or not math.isfinite(x) or x <= 0.0 for x in (o, h, l, c)):
            return None
        if v is None or not math.isfinite(v):
            v = 0.0
        return Candle(
            coin=coin,
            interval=tf,
            open_time_ms=ts,
            close_time_ms=ts + gran_ms,
            o=o, h=h, l=l, c=c, v=v,
            n=0,
        )
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _is_bar_closed(open_ms: int, gran_ms: int, now_ms: int) -> bool:
    """判断 K 线是否已收盘（收盘时间 <= 当前时间）。

    收盘时间 = open_ms + gran_ms。允许 1 秒的时钟容差（WS 推送可能比本地时钟稍快）。
    """
    close_ms = open_ms + gran_ms
    return close_ms <= now_ms + 1000  # 1s 容差


class HarmonicCandleWS:
    """Bitget K 线 WS 增量驱动谐波分析器。

    Attributes:
        harmonic_monitor:  已建好的 HarmonicMonitor 实例（复用其 store/order/tol/bars）。
        bg_ws:             BitgetWSClient 实例（订阅 candle channel）。
        on_update:         可选回调 (coin, tf, result, now_ms) → None|Awaitable，
                           分析完成后调用（供 app 落库/推送）。默认 None（静默更新 DB）。
    """
    __slots__ = (
        "_monitor", "_bg_ws", "_on_update",
        "_sema", "_pending", "_tasks",
        "_sym2coin",   # Gap 1: attach() 预建 symbol→coin 反向映射缓存（热路径 O(1) 查找）
    )

    def __init__(
        self,
        harmonic_monitor: Any,     # HarmonicMonitor（鸭子类型，避免循环导入）
        bg_ws: BitgetWSClient,
        on_update: Callable[[str, str, dict | None, int], Any] | None = None,
    ) -> None:
        self._monitor = harmonic_monitor
        self._bg_ws = bg_ws
        self._on_update = on_update
        # 分析并发限制（_MAX_CONCURRENT_ANALYSIS 个任务同时运行）
        self._sema = asyncio.Semaphore(_MAX_CONCURRENT_ANALYSIS)
        # 待处理 (coin, tf) 去重集（WS 回调不创建 Task，只登记，避免热路径阻塞）
        self._pending: set[tuple[str, str]] = set()
        # 持有 Task 引用，防 GC
        self._tasks: set[asyncio.Task] = set()
        # Gap 1: symbol→coin 预建缓存（attach() 时一次性建，热路径 O(1)）。
        # 全永续 universe 在 _seed 后固定（不动态扩缩），coin_to_symbol 变化须重新 attach()。
        self._sym2coin: dict[str, str] = {}

    def attach(self) -> None:
        """订阅所有 (coin, tf) 的 candle WS channel，挂载 handler。

        每个 tf 只注册一次 handler（BitgetWSClient.subscribe 已去重）；
        每个 tf 的所有 symbol 订阅一起，handler 内按 arg["instId"] 区分 symbol。

        Gap 1 修复：attach() 调用时预建 _sym2coin 缓存（symbol→coin 反向映射），
        供 _on_candle 热路径 O(1) 查找。coin_to_symbol 变化须重新调用 attach()；
        全永续 universe 在 _seed 后固定，此假设成立。
        """
        monitor = self._monitor
        if monitor is None:
            return

        # Gap 1: 预建 symbol→coin 反向映射（一次性，O(n)；热路径 _on_candle 用 O(1) 查）
        self._sym2coin = {symbol: coin for coin, symbol in monitor.coin_to_symbol.items()}

        # 按 tf 分组订阅（每 tf 一个 handler）
        for tf in monitor.timeframes:
            channel = _TF_TO_CHANNEL.get(tf)
            if channel is None:
                log.warning("谐波 WS：未知周期 %s，跳过 WS 订阅", tf)
                continue

            # 只注册一次 handler（BitgetWSClient.subscribe 内去重，但 handler bound method 不同 tf 不同）
            handler = self._make_handler(tf)

            for coin, symbol in monitor.coin_to_symbol.items():
                sub = BitgetSub(channel=channel, inst_id=symbol)
                self._bg_ws.subscribe(sub, handler)

        log.info(
            "谐波 K线 WS 已订阅：%d 币 × %d 周期，sym2coin 缓存 %d 条",
            len(monitor.coin_to_symbol), len(monitor.timeframes), len(self._sym2coin),
        )

    def _make_handler(self, tf: str) -> Callable:
        """为指定 tf 创建 WS 推送回调（闭包捕获 tf）。"""
        gran_ms = GRANULARITY_MS.get(tf, 0)

        def _on_candle(arg: dict, data: list, recv_ns: int) -> None:
            """Bitget candle WS 推送回调（非阻塞热路径）。

            仅解析 + 判断收盘 + 登记 _pending，不执行 DB 写/分析（异步 Task 处理）。

            Gap 1 修复：改用 self._sym2coin（attach() 预建缓存），O(1) 查找。
            不再每次重建 {v:k for k,v in monitor.coin_to_symbol.items()}（原 O(n) 违纪）。
            若 attach() 未调（如单测直接调 _make_handler），惰性一次性建缓存（保向后兼容）。
            """
            inst_id: str = arg.get("instId", "")
            # Gap 1: O(1) 查找（attach() 预建缓存）。
            # 惰性兜底：attach() 未调时一次性建（仅发生在测试场景；正式生产 attach() 必先调用）。
            monitor = self._monitor
            if not self._sym2coin and monitor is not None:
                self._sym2coin = {sym: c for c, sym in monitor.coin_to_symbol.items()}
            coin = self._sym2coin.get(inst_id)
            if coin is None:
                return  # 非监控币，忽略

            now_ms = int(recv_ns / 1_000_000) if recv_ns else int(time.time() * 1000)

            for row in (data or []):
                candle = _parse_candle_row(row, coin, tf, gran_ms)
                if candle is None:
                    continue
                # 仅处理已收盘 bar（forming bar 跳过，不触发重计算）
                if not _is_bar_closed(candle.open_time_ms, gran_ms, now_ms):
                    log.debug("谐波 WS 跳过未收盘 bar %s/%s ts=%d", coin, tf, candle.open_time_ms)
                    continue
                # 登记待处理：去重（同 coin/tf 已在队列则跳过，防短期重复触发）
                key = (coin, tf)
                if key not in self._pending:
                    self._pending.add(key)
                    # 创建异步 Task 执行 DB 写 + 谐波分析（不阻塞 WS event loop）
                    t = asyncio.create_task(
                        self._process_closed_bar(coin, tf, candle, now_ms),
                        name=f"harmonic_ws_{coin}_{tf}",
                    )
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)

        return _on_candle

    async def _process_closed_bar(
        self, coin: str, tf: str, candle: Candle, now_ms: int
    ) -> None:
        """收盘 bar 异步处理：DB 写 + 谐波增量分析 + 可选回调。

        该方法在独立 Task 中运行（非 WS 热路径）。
        _sema 控制并发，防止密集 WS 触发堆积分析任务。
        """
        key = (coin, tf)
        async with self._sema:
            try:
                # 1. 增量写 DB：统一走 ingest_ws_closed_bar（A2：消除双写不一致）
                # ingest_ws_closed_bar 内部调用 _clean_candles + store.upsert_candles，
                # 与 REST 回填路径完全一致，不再直接调 upsert_candles。
                monitor = self._monitor
                if monitor is not None and monitor.store is not None:
                    ok = await asyncio.to_thread(
                        ingest_ws_closed_bar, candle, monitor.store
                    )
                    if ok:
                        log.debug(
                            "谐波 WS 落库 %s/%s ts=%d c=%s",
                            coin, tf, candle.open_time_ms, candle.c,
                        )
                    else:
                        log.debug(
                            "谐波 WS 脏数据已过滤，跳过落库 %s/%s ts=%d",
                            coin, tf, candle.open_time_ms,
                        )

                # 2. A3：若 monitor 有对应 HarmonicState，增量 update（提供实时增量结果）。
                # update() 返回快照与全量 analyze_candles 一致（由 HarmonicMonitor._fetch_tf 守卫）；
                # 此处调用后的 snapshot 只用于辅助日志，实际 result 仍来自步骤 3（DB 读取 + analyze）。
                _hs = None
                if monitor is not None and hasattr(monitor, "_states"):
                    _hs = monitor._states.get((coin, tf))
                    if _hs is not None:
                        try:
                            _hs.update(candle)  # 增量喂入，不读其返回值（result 来自下方全量路径）
                        except Exception:  # noqa: BLE001
                            log.warning("谐波 WS HarmonicState.update 失败 %s/%s", coin, tf)

                # 3. 从 DB 读取该 (coin, tf) 最新 K 线序列，执行增量谐波分析
                result: dict | None = None
                if monitor is not None and monitor.store is not None:
                    candles = await asyncio.to_thread(
                        monitor.store.get_candles, coin, tf, monitor.bars
                    )
                    if len(candles) >= 2 * monitor.order + 3:
                        # analyze_candles 是 CPU 密集（纯 Python），to_thread 避免阻塞 event loop
                        result = await asyncio.to_thread(
                            analyze_candles, candles, monitor.order, monitor.tol
                        )
                        log.debug(
                            "谐波 WS 增量分析 %s/%s: completed=%d forming=%d",
                            coin, tf,
                            len((result or {}).get("completed") or []),
                            len((result or {}).get("forming") or []),
                        )

                # 3. 可选回调（供 app 落库/推送，异步友好）
                if self._on_update is not None:
                    try:
                        ret = self._on_update(coin, tf, result, now_ms)
                        if isinstance(ret, Awaitable):
                            await ret
                    except Exception:  # noqa: BLE001
                        log.exception("谐波 WS on_update 回调出错 %s/%s", coin, tf)

            except Exception:  # noqa: BLE001
                log.exception("谐波 WS 增量处理出错 %s/%s", coin, tf)
            finally:
                self._pending.discard(key)
