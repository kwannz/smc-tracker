"""Bitget V2 REST 客户端（USDT-M 永续 + 币种链上合约）。

实证可用的端点（均为公开数据，无需鉴权）：
  GET /api/v2/mix/market/tickers?productType=USDT-FUTURES
      → 一次拿全部永续的 holdingAmount(OI)/fundingRate/markPrice/lastPr（高效）
  GET /api/v2/mix/market/open-interest?symbol=X&productType=USDT-FUTURES
  GET /api/v2/mix/market/contracts?productType=USDT-FUTURES   → 永续合约列表
  GET /api/v2/spot/public/coins[?coin=X]   → 币种各链 contractAddress（blockchain 地址）
"""
from __future__ import annotations

from typing import Any

import aiohttp
import orjson

BASE = "https://api.bitget.com"
USDT_FUTURES = "USDT-FUTURES"


from ..util import to_float as _f  # 统一安全数值解析


class BitgetREST:
    def __init__(self, base: str = BASE) -> None:
        self.base = base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "BitgetREST":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session is not None:
            await self._session.close()

    async def _get(self, path: str, **params: Any) -> Any:
        assert self._session is not None, "需在 async with 上下文中使用"
        async with self._session.get(self.base + path, params=params,
                                     headers={"User-Agent": "smc-tracker"}) as resp:
            resp.raise_for_status()
            body = orjson.loads(await resp.read())
        if str(body.get("code")) not in ("00000", "0"):
            raise RuntimeError(f"Bitget API 错误: {body.get('code')} {body.get('msg')}")
        return body.get("data")

    # ---- 永续合约列表 ----
    async def contracts(self) -> list[dict]:
        return await self._get("/api/v2/mix/market/contracts", productType=USDT_FUTURES)

    async def perp_base_coins(self) -> dict[str, str]:
        """返回 {symbol: baseCoin}，如 {'DOGEUSDT':'DOGE', '1000BONKUSDT':'1000BONK'}。"""
        rows = await self.contracts()
        return {r["symbol"]: r["baseCoin"] for r in rows if r.get("baseCoin")}

    # ---- 全市场 ticker（含 OI/资金费/标记价）----
    async def tickers(self) -> dict[str, dict]:
        """一次拉全部永续 ticker，返回 {symbol: ticker_dict}。"""
        rows = await self._get("/api/v2/mix/market/tickers", productType=USDT_FUTURES)
        return {r["symbol"]: r for r in rows}

    async def ticker(self, symbol: str) -> dict:
        rows = await self._get("/api/v2/mix/market/ticker",
                               symbol=symbol, productType=USDT_FUTURES)
        return rows[0] if rows else {}

    async def open_interest(self, symbol: str) -> float:
        d = await self._get("/api/v2/mix/market/open-interest",
                            symbol=symbol, productType=USDT_FUTURES)
        lst = d.get("openInterestList", []) if isinstance(d, dict) else []
        return _f(lst[0]["size"]) if lst else 0.0

    @staticmethod
    def parse_oi_row(symbol: str, coin: str, tk: dict, ts: int) -> tuple:
        """从 ticker dict 解析出 bitget_oi 表的一行。
        holdingAmount=OI(币数), markPrice=标记价 → oi_usd=OI×mark。"""
        oi_size = _f(tk.get("holdingAmount"))
        mark = _f(tk.get("markPrice") or tk.get("lastPr"))
        return (symbol, coin, oi_size, oi_size * mark, mark,
                _f(tk.get("fundingRate")), ts)

    # ---- 币种链上合约地址 ----
    async def coin_chains(self, coin: str) -> list[tuple[str, str]]:
        """返回 [(chain, contractAddress), ...]，仅保留有合约地址的链。"""
        data = await self._get("/api/v2/spot/public/coins", coin=coin)
        out: list[tuple[str, str]] = []
        for c in (data or []):
            if c.get("coin", "").upper() != coin.upper():
                continue
            for ch in c.get("chains", []):
                addr = ch.get("contractAddress")
                if addr:
                    out.append((ch.get("chain", ""), addr))
        return out

    async def all_coin_chains(self) -> dict[str, list[tuple[str, str]]]:
        """一次拉全部币种合约（避免逐币并发触发限流）。
        返回 {COIN大写: [(chain, contract), ...]}，仅含有合约地址的链。"""
        data = await self._get("/api/v2/spot/public/coins")
        out: dict[str, list[tuple[str, str]]] = {}
        for c in (data or []):
            coin = c.get("coin", "").upper()
            if not coin:
                continue
            chains = [(ch.get("chain", ""), ch.get("contractAddress"))
                      for ch in c.get("chains", []) if ch.get("contractAddress")]
            if chains:
                out[coin] = chains
        return out
