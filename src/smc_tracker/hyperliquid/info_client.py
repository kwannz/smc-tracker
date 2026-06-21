"""Hyperliquid REST Info 客户端（POST /info）。

用于 WS 之外的快照/历史拉取：合约元数据、地址持仓、历史成交、K 线回填。
低延迟：复用 aiohttp 连接池 + orjson；解析为本项目数据模型。
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import orjson

from ..models import Candle, Fill, Position, Side
from .constants import MAINNET_REST, VALID_INTERVALS

log = logging.getLogger("hl.info")


from ..util import to_float as _f  # 统一安全数值解析


class HyperliquidInfo:
    def __init__(self, rest_url: str = MAINNET_REST) -> None:
        self.url = rest_url.rstrip("/") + "/info"
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "HyperliquidInfo":
        # 分离连接超时(快失败)与总超时(容忍大 payload)：活跃巨鲸的 userFills 响应可达数 MB，
        # 原 total=10 偏紧会超时丢数据(#51 审计发现:poll 对 15 庄调 user_fills,超时即丢该庄流向)。
        self._session = aiohttp.ClientSession(
            json_serialize=lambda o: orjson.dumps(o).decode(),
            timeout=aiohttp.ClientTimeout(total=30, sock_connect=8),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session is not None:
            await self._session.close()

    async def _post(self, body: dict[str, Any]) -> Any:
        assert self._session is not None, "需在 async with 上下文中使用"
        async with self._session.post(self.url, data=orjson.dumps(body),
                                       headers={"Content-Type": "application/json"}) as resp:
            resp.raise_for_status()
            return orjson.loads(await resp.read())

    # ---- 元数据 ----
    async def meta(self) -> dict[str, Any]:
        """perp 合约宇宙（含每个 coin 的 szDecimals 等）。"""
        return await self._post({"type": "meta"})

    async def all_mids(self) -> dict[str, str]:
        """所有 coin 的中间价。"""
        return await self._post({"type": "allMids"})

    # ---- 地址状态 ----
    async def clearinghouse_state(self, user: str) -> dict[str, Any]:
        return await self._post({"type": "clearinghouseState", "user": user})

    async def positions(self, user: str) -> list[Position]:
        """解析地址当前所有非空 perp 持仓。"""
        state = await self.clearinghouse_state(user)
        out: list[Position] = []
        for ap in state.get("assetPositions", []):
            p = ap.get("position", {})
            szi = _f(p.get("szi"))
            if szi == 0:
                continue
            lev = p.get("leverage", {})
            out.append(Position(
                coin=p.get("coin", ""),
                szi=szi,
                entry_px=_f(p.get("entryPx")),
                position_value=_f(p.get("positionValue")),
                unrealized_pnl=_f(p.get("unrealizedPnl")),
                leverage=_f(lev.get("value")) if isinstance(lev, dict) else 0.0,
                liquidation_px=_f(p.get("liquidationPx")) if p.get("liquidationPx") else None,
            ))
        return out

    async def user_fills(self, user: str) -> list[Fill]:
        """地址近期成交回报（最近约 2000 笔）。"""
        data = await self._post({"type": "userFills", "user": user})
        return [self._parse_fill(f, user) for f in (data or [])]

    async def user_fills_by_time(
        self,
        user: str,
        start_ms: int,
        end_ms: int | None = None,
        max_pages: int = 20,
    ) -> list[Fill]:
        """分页拉取地址历史成交（按时间升序）。

        HL userFillsByTime 每页约 2000 笔按 time 升序返回；满页则继续分页，
        用最后一笔 time+1 作新 startTime，最多 max_pages 页。
        去重依据：(hash, oid)——HL fill 两者唯一标识一笔。
        单页异常 try/except 后停止分页并返回已得数据（降级不丢已有数据）。

        :param user:      HL 地址
        :param start_ms:  开始时间（毫秒 Unix 时间戳）
        :param end_ms:    结束时间（None=不传 endTime，即拉到当前）
        :param max_pages: 最多分页次数（防止无限循环）
        :return:          按 time_ms 升序、去重后的 Fill 列表
        """
        seen: set[tuple] = set()        # (hash, oid) 去重集合
        result: list[Fill] = []
        cur_start = start_ms

        for _page in range(max_pages):
            body: dict = {"type": "userFillsByTime", "user": user, "startTime": cur_start}
            if end_ms is not None:
                body["endTime"] = end_ms
            try:
                data = await self._post(body)
            except Exception as exc:   # noqa: BLE001
                log.warning("user_fills_by_time 第 %d 页拉取失败(%s): %s", _page + 1,
                            user[:10], exc)
                break

            page_fills = data or []
            if not page_fills:
                break

            for f in page_fills:
                fill = self._parse_fill(f, user)
                key = (fill.hash, fill.oid)
                if key not in seen:
                    seen.add(key)
                    result.append(fill)

            # 若本页不满 2000 笔，说明已是最后一页
            if len(page_fills) < 2000:
                break

            # 继续：从最后一笔 time+1 开始下一页
            last_ms = int(page_fills[-1].get("time", 0)) if isinstance(page_fills[-1], dict) else 0
            if last_ms <= 0:
                # 解析 Fill 时已用 time_ms，尝试从已解析的最后一笔取
                last_ms = result[-1].time_ms if result else 0
            if last_ms <= 0 or last_ms < cur_start:
                break
            cur_start = last_ms + 1

        # 确保最终列表按 time_ms 升序
        result.sort(key=lambda f: f.time_ms)
        return result

    @staticmethod
    def _parse_fill(f: dict[str, Any], user: str) -> Fill:
        return Fill(
            coin=f.get("coin", ""),
            side=Side.from_hl(f.get("side", "B")),
            px=_f(f.get("px")),
            sz=_f(f.get("sz")),
            time_ms=int(f.get("time", 0)),
            start_position=_f(f.get("startPosition")),
            dir=f.get("dir", ""),
            closed_pnl=_f(f.get("closedPnl")),
            hash=f.get("hash", ""),
            oid=int(f.get("oid", 0)),
            crossed=bool(f.get("crossed", False)),
            address=user,
        )

    # ---- 历史 K 线 ----
    async def candle_snapshot(self, coin: str, interval: str,
                              start_ms: int, end_ms: int) -> list[Candle]:
        if interval not in VALID_INTERVALS:        # 数据质量校验
            raise ValueError(f"无效 K 线周期 {interval}，应为 {VALID_INTERVALS}")
        body = {"type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval,
                        "startTime": start_ms, "endTime": end_ms}}
        data = await self._post(body)
        out: list[Candle] = []
        for c in (data or []):
            out.append(Candle(
                coin=c.get("s", coin),
                interval=c.get("i", interval),
                open_time_ms=int(c.get("t", 0)),
                close_time_ms=int(c.get("T", 0)),
                o=_f(c.get("o")), h=_f(c.get("h")),
                l=_f(c.get("l")), c=_f(c.get("c")),
                v=_f(c.get("v")), n=int(c.get("n", 0)),
            ))
        return out
