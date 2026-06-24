"""Bitget V2 REST 客户端（USDT-M 永续 + 币种链上合约 + K 线拉取）。

实证可用的端点（均为公开数据，无需鉴权）：
  GET /api/v2/mix/market/tickers?productType=USDT-FUTURES
      → 一次拿全部永续的 holdingAmount(OI)/fundingRate/markPrice/lastPr（高效）
  GET /api/v2/mix/market/open-interest?symbol=X&productType=USDT-FUTURES
  GET /api/v2/mix/market/contracts?productType=USDT-FUTURES   → 永续合约列表
  GET /api/v2/spot/public/coins[?coin=X]   → 币种各链 contractAddress（blockchain 地址）
  GET /api/v2/mix/market/candles?symbol=&productType=USDT-FUTURES&granularity=&limit=N
      → 最近 N 根 K 线（升序，单次 limit 上限=1000）
  GET /api/v2/mix/market/history-candles?...&endTime=<ms>&limit≤200
      → 历史分页 K 线（endTime 之前，升序，单次 ≤200）
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

import aiohttp
import orjson

BASE = "https://api.bitget.com"
USDT_FUTURES = "USDT-FUTURES"

from ..models import Candle
from ..util import to_float as _f  # 统一安全数值解析

# ---- 合法 granularity → 毫秒映射（实证 Bitget 合法值）----
# 分钟用小写 m，小时及以上用大写 H/D/W/M（已实证）
GRANULARITY_MS: dict[str, int] = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H":  3_600_000,
    "4H":  14_400_000,
    "6H":  21_600_000,
    "12H": 43_200_000,
    "1D":  86_400_000,
    "3D":  259_200_000,
    "1W":  604_800_000,
    "1M":  2_592_000_000,  # 近似 30 天
}


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
        # 429(限流)/5xx(服务端) 退避重试：大周期回填请求量大、公开 IP 易触发限流，
        # 退避 0.5/1.0s 重试 3 次；其它 4xx 直接抛（非限流，重试无益）。
        for attempt in range(3):
            try:
                async with self._session.get(self.base + path, params=params,
                                             headers={"User-Agent": "smc-tracker"}) as resp:
                    resp.raise_for_status()
                    body = orjson.loads(await resp.read())
                if str(body.get("code")) not in ("00000", "0"):
                    raise RuntimeError(f"Bitget API 错误: {body.get('code')} {body.get('msg')}")
                return body.get("data")
            except aiohttp.ClientResponseError as e:
                if (e.status != 429 and e.status < 500) or attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise RuntimeError("Bitget _get 重试耗尽（不可达）")

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

    # ---- 多周期 K 线（含分页回溯）----

    @staticmethod
    def _safe_price(raw: Any) -> float | None:
        """安全价格解析：字符串/数值 → float，若原始值为 NaN/inf/不可解析 → None（拒绝）。

        与 util.to_float 不同：to_float 对 NaN/inf 返回默认 0.0（无法区分真实零价），
        本方法需要明确区分「无效值」和「合法零值」，因此非法则返回 None（哨兵）。
        """
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    def _parse_kline_rows(
        self, rows: list, granularity: str, coin_label: str
    ) -> list[Candle]:
        """解析原始 K 线行列表 → list[Candle]，跳过脏/非法行（数据质量守卫）。

        每行格式：[ts_ms, open, high, low, close, baseVol, quoteVol]。
        价格字段任一为 NaN/inf/不可解析 → 跳过该行（严格数据质量守卫）。
        """
        gran_ms = GRANULARITY_MS[granularity]
        out: list[Candle] = []
        for row in (rows or []):
            try:
                if not row or len(row) < 6:
                    continue
                ts = int(float(row[0]))  # 容浮点格式 ts(如 '1.7e12')，避免静默丢整根 K 线(B1)
                o = self._safe_price(row[1])
                h = self._safe_price(row[2])
                l = self._safe_price(row[3])
                c = self._safe_price(row[4])
                # 任意价格字段无效 → 跳过
                if o is None or h is None or l is None or c is None:
                    continue
                v = _f(row[5])  # 成交量用 to_float（0.0 兜底可接受）
                out.append(Candle(
                    coin=coin_label,
                    interval=granularity,
                    open_time_ms=ts,
                    close_time_ms=ts + gran_ms,
                    o=o, h=h, l=l, c=c, v=v,
                    n=0,  # Bitget 不提供成交笔数
                ))
            except (TypeError, ValueError, IndexError):
                continue  # 跳过不可解析行，不崩溃
        return out

    async def klines(
        self,
        symbol: str,
        granularity: str,
        bars: int = 1000,
        coin: str = "",
    ) -> list[Candle]:
        """拉取永续合约多周期 K 线，支持超 1000 根分页回溯。

        bars 强制 clamp 到 [1, 1999]（防单次请求过大）。
        先用 candles 端点取最近 min(bars,1000) 根；若 bars>1000 则用
        history-candles 以最旧一根 ts 为 endTime 向前分页（每页 ≤200）直到凑够
        bars 或无更多数据。
        结果按 ts 去重 + 升序排序后返回。

        Args:
            symbol:      交易对，如 "BTCUSDT"
            granularity: K 线周期，需在 GRANULARITY_MS 中（校验守卫）
            bars:        目标根数，clamp 到 [1, 1999]
            coin:        Candle.coin 字段标签；为空则用 symbol
        """
        if granularity not in GRANULARITY_MS:
            raise ValueError(
                f"非法 granularity={granularity!r}，"
                f"合法值: {sorted(GRANULARITY_MS.keys())}"
            )
        # bars clamp 守卫（用户要求每个周期控制 2000 以下）
        bars = max(1, min(bars, 2500))   # 上限 2500（用户#：每周期保留 2500 bar；不强制，取可得）
        coin_label = coin if coin else symbol

        # ---- 第一阶段：candles 端点取最近 min(bars,1000) 根 ----
        first_n = min(bars, 1000)
        raw = await self._get(
            "/api/v2/mix/market/candles",
            symbol=symbol,
            productType=USDT_FUTURES,
            granularity=granularity,
            limit=str(first_n),
        )
        collected: list[Candle] = self._parse_kline_rows(raw or [], granularity, coin_label)

        # ---- 第二阶段：不足 bars 则用 history-candles 向前分页回填 ----
        # 实证(2026-06)：Bitget candles 端点对大周期单次仅返回有限根（1W=13/1D=90/12H=180，
        # 即便 limit=1000 也如此 ≈90 天上限）。故**不论 bars 是否 >1000**，只要 collected<bars
        # 就回填，直到够 bars / 无更多数据 / 达页预算上限——否则大周期(尤其 1W) 永远不足 BB 所需 21 根。
        # endTime 排他(返回其之前的根)，跨页按 open_time_ms 末尾统一去重，故无需精确计数。
        max_pages = 10  # 页预算上限（防大周期无限翻页；bars=2500 时小周期 1000+9×200 可逼近，大周期取可得）
        pages = 0
        while collected and len(collected) < bars and pages < max_pages:
            oldest_ts = min(c.open_time_ms for c in collected)
            if oldest_ts <= 0:
                break
            page_n = min(bars - len(collected), 200)
            hist_raw = await self._get(
                "/api/v2/mix/market/history-candles",
                symbol=symbol,
                productType=USDT_FUTURES,
                granularity=granularity,
                limit=str(page_n),
                endTime=str(oldest_ts),
            )
            page = self._parse_kline_rows(hist_raw or [], granularity, coin_label)
            if not page:
                break  # 无更多历史
            new_oldest = min(c.open_time_ms for c in page)
            collected.extend(page)
            pages += 1
            if new_oldest >= oldest_ts:
                break  # 没有更早数据，防死循环

        # ---- 去重 + 升序排序 ----
        seen: dict[int, Candle] = {}
        for c in collected:
            seen[c.open_time_ms] = c  # 相同 ts 保留后者（一致性）
        result = sorted(seen.values(), key=lambda c: c.open_time_ms)
        return result
