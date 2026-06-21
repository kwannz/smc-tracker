"""链上 meme 巨鲸转账监控编排（多链增量轮询 + 落 SQLite）。

职责：
  1. 从 Store 读 meme EVM 合约（只处理 ERC20/BEP20/BASE；SOL 跳过，见 TODO）。
  2. 每条链按 chain_rpc 配置连公开 RPC，记录每合约 last_block，增量抓新区块。
  3. 解析 Transfer，按 min_amount_usd（无价时退化为不过滤/按 token 量阈值）筛大额，落库。

新表 onchain_transfers 由本模块自管（CREATE TABLE IF NOT EXISTS），
不改 storage/db.py，避免与并行 agent 冲突。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from .evm import CHAIN_BY_TOKEN_STANDARD, EVMTransferWatcher, Transfer

# 本模块自管的落库表（不写进 storage/db.py 的 SCHEMA）。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS onchain_transfers (
    coin       TEXT    NOT NULL,
    chain      TEXT    NOT NULL,   -- ETH/BSC/BASE
    contract   TEXT    NOT NULL,
    from_addr  TEXT    NOT NULL,
    to_addr    TEXT    NOT NULL,
    amount     REAL    NOT NULL,   -- 缩放后的 token 数量
    amount_usd REAL,               -- 无价时为 NULL
    block      INTEGER NOT NULL,
    tx_hash    TEXT    NOT NULL,
    log_index  INTEGER NOT NULL DEFAULT 0,  -- log 在 tx 内序号(唯一去重键，避免浮点 amount 入主键)
    ts         INTEGER NOT NULL,
    PRIMARY KEY (tx_hash, contract, log_index)
);
CREATE INDEX IF NOT EXISTS ix_onchain_coin_block ON onchain_transfers(coin, block);
CREATE INDEX IF NOT EXISTS ix_onchain_from ON onchain_transfers(from_addr, block);
CREATE INDEX IF NOT EXISTS ix_onchain_to ON onchain_transfers(to_addr, block);
"""


