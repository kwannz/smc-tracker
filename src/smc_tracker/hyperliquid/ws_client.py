"""Hyperliquid 异步 WebSocket 客户端（低延迟）。

设计要点：
- asyncio + websockets，全程非阻塞；orjson 解析（比 stdlib json 快约 3x）。
- 自动重连（指数退避）+ 断线后自动重订阅。
- 心跳保活：每 ping_interval 秒发送 {"method":"ping"}（Hyperliquid 要求 <60s 否则断开）。
- 按 channel 分发到注册的 handler；handler 可为同步或异步函数。
- 接收即打单调时间戳 recv_ns，便于端到端延迟统计。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import orjson
import websockets

from .constants import MAINNET_WS

log = logging.getLogger("hl.ws")

# handler 签名：handler(data: Any, recv_ns: int) -> None | Awaitable[None]
Handler = Callable[[Any, int], Any]

# 连接存活 ≥ 此秒数才视为「稳定」→ 断开后重置退避；否则继续指数增长（防重连风暴）
_STABLE_CONN_SEC = 30.0


def _reconnect_backoff(
    conn_elapsed_sec: float, current_backoff: float, max_backoff: float,
    stable_sec: float = _STABLE_CONN_SEC,
) -> tuple[float, float]:
    """重连退避决策（纯函数，可测）：返回 (本次 sleep 秒数, 下次退避基数)。

    根因修复（防重连风暴）：**不在「连接成功」时重置退避，而在「连接稳定」时**。
    server 接受连接后立即断（限流/维护）的失败模式下，若每次连上即重置退避，会形成
    1s 间隔无限重连风暴（自我 DoS）。本函数仅当连接存活 ≥ stable_sec 才重置为 1.0，
    否则保持 current_backoff 继续指数增长（×2，封顶 max_backoff）。
    """
    base = 1.0 if conn_elapsed_sec >= stable_sec else current_backoff
    return base, min(base * 2.0, max_backoff)


@dataclass(frozen=True, slots=True)
class Subscription:
    """一个 WS 订阅。type 必填，其余按订阅类型可选。"""
    type: str
    coin: str | None = None
    user: str | None = None
    interval: str | None = None

    def to_payload(self) -> dict[str, Any]:
        p: dict[str, Any] = {"type": self.type}
        if self.coin is not None:
            p["coin"] = self.coin
        if self.user is not None:
            p["user"] = self.user
        if self.interval is not None:
            p["interval"] = self.interval
        return p


class HyperliquidWSClient:
    def __init__(
        self,
        ws_url: str = MAINNET_WS,
        ping_interval_sec: float = 50.0,
        reconnect_max_backoff_sec: float = 30.0,
    ) -> None:
        self.ws_url = ws_url
        self.ping_interval_sec = ping_interval_sec
        self.reconnect_max_backoff_sec = reconnect_max_backoff_sec

        self._subs: set[Subscription] = set()
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._conn: websockets.ClientConnection | None = None
        self._running = False
        self._connected_evt = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()   # 持有 create_task 引用，防被 GC 取消

    # ---- 订阅注册 ----------------------------------------------------------
    def subscribe(self, sub: Subscription, handler: Handler) -> None:
        """注册订阅及其 handler。可在连接前后调用；若已连接则立即发送订阅。

        关键：handler 按 channel(sub.type) 分桶，且**去重**——同一 channel 的消息体自带
        coin/user 字段，handler 自行区分，单份即可处理全部订阅；若不去重，按币/址各注册一份
        会使每条消息被同一 handler 分发 N 次，导致净流向/成交累积器 N 倍失真。
        """
        if handler not in self._handlers[sub.type]:
            self._handlers[sub.type].append(handler)
        if sub not in self._subs:
            self._subs.add(sub)
            if self._conn is not None:
                t = asyncio.create_task(self._send_subscribe(sub))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

    def on(self, channel: str, handler: Handler) -> None:
        """仅注册某 channel 的 handler，不新增订阅（用于一对多分发）。去重防重复分发。"""
        if handler not in self._handlers[channel]:
            self._handlers[channel].append(handler)

    # ---- 运行主循环 --------------------------------------------------------
    async def run(self) -> None:
        """连接并持续接收，断线自动重连。阻塞直到 stop()。"""
        self._running = True
        backoff = 1.0
        while self._running:
            conn_start = time.monotonic()    # 本轮连接(含 connect 尝试)起始，用于稳定性判定
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=None,        # 用 Hyperliquid 自定义 ping，关闭 ws 协议 ping
                    max_queue=1024,
                    open_timeout=10,
                ) as conn:
                    self._conn = conn
                    self._connected_evt.set()
                    # 不在此处重置退避——避免「连上即断」时退避被反复清零形成重连风暴；
                    # 退避在断开后按连接存活时长决定（见 _reconnect_backoff）。
                    log.info("WS 已连接 %s", self.ws_url)
                    await self._resubscribe_all()
                    ping_task = asyncio.create_task(self._ping_loop())
                    try:
                        await self._recv_loop()
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 网络层任意异常都重连
                if self._running:        # 主动 stop() 引发的关闭不告警
                    log.warning("WS 连接异常: %s", e)
            finally:
                self._conn = None
                self._connected_evt.clear()
            if not self._running:
                break
            # 稳定连接(存活≥_STABLE_CONN_SEC)断开 → 退避重置；瞬断 → 继续指数增长(防风暴)
            elapsed = time.monotonic() - conn_start
            sleep_sec, backoff = _reconnect_backoff(
                elapsed, backoff, self.reconnect_max_backoff_sec)
            log.info("WS 重连中（退避 %.1fs，本轮连接存活 %.0fs）…", sleep_sec, elapsed)
            await asyncio.sleep(sleep_sec)

    async def stop(self) -> None:
        self._running = False
        for t in list(self._tasks):       # 取消未决的订阅任务
            t.cancel()
        self._tasks.clear()
        if self._conn is not None:
            await self._conn.close()

    async def wait_connected(self) -> None:
        await self._connected_evt.wait()

    # ---- 内部 --------------------------------------------------------------
    async def _send_subscribe(self, sub: Subscription) -> None:
        if self._conn is None:
            return
        msg = {"method": "subscribe", "subscription": sub.to_payload()}
        # 关键：Hyperliquid WS 只接受文本帧，orjson.dumps 返回 bytes 需 decode 成 str。
        # send 加超时：写缓冲满(对端慢)时不会无限挂起阻塞重订阅/接收链路。
        try:
            await asyncio.wait_for(self._conn.send(orjson.dumps(msg).decode()), timeout=10)
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            log.warning("订阅发送失败/超时 %s: %s", sub.to_payload(), e)
            return
        log.debug("订阅 %s", sub.to_payload())

    async def _resubscribe_all(self) -> None:
        for sub in self._subs:
            await self._send_subscribe(sub)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval_sec)
            if self._conn is not None:
                try:
                    await asyncio.wait_for(
                        self._conn.send('{"method":"ping"}'), timeout=10)  # 文本帧
                except Exception:  # noqa: BLE001 — 超时/断链均退出，由 run() 重连
                    return

    async def _recv_loop(self) -> None:
        assert self._conn is not None
        async for raw in self._conn:
            recv_ns = time.monotonic_ns()
            try:
                msg = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            channel = msg.get("channel")
            if channel is None or channel == "pong":
                continue
            if channel == "subscriptionResponse":
                continue
            await self._dispatch(channel, msg.get("data"), recv_ns)

    async def _dispatch(self, channel: str, data: Any, recv_ns: int) -> None:
        handlers = self._handlers.get(channel)
        if not handlers:
            return
        for h in handlers:
            try:
                res = h(data, recv_ns)
                if isinstance(res, Awaitable):
                    await res
            except Exception:  # noqa: BLE001 — 单个 handler 异常不影响接收循环
                log.exception("handler 处理 channel=%s 出错", channel)
