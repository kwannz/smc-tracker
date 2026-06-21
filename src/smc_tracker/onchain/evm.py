"""链上 EVM Transfer 监控（纯公开 RPC，零鉴权，不依赖 web3）。

第一性原理：ERC20 的 Transfer 是合约事件日志，公开 EVM RPC 直接可查。
  topic0 = keccak256("Transfer(address,address,uint256)")
  topics[1] = from（indexed，32字节，地址在后 20 字节 / 40 hex）
  topics[2] = to  （indexed）
  data      = value（uint256，按 token decimals 缩放成可读金额）

只用 aiohttp + orjson 发原始 JSON-RPC POST，无任何 API key。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp
import orjson

# ERC20 Transfer 事件签名 keccak256，已实证（PEPE 等可查）。
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# decimals() 函数选择器，用于 eth_call 读取 token 精度。
DECIMALS_SELECTOR = "0x313ce567"

# 链标识：合约所在链（meme_contracts.chain）→ 规范链名（落库用）。
CHAIN_BY_TOKEN_STANDARD: dict[str, str] = {
    "ERC20": "ETH",
    "BEP20": "BSC",
    "BASE": "BASE",
    # SOL 不在此处——Solana 非 EVM，本期跳过（见 monitor.py TODO）。
}


@dataclass(slots=True)
class Transfer:
    """一笔解析后的 ERC20 Transfer。amount 已按 decimals 缩放为可读值。"""
    chain: str          # 规范链名 ETH/BSC/BASE
    contract: str       # token 合约地址（小写）
    coin: str           # meme 符号，如 PEPE
    from_addr: str      # 转出地址（0x + 40 hex，小写）
    to_addr: str        # 转入地址
    amount: float       # 缩放后的 token 数量
    block: int          # 区块高度
    tx_hash: str        # 交易哈希
    log_index: int = 0  # 该 log 在 tx 内的序号(单 tx 内唯一，天然去重键)


def _topic_to_addr(topic: str) -> str:
    """32 字节 indexed topic → 0x 前缀的 20 字节地址（小写）。

    topic 形如 0x000...000<40hex 地址>，取后 40 hex。
    """
    h = topic[2:] if topic.startswith("0x") else topic
    return "0x" + h[-40:].lower()


def _hex_to_int(h: str) -> int:
    """十六进制字符串（可带 0x，可为空）转 int；空/异常返回 0。"""
    if not h:
        return 0
    try:
        return int(h, 16)
    except ValueError:
        return 0


def parse_decimals(result_hex: str | None) -> int:
    """解析 decimals() 的 eth_call 返回（uint8 编码在 32 字节里）；失败默认 18。"""
    if not result_hex or result_hex in ("0x", "0x0"):
        return 18
    try:
        v = int(result_hex, 16)
    except (ValueError, TypeError):
        return 18
    # 合理性约束：ERC20 decimals 落在 [0, 36]，越界视为解析异常。
    return v if 0 <= v <= 36 else 18


def parse_transfer_log(
    log: dict[str, Any],
    *,
    chain: str,
    coin: str,
    decimals: int,
) -> Transfer | None:
    """把一条 eth_getLogs 的 log dict 解析成 Transfer（纯函数，便于单测）。

    非 Transfer（topic0 不符）或 topics 不足时返回 None。
    """
    topics = log.get("topics") or []
    # 需要 topic0(签名) + from + to 三个 indexed topic。
    if len(topics) < 3:
        return None
    if topics[0].lower() != TRANSFER_TOPIC0:
        return None
    raw_value = _hex_to_int(log.get("data", "0x"))
    amount = raw_value / (10 ** decimals)
    return Transfer(
        chain=chain,
        contract=(log.get("address") or "").lower(),
        coin=coin,
        from_addr=_topic_to_addr(topics[1]),
        to_addr=_topic_to_addr(topics[2]),
        amount=amount,
        block=_hex_to_int(log.get("blockNumber", "0x0")),
        tx_hash=(log.get("transactionHash") or "").lower(),
        log_index=_hex_to_int(log.get("logIndex", "0x0")),
    )


class EVMTransferWatcher:
    """单条链上单个 token 合约的 Transfer 监控器（公开 RPC，无 key）。

    用法：
        async with aiohttp.ClientSession() as s:
            w = EVMTransferWatcher(rpc_url, contract, chain="ETH", coin="PEPE", session=s)
            head = await w.block_number()
            xs = await w.get_transfers(head - 4, head)
    """

    def __init__(
        self,
        rpc_url: str,
        contract: str,
        *,
        chain: str = "ETH",
        coin: str = "",
        decimals: int | None = None,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.rpc_url = rpc_url
        self.contract = contract.lower()
        self.chain = chain
        self.coin = coin
        self.decimals = decimals  # None=未知，首次 get_transfers 前惰性探测
        self._session = session
        self._own_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._id = 0

    async def __aenter__(self) -> "EVMTransferWatcher":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._own_session = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        """发一条 JSON-RPC POST，返回 result；RPC error 抛 RuntimeError。"""
        assert self._session is not None, "需先进入 async with 上下文或传入 session"
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        async with self._session.post(
            self.rpc_url,
            data=orjson.dumps(payload),
            headers={"Content-Type": "application/json", "User-Agent": "smc-tracker"},
            timeout=self._timeout,
        ) as resp:
            resp.raise_for_status()
            body = orjson.loads(await resp.read())
        if "error" in body and body["error"]:
            raise RuntimeError(f"RPC 错误 {method}: {body['error']}")
        return body.get("result")

    async def block_number(self) -> int:
        """当前链头区块高度。"""
        return _hex_to_int(await self._rpc("eth_blockNumber", []))

    async def fetch_decimals(self) -> int:
        """eth_call 读取 token decimals，缓存到 self.decimals；失败默认 18。"""
        if self.decimals is not None:
            return self.decimals
        try:
            res = await self._rpc(
                "eth_call",
                [{"to": self.contract, "data": DECIMALS_SELECTOR}, "latest"],
            )
            self.decimals = parse_decimals(res)
        except Exception:
            # 探测失败不致命——按主流默认 18 继续，避免阻塞监控。
            self.decimals = 18
        return self.decimals

    async def get_transfers(self, from_block: int, to_block: int) -> list[Transfer]:
        """查 [from_block, to_block] 区间内本合约的所有 Transfer，解析为 Transfer 列表。

        区块范围应尽量小（如最近 3-5 块）以避免公开 RPC 超时/限流。
        """
        if self.decimals is None:
            await self.fetch_decimals()
        dec = self.decimals if self.decimals is not None else 18
        logs = await self._rpc(
            "eth_getLogs",
            [
                {
                    "address": self.contract,
                    "topics": [TRANSFER_TOPIC0],
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                }
            ],
        )
        out: list[Transfer] = []
        for log in (logs or []):
            t = parse_transfer_log(log, chain=self.chain, coin=self.coin, decimals=dec)
            if t is not None:
                out.append(t)
        return out
