"""Bitget V2 公共 WebSocket 客户端（低延迟）。

实证协议（wss://ws.bitget.com/v2/ws/public）：
- 订阅：{"op":"subscribe","args":[{"instType":"USDT-FUTURES","channel":"ticker","instId":"DOGEUSDT"}]}
- 保活：每 ~25s 发送文本 "ping"，服务器回文本 "pong"（30s 不发会被断开）。
- 推送：{"action":"snapshot"|"update","arg":{instType,channel,instId},"data":[...]}
- 订阅确认：{"event":"subscribe","arg":{...}}；错误：{"event":"error","code","msg"}
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

log = logging.getLogger("bitget.ws")

# handler(arg: dict, data: list, recv_ns: int)
Handler = Callable[[dict, list, int], Any]

WS_URL = "wss://ws.bitget.com/v2/ws/public"


@dataclass(frozen=True, slots=True)
class BitgetSub:
    channel: str                       # "ticker" / "trade" / "candle1m" ...
    inst_id: str                       # "DOGEUSDT"
    inst_type: str = "USDT-FUTURES"

    def to_arg(self) -> dict[str, str]:
        return {"instType": self.inst_type, "channel": self.channel, "instId": self.inst_id}


class BitgetWSClient:
    def __init__(self, ws_url: str = WS_URL, ping_interval_sec: float = 25.0,
                 reconnect_max_backoff_sec: float = 30.0) -> None:
        self.ws_url = ws_url
        self.ping_interval_sec = ping_interval_sec
        self.reconnect_max_backoff_sec = reconnect_max_backoff_sec
        self._subs: set[BitgetSub] = set()
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._conn: websockets.ClientConnection | None = None
        self._running = False
        self._tasks: set[asyncio.Task] = set()    # 持有 create_task 引用，防 GC
        self._last_pong_ns = 0                     # 上次收到 pong 的单调时间(看门狗)

    def subscribe(self, sub: BitgetSub, handler: Handler) -> None:
        # handler 去重：推送 arg 自带 instId，handler 自行区分 symbol，单份即可处理全部；
        # 若按 symbol 各注册一份(同一 bound method)，每条推送会被处理 N 次 → 统计/推送 N 倍放大。
        if handler not in self._handlers[sub.channel]:
            self._handlers[sub.channel].append(handler)
        if sub not in self._subs:
            self._subs.add(sub)
            if self._conn is not None:
                t = asyncio.create_task(self._send([sub]))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=None,
                                              max_queue=2048, open_timeout=10) as conn:
                    self._conn = conn
                    backoff = 1.0
                    self._last_pong_ns = time.monotonic_ns()   # 连上即重置看门狗
                    log.info("Bitget WS 已连接")
                    await self._send(list(self._subs))
                    ping = asyncio.create_task(self._ping_loop())
                    try:
                        await self._recv_loop()
                    finally:
                        ping.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if self._running:
                    log.warning("Bitget WS 异常: %s", e)
            finally:
                self._conn = None
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max_backoff_sec)

    async def stop(self) -> None:
        self._running = False
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        if self._conn is not None:
            await self._conn.close()

    async def _send(self, subs: list[BitgetSub]) -> None:
        if self._conn is None or not subs:
            return
        msg = {"op": "subscribe", "args": [s.to_arg() for s in subs]}
        try:
            await asyncio.wait_for(self._conn.send(orjson.dumps(msg).decode()), timeout=10)
        except Exception as e:  # noqa: BLE001 — 超时/断链不阻塞，由 run() 重连
            log.warning("Bitget 订阅发送失败/超时: %s", e)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval_sec)
            if self._conn is None:
                continue
            # pong 看门狗：超过 2×ping 周期未收到 pong → 判定半死链，主动关连接触发重连。
            if self._last_pong_ns and (
                    time.monotonic_ns() - self._last_pong_ns
                    > 2 * self.ping_interval_sec * 1e9):
                log.warning("Bitget WS 超 %ds 无 pong，主动重连", int(2 * self.ping_interval_sec))
                try:
                    await self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                await asyncio.wait_for(self._conn.send("ping"), timeout=10)  # 文本 "ping"
            except Exception:  # noqa: BLE001
                return

    async def _recv_loop(self) -> None:
        assert self._conn is not None
        async for raw in self._conn:
            recv_ns = time.monotonic_ns()
            if raw == "pong" or raw == b"pong":
                self._last_pong_ns = recv_ns       # 喂看门狗
                continue
            try:
                msg = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            ev = msg.get("event")
            if ev == "error":
                log.warning("Bitget 订阅错误: %s", msg)
                continue
            if ev == "subscribe":
                continue
            arg = msg.get("arg") or {}
            channel = arg.get("channel")
            data = msg.get("data") or []
            if not channel:
                continue
            for h in self._handlers.get(channel, ()):
                try:
                    res = h(arg, data, recv_ns)
                    if isinstance(res, Awaitable):
                        await res
                except Exception:  # noqa: BLE001
                    log.exception("Bitget handler 出错 channel=%s", channel)
