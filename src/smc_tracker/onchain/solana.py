"""Solana 链上监控（无 API key）。

第一性原理实证结论：
- 无 key 公开 SOL RPC **封禁持仓发现类重型方法**（getTokenLargestAccounts / getProgramAccounts
  一律 429/403），故无法像 EVM 那样做地址级转账/持仓监控。
- 但 **getTokenSupply 轻量可用**（低频 + 退避）。故 Solana 侧改为**供应量监控**：
  检测 mint(增发，稀释/砸盘前兆) / burn(销毁，通缩利好)，是 meme 的 rug 相关链上信号。
- getTokenLargestAccounts 仍保留为 best-effort（用户换一个不限流的 RPC 即可启用持仓监控）。

落 SQLite 表 sol_supply（自管），供应量异动作为信号链上因子之一。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
import orjson

log = logging.getLogger("onchain.sol")

# 公开 keyless 端点（轮换；官方 getTokenSupply 可用，重型方法多被限）
DEFAULT_SOL_RPCS = (
    "https://api.mainnet-beta.solana.com",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sol_supply (
    coin     TEXT    NOT NULL,
    mint     TEXT    NOT NULL,
    supply   REAL    NOT NULL,
    decimals INTEGER,
    ts       INTEGER NOT NULL,
    PRIMARY KEY (mint, ts)
);
CREATE INDEX IF NOT EXISTS ix_sol_supply_mint ON sol_supply(mint, ts);
"""


@dataclass(slots=True)
class SupplyChange:
    coin: str
    mint: str
    prev_supply: float
    new_supply: float
    pct: float
    kind: str        # 'mint'(增发) / 'burn'(销毁)
    ts: int


class SolanaRPC:
    def __init__(self, urls: tuple[str, ...] = DEFAULT_SOL_RPCS) -> None:
        self.urls = urls

    async def _call(self, session: aiohttp.ClientSession, method: str,
                    params: list[Any], retries: int = 3) -> Any:
        body = orjson.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        last_exc: Exception | None = None
        rate_limited = False
        for attempt in range(retries):
            url = self.urls[attempt % len(self.urls)]
            try:
                async with session.post(url, data=body,
                                        headers={"Content-Type": "application/json"}) as resp:
                    if resp.status in (429, 403):
                        rate_limited = True
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    out = orjson.loads(await resp.read())
                    if "error" in out:
                        # JSON-RPC 业务 error：软限流类(rate/limit/429)纳入退避重试，否则抛
                        emsg = str(out["error"]).lower()
                        if any(s in emsg for s in ("rate", "limit", "429", "too many")):
                            rate_limited = True
                            last_exc = RuntimeError(out["error"])
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        raise RuntimeError(out["error"])
                    return out.get("result")
            except aiohttp.ClientError as e:  # noqa: PERF203
                last_exc = e
                await asyncio.sleep(0.5 * (attempt + 1))
        if last_exc:
            raise last_exc
        if rate_limited:                  # 全部尝试被限流 → 抛出而非静默 None，让上层感知
            raise RuntimeError(f"{method}: 全部 {retries} 次尝试均被限流")
        return None

    async def token_supply(self, session: aiohttp.ClientSession,
                           mint: str) -> tuple[float, int] | None:
        v = await self._call(session, "getTokenSupply", [mint])
        if not v or "value" not in v:
            return None
        val = v["value"]
        try:
            return float(val["uiAmountString"]), int(val["decimals"])
        except (KeyError, TypeError, ValueError):
            return None

    async def largest_accounts(self, session: aiohttp.ClientSession,
                               mint: str) -> list[dict]:
        """best-effort：无 key 公开 RPC 多被限流，失败返回 []。"""
        try:
            v = await self._call(session, "getTokenLargestAccounts", [mint], retries=1)
            return (v or {}).get("value", []) if isinstance(v, dict) else []
        except Exception:  # noqa: BLE001
            return []


def detect_change(prev: float | None, new: float, min_pct: float) -> tuple[float, str] | None:
    """供应量变化检测；返回 (pct, kind) 或 None。"""
    if prev is None or prev <= 0:
        return None
    pct = (new - prev) / prev
    if abs(pct) < min_pct:
        return None
    return pct, ("mint" if pct > 0 else "burn")


class SolanaSupplyMonitor:
    def __init__(self, store: Any, rpc: SolanaRPC | None = None,
                 min_change_pct: float = 0.005) -> None:
        self.store = store
        self.rpc = rpc or SolanaRPC()
        self.min_change_pct = min_change_pct
        store.conn.executescript(_SCHEMA)

    def sol_mints(self) -> list[tuple[str, str]]:
        """从 meme_contracts 取 SOL 链合约，返回 [(coin, mint), ...]。"""
        return [(coin, contract) for coin, chain, contract in self.store.contracts()
                if chain == "SOL"]

    def _last_supply(self, mint: str) -> float | None:
        row = self.store.conn.execute(
            "SELECT supply FROM sol_supply WHERE mint=? ORDER BY ts DESC LIMIT 1",
            (mint,)).fetchone()
        return row[0] if row else None

    async def poll_once(self, now_ms: int,
                        session: aiohttp.ClientSession | None = None) -> list[SupplyChange]:
        own = session is None
        if own:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        changes: list[SupplyChange] = []
        try:
            for coin, mint in self.sol_mints():
                try:                              # 单 mint 限流/失败不拖垮整批
                    res = await self.rpc.token_supply(session, mint)
                except Exception as e:  # noqa: BLE001
                    log.debug("SOL 供应查询失败 %s: %s", coin, e)
                    continue
                if res is None:
                    continue
                supply, decimals = res
                prev = self._last_supply(mint)
                self.store.conn.execute(
                    "INSERT OR REPLACE INTO sol_supply(coin,mint,supply,decimals,ts) "
                    "VALUES(?,?,?,?,?)", (coin, mint, supply, decimals, now_ms))
                ch = detect_change(prev, supply, self.min_change_pct)
                if ch:
                    pct, kind = ch
                    changes.append(SupplyChange(coin, mint, prev, supply, pct, kind, now_ms))
                await asyncio.sleep(0.4)        # 低频，避开限流
        finally:
            if own:
                await session.close()
        return changes
