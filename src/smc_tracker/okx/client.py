"""OKX V5 公共 REST 客户端（永续，无需 API key）。

实证可用端点（www.okx.com，纯公开数据，2026-06-22 实测 code=0）：
  GET /api/v5/market/ticker?instId=BTC-USDT-SWAP
      → last/open24h/volCcy24h/ts（**无 24h 涨幅字段，须自算** (last-open24h)/open24h）
  GET /api/v5/public/open-interest?instId=X 或 ?instType=SWAP（全市场一次拉）
      → oi(合约张数)/oiCcy(币数)/oiUsd(美元)/ts
  GET /api/v5/public/funding-rate?instId=X   → fundingRate/nextFundingTime/premium（**不支持批量**）
  GET /api/v5/public/mark-price?instId=X 或 ?instType=SWAP   → markPx/ts
  GET /api/v5/market/candles?instId=&bar=&limit=
      → 倒序 9 元素 [ts,o,h,l,c,vol(张),volCcy(币数),volCcyQuote(USDT),confirm]，**需 reverse**
  GET /api/v5/public/instruments?instType=SWAP   → 全市场合约（387 个，USDT 本位 372）
包装：{"code":"0","msg":"","data":[...]}；code!=0 视为错误。
"""
from __future__ import annotations

from typing import Any

import aiohttp
import orjson

from ..util import to_float as _f  # 统一安全数值解析

BASE = "https://www.okx.com"


def _i(x: Any, default: int = 0) -> int:
    """安全转 int（ts/时间戳为字符串 ms epoch）。"""
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


# ---- 纯解析函数（无网络，确定性可测）----

def parse_ticker(d: dict) -> dict:
    """OKX ticker → 归一 {inst_id, price, open24h, chg24, vol24h_ccy, ts}。

    OKX ticker 无 24h 涨幅字段，用 (last-open24h)/open24h 自算（open<=0 → 0，不除零）。
    """
    last = _f(d.get("last"))
    open24 = _f(d.get("open24h"))
    chg24 = (last - open24) / open24 if open24 > 0 else 0.0
    return {
        "inst_id": d.get("instId", ""),
        "price": last,
        "open24h": open24,
        "chg24": chg24,
        "vol24h_ccy": _f(d.get("volCcy24h")),
        "ts": _i(d.get("ts")),
    }


def parse_oi(d: dict) -> dict:
    """OKX open-interest → {inst_id, oi_ccy(币数), oi_usd(美元), ts}。"""
    return {
        "inst_id": d.get("instId", ""),
        "oi_ccy": _f(d.get("oiCcy")),
        "oi_usd": _f(d.get("oiUsd")),
        "ts": _i(d.get("ts")),
    }


def parse_funding(d: dict) -> dict:
    """OKX funding-rate → {inst_id, funding_rate, next_funding_time, premium}。"""
    return {
        "inst_id": d.get("instId", ""),
        "funding_rate": _f(d.get("fundingRate")),
        "next_funding_time": _i(d.get("nextFundingTime")),
        "premium": _f(d.get("premium")),
    }


def parse_mark(d: dict) -> dict:
    """OKX mark-price → {inst_id, mark_px, ts}。"""
    return {
        "inst_id": d.get("instId", ""),
        "mark_px": _f(d.get("markPx")),
        "ts": _i(d.get("ts")),
    }


def parse_candles(rows: list[list]) -> list[tuple]:
    """OKX candles(倒序9元素) → 正序 [(ts,o,h,l,c,vol_ccy), ...]。

    OKX 返回最新在前 → reverse 成时间正序；vol 取 volCcy(index 6，币数)，与本项目 K 线币数口径一致。
    长度守卫：跳过元素不足 7 的异常行（防裸下标越界）。
    """
    out: list[tuple] = []
    for r in reversed(rows):
        if len(r) < 7:
            continue
        out.append((_i(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[6])))
    return out


class OKXClient:
    """OKX V5 公共 REST（async with 上下文管理 aiohttp session）。"""

    def __init__(self, base: str = BASE) -> None:
        self.base = base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "OKXClient":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session is not None:
            await self._session.close()

    async def _get(self, path: str, **params: Any) -> list:
        assert self._session is not None, "需在 async with 上下文中使用"
        async with self._session.get(self.base + path, params=params,
                                     headers={"User-Agent": "smc-tracker"}) as resp:
            resp.raise_for_status()
            body = orjson.loads(await resp.read())
        if str(body.get("code")) != "0":
            raise RuntimeError(f"OKX API 错误: {body.get('code')} {body.get('msg')}")
        return body.get("data") or []

    # ---- 单 instId 行情 ----
    async def ticker(self, inst_id: str) -> dict:
        rows = await self._get("/api/v5/market/ticker", instId=inst_id)
        return parse_ticker(rows[0]) if rows else {}

    async def open_interest(self, inst_id: str) -> dict:
        rows = await self._get("/api/v5/public/open-interest", instId=inst_id)
        return parse_oi(rows[0]) if rows else {}

    async def funding_rate(self, inst_id: str) -> dict:
        rows = await self._get("/api/v5/public/funding-rate", instId=inst_id)
        return parse_funding(rows[0]) if rows else {}

    async def mark_price(self, inst_id: str) -> dict:
        rows = await self._get("/api/v5/public/mark-price", instId=inst_id)
        return parse_mark(rows[0]) if rows else {}

    async def candles(self, inst_id: str, bar: str = "5m", limit: int = 100) -> list[tuple]:
        rows = await self._get("/api/v5/market/candles", instId=inst_id, bar=bar, limit=str(limit))
        return parse_candles(rows)

    # ---- 全市场批量（冷启动/基线快照）----
    async def all_open_interest(self, inst_type: str = "SWAP") -> dict[str, dict]:
        """一次拉全市场 OI（instType=SWAP），返回 {inst_id: parsed_oi}。"""
        rows = await self._get("/api/v5/public/open-interest", instType=inst_type)
        return {r["instId"]: parse_oi(r) for r in rows if r.get("instId")}

    async def all_mark_price(self, inst_type: str = "SWAP") -> dict[str, dict]:
        """一次拉全市场标记价，返回 {inst_id: parsed_mark}。"""
        rows = await self._get("/api/v5/public/mark-price", instType=inst_type)
        return {r["instId"]: parse_mark(r) for r in rows if r.get("instId")}

    async def swap_instruments(self, inst_type: str = "SWAP") -> list[str]:
        """全市场永续 instId 列表（仅 USDT 本位 -USDT-SWAP）。"""
        rows = await self._get("/api/v5/public/instruments", instType=inst_type)
        return [r["instId"] for r in rows if r.get("instId", "").endswith("-USDT-SWAP")]

    async def swap_meta(self, inst_type: str = "SWAP") -> dict[str, dict]:
        """全市场永续元数据 {inst_id: {ct_val(合约面值,币), ct_val_ccy}}（仅 USDT 本位）。

        OKX SWAP 的 trades.sz 单位是合约张数，名义须乘 ct_val（BTC=0.01/ETH=0.1/DOGE=1000）。
        """
        rows = await self._get("/api/v5/public/instruments", instType=inst_type)
        return {
            r["instId"]: {"ct_val": _f(r.get("ctVal")), "ct_val_ccy": r.get("ctValCcy", "")}
            for r in rows if r.get("instId", "").endswith("-USDT-SWAP")
        }
