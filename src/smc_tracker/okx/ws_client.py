"""OKX V5 公共 WebSocket 客户端（低延迟，无 API key）。

实证协议（wss://ws.okx.com:8443/ws/v5/public，2026-06-22 实测）：
- 订阅：{"op":"subscribe","args":[{"channel":"trades","instId":"BTC-USDT-SWAP"}]}（arg 无 instType）
- 保活：每 ~25s 发送**文本 "ping"**，服务器回**文本 "pong"**（30s 不发会被断开）。
- 推送：{"arg":{"channel","instId"},"data":[...]}；订阅确认 {"event":"subscribe","arg":{...}}；
  错误 {"event":"error","code","msg"}。
- 可订阅永续实时频道：trades(带 side) / tickers / open-interest / mark-price / funding-rate。

结构与 `bitget/ws_client.py` 同构（重连 + 心跳 + 看门狗 + handler 按 channel 分发），
差异仅 URL / arg 无 instType / 分发逻辑抽成可测的 `_handle_raw`。
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

log = logging.getLogger("okx.ws")

# handler(arg: dict, data: list, recv_ns: int)
Handler = Callable[[dict, list, int], Any]

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"


@dataclass(frozen=True, slots=True)
class OKXSub:
    channel: str                       # "trades" / "open-interest" / "mark-price" / "tickers" ...
    inst_id: str                       # "BTC-USDT-SWAP"（单 inst 订阅）
    inst_type: str = ""                # "SWAP" 等（firehose 全市场订阅，如 liquidation-orders）

    def to_arg(self) -> dict[str, str]:
        # inst_id 非空 → 单 inst 订阅 {channel, instId}（现状不变，向后兼容）；
        # inst_id 为空且 inst_type 非空 → 全市场订阅 {channel, instType}（强平等 firehose 频道）。
        if self.inst_id:
            return {"channel": self.channel, "instId": self.inst_id}
        if self.inst_type:
            return {"channel": self.channel, "instType": self.inst_type}
        return {"channel": self.channel}


class OKXWSClient:
    def __init__(self, ws_url: str = WS_URL, ping_interval_sec: float = 25.0,
                 reconnect_max_backoff_sec: float = 30.0) -> None:
        self.ws_url = ws_url
        self.ping_interval_sec = ping_interval_sec
        self.reconnect_max_backoff_sec = reconnect_max_backoff_sec
        self._subs: set[OKXSub] = set()
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._conn: websockets.ClientConnection | None = None
        self._running = False
        self._tasks: set[asyncio.Task] = set()    # 持有 create_task 引用，防 GC
        self._last_pong_ns = 0                     # 上次收到 pong 的单调时间(看门狗)

    def subscribe(self, sub: OKXSub, handler: Handler) -> None:
        # handler 去重：推送 arg 自带 instId，单份 handler 即可处理全部 symbol，
        # 否则同一 bound method 注册 N 份会令每条推送被处理 N 次（统计 N 倍放大）。
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
                    log.info("OKX WS 已连接")
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
                    log.warning("OKX WS 异常: %s", e)
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

    async def _send(self, subs: list[OKXSub]) -> None:
        if self._conn is None or not subs:
            return
        msg = {"op": "subscribe", "args": [s.to_arg() for s in subs]}
        try:
            await asyncio.wait_for(self._conn.send(orjson.dumps(msg).decode()), timeout=10)
        except Exception as e:  # noqa: BLE001 — 超时/断链不阻塞，由 run() 重连
            log.warning("OKX 订阅发送失败/超时: %s", e)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval_sec)
            if self._conn is None:
                continue
            # pong 看门狗：超过 2×ping 周期未收到 pong → 判定半死链，主动关连接触发重连。
            if self._last_pong_ns and (
                    time.monotonic_ns() - self._last_pong_ns
                    > 2 * self.ping_interval_sec * 1e9):
                log.warning("OKX WS 超 %ds 无 pong，主动重连", int(2 * self.ping_interval_sec))
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
            await self._handle_raw(raw, time.monotonic_ns())

    async def _handle_raw(self, raw: str | bytes, recv_ns: int) -> None:
        """处理单条 WS 原始消息：pong 喂看门狗 / event 忽略 / data 按 channel 分发。

        从 _recv_loop 抽出便于单测（喂构造消息直接验证分发，无需真实连接）。
        """
        if raw == "pong" or raw == b"pong":
            self._last_pong_ns = recv_ns       # 喂看门狗
            return
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return
        ev = msg.get("event")
        if ev == "error":
            log.warning("OKX 订阅错误: %s", msg)
            return
        if ev in ("subscribe", "unsubscribe"):
            return
        arg = msg.get("arg") or {}
        channel = arg.get("channel")
        data = msg.get("data") or []
        if not channel:
            return
        for h in self._handlers.get(channel, ()):
            try:
                res = h(arg, data, recv_ns)
                if isinstance(res, Awaitable):
                    await res
            except Exception:  # noqa: BLE001
                log.exception("OKX handler 出错 channel=%s", channel)