class OnchainMemeMonitor:
    """多链 meme 大额转账监控器。

    chain_rpc: {链名: rpc_url}，链名用规范名 ETH/BSC/BASE，如
        {"ETH": "https://ethereum-rpc.publicnode.com",
         "BSC": "https://bsc-rpc.publicnode.com",
         "BASE": "https://base-rpc.publicnode.com"}
    """

    def __init__(
        self,
        store: Any,
        chain_rpc: dict[str, str],
        *,
        min_amount_usd: float = 50_000.0,
        max_block_span: int = 5,
        head_lag: int = 1,
        prices: dict[str, float] | None = None,
    ) -> None:
        self.store = store
        self.chain_rpc = chain_rpc
        self.min_amount_usd = min_amount_usd
        self.max_block_span = max_block_span  # 单次 getLogs 区块跨度上限（防超时/限流）
        # 公开 RPC 多为负载均衡：eth_blockNumber 与 eth_getLogs 可能命中不同后端节点，
        # 后者 head 略旧会报 "block range extends beyond current head block"。
        # 留 head_lag 个区块安全余量，只查已稳定的区块。
        self.head_lag = head_lag
        self.prices = prices or {}            # {COIN: usd_price}，可选；无价则 amount_usd=NULL
        self._last_block: dict[str, int] = {}  # contract(lower) -> 已处理到的区块
        self._decimals: dict[str, int] = {}    # contract(lower) -> 缓存的 decimals
        store.conn.executescript(_SCHEMA)
        # 旧库迁移：补 log_index 列(SQLite 无 ADD COLUMN IF NOT EXISTS，已存在则忽略)
        try:
            store.conn.execute(
                "ALTER TABLE onchain_transfers ADD COLUMN log_index INTEGER NOT NULL DEFAULT 0")
        except Exception:  # noqa: BLE001 — 列已存在/新表均无需迁移
            pass

    # ---- 合约读取 ----
    def evm_contracts(self) -> list[tuple[str, str, str]]:
        """从 store 读所有 meme 合约，过滤出可监控的 EVM 链。

        返回 [(coin, chain_norm, contract), ...]，chain_norm ∈ {ETH,BSC,BASE}。
        SOL（Solana）非 EVM，本期跳过。
        """
        out: list[tuple[str, str, str]] = []
        for coin, token_standard, contract in self.store.contracts():
            chain_norm = CHAIN_BY_TOKEN_STANDARD.get(token_standard)
            if chain_norm is None:
                continue  # SOL 等非 EVM 链：TODO 公开 SOL RPC 后续做
            if chain_norm not in self.chain_rpc:
                continue  # 没配该链 RPC，跳过
            out.append((coin, chain_norm, contract))
        return out

    def skipped_contracts(self) -> list[tuple[str, str, str]]:
        """返回被跳过的合约（非 EVM，如 Solana），供调用方报告。"""
        out: list[tuple[str, str, str]] = []
        for coin, token_standard, contract in self.store.contracts():
            if token_standard not in CHAIN_BY_TOKEN_STANDARD:
                out.append((coin, token_standard, contract))
        return out

    # ---- 估值 ----
    def _amount_usd(self, t: Transfer) -> float | None:
        """有价时返回 USD 估值，否则 None（落库存 NULL）。"""
        px = self.prices.get(t.coin.upper())
        if px is None:
            return None
        return t.amount * px

    # ---- 落库 ----
    def insert(self, rows: list[tuple]) -> int:
        """批量插入 onchain_transfers。

        rows: (coin,chain,contract,from_addr,to_addr,amount,amount_usd,block,tx_hash,log_index,ts)
        重复（同 tx/合约/log_index）忽略。返回插入条数。
        """
        if not rows:
            return 0
        before = self.store.conn.total_changes
        self.store.conn.executemany(
            "INSERT OR IGNORE INTO onchain_transfers"
            "(coin,chain,contract,from_addr,to_addr,amount,amount_usd,block,tx_hash,log_index,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return self.store.conn.total_changes - before

    def _passes_threshold(self, t: Transfer, amount_usd: float | None) -> bool:
        """大额判定：有价按 USD 阈值；无价时不过滤（保留，由人工/后续估值判断）。"""
        if amount_usd is not None:
            return amount_usd >= self.min_amount_usd
        return True

    def _to_row(self, t: Transfer, ts: int) -> tuple:
        usd = self._amount_usd(t)
        return (
            t.coin, t.chain, t.contract, t.from_addr, t.to_addr,
            t.amount, usd, t.block, t.tx_hash, t.log_index, ts,
        )

    # ---- 单合约一轮抓取 ----
    async def poll_contract(
        self,
        session: aiohttp.ClientSession,
        coin: str,
        chain: str,
        contract: str,
        *,
        lookback: int = 4,
    ) -> list[Transfer]:
        """抓单合约的新区块增量 Transfer，落库大额，返回本轮捕获的大额列表。

        首轮（无 last_block）从 head-lookback 起；之后从 last_block+1 起。
        单次区块跨度限制在 max_block_span 内。
        """
        key = contract.lower()
        rpc = self.chain_rpc[chain]
        watcher = EVMTransferWatcher(
            rpc, contract, chain=chain, coin=coin,
            decimals=self._decimals.get(key), session=session,
        )
        head = await watcher.block_number()
        # 【缺陷31修复】block_number() 偶发返回 0 或异常值时，safe_head 可能为负数，
        # 导致 hex(负数) 生成非法 eth_getLogs 参数。本轮跳过，等下轮 RPC 恢复后再抓。
        safe_head = head - self.head_lag
        if head <= 0 or safe_head < 0:
            return []  # 偶发坏响应，本轮跳过
        last = self._last_block.get(key)
        from_block = safe_head - lookback if last is None else last + 1
        from_block = max(0, from_block)  # 夹紧为非负，防止首轮 lookback 大于 safe_head
        if from_block > safe_head:
            return []  # 没有新（已稳定）区块
        # 限制跨度，避免一次拉太多触发限流。
        to_block = min(safe_head, from_block + self.max_block_span - 1)

        transfers = await watcher.get_transfers(from_block, to_block)
        # 缓存探测到的 decimals。
        if watcher.decimals is not None:
            self._decimals[key] = watcher.decimals
        self._last_block[key] = to_block

        now_ms = int(time.time() * 1000)
        big: list[Transfer] = []
        rows: list[tuple] = []
        for t in transfers:
            usd = self._amount_usd(t)
            if self._passes_threshold(t, usd):
                big.append(t)
                rows.append(self._to_row(t, now_ms))
        self.insert(rows)
        return big

    # ---- 一轮全量 ----
    async def poll_once(
        self,
        session: aiohttp.ClientSession | None = None,
        *,
        lookback: int = 4,
    ) -> list[Transfer]:
        """对所有 EVM meme 合约各抓一轮，返回本轮所有大额 Transfer（已落库）。"""
        own = session is None
        if own:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        try:
            # 【缺陷33修复】回收已下架合约在 _last_block/_decimals 中的残留键，
            # 防止长期运行内存持续增长。非热路径，O(合约数)，安全可接受。
            live = {c.lower() for _, _, c in self.evm_contracts()}
            if len(self._last_block) > len(live):
                self._last_block = {k: v for k, v in self._last_block.items() if k in live}
                self._decimals = {k: v for k, v in self._decimals.items() if k in live}

            captured: list[Transfer] = []
            for coin, chain, contract in self.evm_contracts():
                try:
                    captured.extend(
                        await self.poll_contract(
                            session, coin, chain, contract, lookback=lookback
                        )
                    )
                except Exception as exc:  # 单合约失败不影响其它（公开 RPC 偶发限流）
                    print(f"  ⚠️ {coin}@{chain} 抓取失败: {exc}")
                # 【缺陷32修复】顺序轮询多合约之间加节流，避免集中打公开 RPC 触发限流。
                # 与 solana.py 的低频节流风格一致（0.15s ≈ 6~7 合约/秒，足够宽松）。
                await asyncio.sleep(0.15)
            return captured
        finally:
            if own and session is not None:
                await session.close()
