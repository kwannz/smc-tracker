"""Blockstream BTC HTTP 客户端（从 exchange_flow.py 拆出，降行数；CLAUDE.md 模块化扁平 ≤800）。

封装 blockstream.info 公开 REST API（无 key），供 exchange_flow.btc_flow_24h 消费。
自包含：仅依赖 aiohttp/asyncio + 两个本模块常量，零其它 exchange_flow 耦合。
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger("onchain.exchange_flow")

# blockstream.info 公开端点（无 key，部分地区可能需要代理）
_DEFAULT_BASE_URL = "https://blockstream.info/api"

# BTC 分页上限（#97 由 6→20 缓解热钱包低估）：覆盖约 20×25=500 笔已确认 tx/24h。
# 分页本就按 24h 窗口边界早停（见 address_txs_window），普通钱包仍只翻数页；提高上限仅对
# 真·高频热钱包多翻几页，把低估阈值从 >150 笔抬到 >500 笔，显著缓解（每页 0.2s 节流约束速率）。
_BTC_MAX_PAGES = 20


class BlockstreamClient:
    """封装 blockstream.info 公开 REST API，支持外部注入 aiohttp.ClientSession（便于测试）。

    base_url 可在测试中替换为 mock 服务器地址，生产用默认值。
    """

    def __init__(self, base_url: str = _DEFAULT_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    async def address_txs_window(
        self,
        session: aiohttp.ClientSession,
        addr: str,
        now_ms: int,
        window_ms: int = 86_400_000,
        max_pages: int = _BTC_MAX_PAGES,
    ) -> list[dict]:
        """分页抓取地址近 window_ms 内的 tx，最多 max_pages 页（每页 ≤25 笔已确认 tx）。

        分页逻辑：
          1. 先 GET /address/{addr}/txs 取首页，加入累积列表 acc。
          2. 循环（最多 max_pages 轮）：
             - 若上页为空则停（无更早数据）。
             - 取上页最后一笔已确认 tx 的 txid 与 block_time；
               若 block_time*1000 < now_ms-window_ms（已翻过窗口边界）则停。
             - 否则 GET /address/{addr}/txs/chain/{last_txid} 取下一页，加入 acc；
               每页请求之间 await asyncio.sleep(0.2) 轻节流（避免公开 API 触发 429）。
          3. 任意请求失败（非 200/异常）立即停止并返回已累积的 acc（优雅降级）。

        返回 acc（btc_flow_24h 自带 24h 时间过滤与 confirmed 过滤，chain 分页天然无重叠，
        无需在此去重）。
        """
        acc: list[dict] = []
        cutoff_ms = now_ms - window_ms

        # ---- 首页 ----
        url = f"{self.base_url}/address/{addr}/txs"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning(
                        "blockstream /txs 非200 addr=%s status=%s", addr, resp.status
                    )
                    return acc
                page = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("blockstream /txs 首页失败 addr=%s: %s", addr, exc)
            return acc
        acc.extend(page)

        # ---- 分页循环 ----
        for _ in range(max_pages):
            if not page:
                break  # 上页为空，无更早数据

            # 取上页最后一笔已确认 tx 的 txid / block_time
            last_confirmed: dict | None = None
            for tx in reversed(page):
                status = tx.get("status") or {}
                if status.get("confirmed") and status.get("block_time"):
                    last_confirmed = tx
                    break

            if last_confirmed is None:
                break  # 上页全为未确认，无法定位分页锚点

            last_txid = last_confirmed.get("txid", "")
            last_block_time_s = (last_confirmed.get("status") or {}).get("block_time", 0)
            if not last_txid or last_block_time_s * 1000 < cutoff_ms:
                # 已翻过 24h 边界，停止分页
                break

            # 节流：每页间等待 0.2s
            await asyncio.sleep(0.2)

            chain_url = f"{self.base_url}/address/{addr}/txs/chain/{last_txid}"
            try:
                async with session.get(
                    chain_url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        log.warning(
                            "blockstream /txs/chain 非200 addr=%s txid=%s status=%s",
                            addr, last_txid, resp.status,
                        )
                        break  # 降级：返回已积累的 acc
                    page = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "blockstream /txs/chain 失败 addr=%s txid=%s: %s",
                    addr, last_txid, exc,
                )
                break  # 降级：返回已积累的 acc

            acc.extend(page)

        return acc

    async def address_stats(
        self,
        session: aiohttp.ClientSession,
        addr: str,
    ) -> dict | None:
        """GET /address/{addr} → 地址统计（chain_stats + mempool_stats）。

        失败返回 None（优雅降级）。
        """
        url = f"{self.base_url}/address/{addr}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning("blockstream /address 非200 addr=%s status=%s", addr, resp.status)
                    return None
                return await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("blockstream /address 失败 addr=%s: %s", addr, exc)
            return None
