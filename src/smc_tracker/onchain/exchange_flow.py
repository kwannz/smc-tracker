"""交易所链上资金流监控（BTC + EVM 稳定币，keyless 公开 API）。

职责：
  1. 追踪 Binance/OKX/Bitget 等交易所已知 BTC 钱包的 24h 资金流向（blockstream.info）。
  2. 追踪已知 ETH 钱包地址的 USDT/USDC 稳定币流入/流出（ETH 主网公开 RPC，eth_getLogs）。
     - 稳定币流入交易所 = 买盘弹药进场（🟢 看涨信号）
     - 稳定币流出交易所 = 资金撤离（🔴 看跌信号）
     - 与 BTC 语义相反：BTC 流入 = 潜在抛压（🔴）；BTC 流出 = 吸筹（🟢）。
  3. 大额流入交易所 = 潜在抛压；净流出 = 可能吸筹/囤币。
  4. 落 SQLite 表 exchange_flows（自管），提供 recent() 查询接口。

BTC 数据源：blockstream.info 公开 API（无 key）
  - GET /address/{addr}                       → 地址统计
  - GET /address/{addr}/txs                   → 最近交易列表（已确认+未确认，≤25笔/页）
  - GET /address/{addr}/txs/chain/{last_txid} → 分页：返回 last_txid 之前更早的 ≤25 笔已确认 tx

EVM 稳定币数据源：https://ethereum-rpc.publicnode.com（无 key）
  - eth_getLogs：批量查 USDT/USDC Transfer 事件，topic 数组(OR)筛地址
    流入：topics[2]（to）= 交易所地址列表
    流出：topics[1]（from）= 交易所地址列表
  - 已验证支持：address 数组(OR) + topic 数组(OR)
  - 保守窗口（block_window=600 块 ≈ 2h，chunk=150 块/次），避开结果上限（通常 10000 条/次）。
    可能受公开 RPC 单次返回上限影响，支持错误回退细分（最多 3 层递归）。
  - USDT=0xdAC17F958D2ee523a2206206994597C13D831ec7，USDC=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
  - TRANSFER topic0=0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
  - padded 地址：0x + 24个0 + 40位地址(小写)；USDT/USDC 均 6 位小数(value/1e6 = 美元)。

BTC 说明：
  - 地址为公开已知种子（链上分析报告佐证），可能不全——仅覆盖主要冷/热钱包。
  - 只统计已确认（status.confirmed == True）的交易，未确认视为未发生。
  - address_txs_window 最多抓 max_pages 页（默认 6），覆盖约 max_pages×25 笔已确认 tx；
    对极端高频热钱包（24h 内 >150 笔）仍可能低估，属已知局限。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ..util import fmt_ts

log = logging.getLogger("onchain.exchange_flow")

# blockstream.info 公开端点（无 key，部分地区可能需要代理）
_DEFAULT_BASE_URL = "https://blockstream.info/api"

# ERC20 Transfer 事件 topic0（keccak256("Transfer(address,address,uint256)")，已实证）
_TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# 自管 SQLite 表（不写进 storage/db.py）
_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange_flows (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,          -- 采集时间戳 ms
    dt       TEXT    NOT NULL,          -- 可读时间（fmt_ts）
    exchange TEXT    NOT NULL,          -- Binance / OKX / Bitget …
    chain    TEXT    NOT NULL,          -- BTC / ETH（稳定币用 ETH）
    inflow   REAL    NOT NULL,          -- 流入：BTC 单位 BTC；ETH 稳定币单位 USD
    outflow  REAL    NOT NULL,          -- 流出：同上
    net      REAL    NOT NULL,          -- net = inflow - outflow
    n_tx     INTEGER NOT NULL,          -- 统计窗口内覆盖的 tx/log 笔数
    n_addr   INTEGER NOT NULL           -- 参与计算的地址数
);
CREATE INDEX IF NOT EXISTS ix_exchange_flows_exch_ts
    ON exchange_flows(exchange, ts);
"""


# ---------------------------------------------------------------------------
# Blockstream HTTP 客户端（BTC）
# ---------------------------------------------------------------------------

class BlockstreamClient:
    """封装 blockstream.info 公开 REST API，支持外部注入 aiohttp.ClientSession（便于测试）。

    base_url 可在测试中替换为 mock 服务器地址，生产用默认值。
    """

    def __init__(self, base_url: str = _DEFAULT_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    async def address_txs(
        self,
        session: aiohttp.ClientSession,
        addr: str,
    ) -> list[dict]:
        """GET /address/{addr}/txs → 最近交易列表（最多 25 笔，已确认+未确认混合）。

        超时/非 200/异常均返回 []（log.warning 记录，不向上抛，保证优雅降级）。

        注意：本方法只取首页 ≤25 笔，保留以供向后兼容。
        生产路径请用 address_txs_window（支持分页，24h 覆盖精度更高）。
        """
        url = f"{self.base_url}/address/{addr}/txs"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning("blockstream /txs 非200 addr=%s status=%s", addr, resp.status)
                    return []
                return await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 — 网络/超时/解析等各类异常统一降级
            log.warning("blockstream /txs 失败 addr=%s: %s", addr, exc)
            return []

    async def address_txs_window(
        self,
        session: aiohttp.ClientSession,
        addr: str,
        now_ms: int,
        window_ms: int = 86_400_000,
        max_pages: int = 6,
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


# ---------------------------------------------------------------------------
# EVM 稳定币流向客户端（ETH 主网，keyless 公开 RPC）
# ---------------------------------------------------------------------------

def _pad_addr(addr: str) -> str:
    """将 20 字节 ETH 地址填充为 32 字节 topic 格式（0x + 24个0 + 40位地址小写）。

    例：0xF977814e90dA44bFA03b6295A0616a897441aceC
      → 0x000000000000000000000000f977814e90da44bfa03b6295a0616a897441acec
    """
    # 去掉 0x 前缀，取后 40 位（防止已经是完整 topic 格式）
    raw = addr.lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    # 取后 40 hex（20字节地址部分）
    raw = raw[-40:]
    return "0x" + "0" * 24 + raw


def sum_stable_logs(logs: list[dict], contract_decimals: dict[str, int]) -> float:
    """纯函数：将 eth_getLogs 返回的 log 列表按合约 decimals 汇总为美元总量。

    参数：
        logs               eth_getLogs 返回的 log 列表（每条含 address + data 字段）
        contract_decimals  {合约地址小写: decimals}，用于 value/10**decimals 换算

    返回：
        各条 log 换算为美元后的总和（float）

    设计为纯函数，无网络依赖，便于单测。
    """
    total: float = 0.0
    for entry in logs:
        contract = (entry.get("address") or "").lower()
        dec = contract_decimals.get(contract, 6)  # 默认 6（USDT/USDC 均为 6）
        data_hex = entry.get("data") or "0x"
        try:
            value = int(data_hex, 16)
        except (ValueError, TypeError):
            continue
        total += value / (10 ** dec)
    return total


class EVMStableFlow:
    """EVM 稳定币（USDT/USDC）交易所资金流查询器（公开 RPC，无 key）。

    通过 eth_getLogs 批量查询 USDT/USDC 的 ERC20 Transfer 事件：
      - 流入：topics[2]（to）= 交易所地址 → 稳定币进入交易所 = 买盘弹药
      - 流出：topics[1]（from）= 交易所地址 → 稳定币离开交易所 = 资金撤离

    支持外部注入 session（便于测试），段间 sleep 节流（0.15s/段）。
    若某段因公开 RPC 结果上限返回 error，自动二分递归细查（最多 3 层）。

    已实证 https://ethereum-rpc.publicnode.com 支持：
      address 数组(OR) + topic 数组(OR)，USDT/USDC decimals=6。
    """

    async def _block_number(self, session: aiohttp.ClientSession, rpc: str) -> int:
        """eth_blockNumber 获取当前链头区块号；失败返回 0。"""
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
        try:
            async with session.post(
                rpc,
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": "smc-tracker"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("eth_blockNumber 非200 status=%s", resp.status)
                    return 0
                body = await resp.json(content_type=None)
                result = body.get("result") or "0x0"
                return int(result, 16)
        except Exception as exc:  # noqa: BLE001
            log.warning("eth_blockNumber 失败: %s", exc)
            return 0

    async def _get_logs_with_split(
        self,
        session: aiohttp.ClientSession,
        rpc: str,
        params: dict,
        seg_from: int,
        seg_to: int,
        depth: int = 0,
        max_depth: int = 3,
    ) -> list[dict]:
        """带二分回退的 eth_getLogs：若返回空且可能是因结果上限，递归细查（最多 max_depth 层）。

        注：公开 RPC 结果过多时通常返回 error（而非空），本函数对 error 情形执行二分；
        真实空结果（无 Transfer）直接返回 []。
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{**params, "fromBlock": hex(seg_from), "toBlock": hex(seg_to)}],
        }
        # 公开 RPC 偶发瞬时限流(403/429/5xx)：退避重试至多 3 次再降级，提升实时可靠性。
        body = None
        for attempt in range(3):
            try:
                async with session.post(
                    rpc,
                    json=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "smc-tracker"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status in (403, 429, 500, 502, 503):   # 瞬时限流/网关 → 退避重试
                        await asyncio.sleep(0.8 * (attempt + 1))
                        continue
                    if resp.status != 200:
                        log.warning(
                            "eth_getLogs 非200 status=%s [%d,%d]", resp.status, seg_from, seg_to
                        )
                        return []
                    body = await resp.json(content_type=None)
                    break
            except Exception as exc:  # noqa: BLE001
                log.warning("eth_getLogs 请求失败 [%d,%d](第%d次): %s",
                            seg_from, seg_to, attempt + 1, exc)
                await asyncio.sleep(0.5 * (attempt + 1))
        if body is None:
            return []

        # RPC error → 可能结果上限，尝试二分
        if "error" in body and body["error"]:
            err_msg = str(body["error"])
            log.warning("eth_getLogs error [%d,%d]: %s", seg_from, seg_to, err_msg)
            # 判断是否是结果过多错误（各节点提示词不同，保守检查常见关键词）
            is_limit_error = any(
                kw in err_msg.lower()
                for kw in ("more than", "limit", "too many", "exceed", "response size")
            )
            if is_limit_error and depth < max_depth and seg_to > seg_from:
                mid = (seg_from + seg_to) // 2
                log.debug(
                    "eth_getLogs 结果上限，二分细查 [%d,%d] depth=%d", seg_from, seg_to, depth
                )
                left = await self._get_logs_with_split(
                    session, rpc, params, seg_from, mid, depth + 1, max_depth
                )
                right = await self._get_logs_with_split(
                    session, rpc, params, mid + 1, seg_to, depth + 1, max_depth
                )
                return left + right
            return []

        return body.get("result") or []

    async def exchange_stable_flow(
        self,
        session: aiohttp.ClientSession,
        rpc: str,
        addrs: list[str],
        stablecoins: list[dict],
        block_window: int,
        chunk: int,
    ) -> dict:
        """查询交易所地址列表在最近 block_window 块内的 USDT/USDC 流入/流出（美元）。

        参数：
            session      aiohttp.ClientSession（外部管理，便于复用/测试注入）
            rpc          EVM RPC URL（公开节点，无 key）
            addrs        交易所 ETH 地址列表（可多个，OR 关系）
            stablecoins  [{"symbol": "USDT", "contract": "0x...", "decimals": 6}, ...]
            block_window 扫描窗口总块数（建议 ≤600，约 2h）
            chunk        每次 getLogs 请求的块跨度（建议 ≤150）

        返回：
            {
              "inflow":  float,  # 稳定币流入交易所总量（USD）= 买盘弹药
              "outflow": float,  # 稳定币流出交易所总量（USD）= 资金撤离
              "net":     float,  # inflow - outflow（正=净流入=弹药进场）
              "n_log":   int,    # 计入统计的 log 条数
              "blocks":  int,    # 实际扫描块数（= block_window，或因链头获取失败为 0）
            }

        实现细节：
          - 地址列表 padded 为 topic 格式（0x + 24个0 + 40位地址）。
          - 合约地址列表（USDT+USDC）作为 address 数组传给 eth_getLogs（OR 关系）。
          - 流入查询：topics=[TRANSFER_TOPIC0, null, [padded_addrs]]（to=交易所）
          - 流出查询：topics=[TRANSFER_TOPIC0, [padded_addrs], null]（from=交易所）
          - 按 chunk 块分段循环，段间 sleep 0.15s 节流。
          - 结果上限回退：错误时二分递归细查（最多 3 层）。
        """
        # 构建合约地址列表（小写）和 decimals 查找表
        contracts = [(sc["contract"].lower(), sc.get("decimals", 6)) for sc in stablecoins]
        contract_addrs = [c for c, _ in contracts]
        contract_decimals = {c: d for c, d in contracts}

        # padded 地址列表（topic OR 数组）
        padded = [_pad_addr(a) for a in addrs]

        # 获取链头
        head = await self._block_number(session, rpc)
        if head == 0:
            log.warning("eth_blockNumber 失败，跳过 EVM 稳定币查询")
            return {"inflow": 0.0, "outflow": 0.0, "net": 0.0, "n_log": 0, "blocks": 0}

        blk_from = max(0, head - block_window)
        blk_to = head

        total_inflow: float = 0.0
        total_outflow: float = 0.0
        n_log: int = 0

        # 按 chunk 分段查询
        seg_from = blk_from
        while seg_from <= blk_to:
            seg_to = min(seg_from + chunk - 1, blk_to)

            # 流入查询：topics[2] = 交易所地址（to = 交易所）
            in_params = {
                "address": contract_addrs,
                "fromBlock": hex(seg_from),
                "toBlock": hex(seg_to),
                "topics": [_TRANSFER_TOPIC0, None, padded],
            }
            in_logs = await self._get_logs_with_split(
                session, rpc, in_params, seg_from, seg_to
            )
            inflow_usd = sum_stable_logs(in_logs, contract_decimals)
            total_inflow += inflow_usd
            n_log += len(in_logs)

            # 流出查询：topics[1] = 交易所地址（from = 交易所）
            out_params = {
                "address": contract_addrs,
                "fromBlock": hex(seg_from),
                "toBlock": hex(seg_to),
                "topics": [_TRANSFER_TOPIC0, padded, None],
            }
            out_logs = await self._get_logs_with_split(
                session, rpc, out_params, seg_from, seg_to
            )
            outflow_usd = sum_stable_logs(out_logs, contract_decimals)
            total_outflow += outflow_usd
            n_log += len(out_logs)

            seg_from = seg_to + 1
            # 节流：段间等待 0.15s，避免公开 RPC 限流
            if seg_from <= blk_to:
                await asyncio.sleep(0.15)

        net = total_inflow - total_outflow
        log.debug(
            "EVM stable flow: inflow=%.2f outflow=%.2f net=%.2f n_log=%d blocks=%d",
            total_inflow, total_outflow, net, n_log, block_window,
        )
        return {
            "inflow":  total_inflow,
            "outflow": total_outflow,
            "net":     net,
            "n_log":   n_log,
            "blocks":  block_window,
        }


# ---------------------------------------------------------------------------
# 纯函数：24h 流量计算（无网络依赖，便于单测）
# ---------------------------------------------------------------------------

def btc_flow_24h(
    txs: list[dict],
    addr: str,
    now_ms: int,
    window_ms: int = 86_400_000,
) -> dict:
    """从交易列表计算指定地址的 24h BTC 流入/流出/净流量。

    参数：
        txs        blockstream /txs 返回的交易列表（含 vin/vout/status）
        addr       目标地址
        now_ms     当前时间戳（毫秒）
        window_ms  统计窗口（默认 24h = 86_400_000 ms）

    返回：
        {
          "inflow_btc":  float,  # vout 中流入 addr 的 BTC 总量
          "outflow_btc": float,  # vin 中从 addr 流出的 BTC 总量
          "net_btc":     float,  # inflow - outflow（正=净流入/潜在抛压）
          "n_tx":        int,    # 计入统计的 tx 数量
        }

    过滤规则：
      - 仅统计 status.confirmed == True 的 tx（未确认不算）
      - 仅统计 block_time（秒）换算到 ms 后 >= now_ms - window_ms 的 tx
      - 用 .get() 全程防御缺字段/空结构
    """
    cutoff_ms = now_ms - window_ms
    inflow_sats: int = 0
    outflow_sats: int = 0
    n_tx: int = 0

    for tx in txs:
        status = tx.get("status") or {}
        # 仅统计已确认交易
        if not status.get("confirmed"):
            continue
        # 仅统计 24h 窗口内的交易（block_time 单位：秒）
        block_time_s = status.get("block_time")
        if block_time_s is None:
            continue
        if block_time_s * 1000 < cutoff_ms:
            continue

        n_tx += 1

        # 流入：本 tx 的 vout 中目标地址收到的 sats
        for vout in tx.get("vout") or []:
            if vout.get("scriptpubkey_address") == addr:
                inflow_sats += int(vout.get("value") or 0)

        # 流出：本 tx 的 vin 中从目标地址花出的 sats
        for vin in tx.get("vin") or []:
            prevout = vin.get("prevout") or {}
            if prevout.get("scriptpubkey_address") == addr:
                outflow_sats += int(prevout.get("value") or 0)

    return {
        "inflow_btc":  inflow_sats  / 1e8,
        "outflow_btc": outflow_sats / 1e8,
        "net_btc":    (inflow_sats - outflow_sats) / 1e8,
        "n_tx":        n_tx,
    }


# ---------------------------------------------------------------------------
# 告警格式化（单位感知：BTC vs 稳定币/美元，语义方向相反）
# ---------------------------------------------------------------------------

def fmt_flow_alert(row: dict) -> str:
    """把一行 poll_once 结果格式化为可读告警文本。

    语义（chain 感知，BTC 与稳定币方向相反）：
      BTC：
        净流入交易所（正 net）= 潜在抛压（庄要卖？）→ 🔴
        净流出交易所（负 net）= 可能吸筹/囤币       → 🟢
      ETH 稳定币（USDT/USDC）：
        净流入交易所（正 net）= 买盘弹药进场         → 🟢
        净流出交易所（负 net）= 资金撤离             → 🔴

    row 字段：exchange, chain, inflow, outflow, net, n_addr, n_tx, alert

    注：BTC 的 n_tx 统计来自分页抓取（最多约 max_pages×25 笔），对极端高频热钱包可能低估。
        ETH 稳定币的 n_tx 为 Transfer event log 条数。
    """
    exchange: str  = row.get("exchange", "?")
    chain: str     = row.get("chain", "BTC")
    inflow: float  = row.get("inflow", 0.0)
    outflow: float = row.get("outflow", 0.0)
    net: float     = row.get("net", 0.0)
    n_addr: int    = row.get("n_addr", 0)
    n_tx: int      = row.get("n_tx", 0)

    is_btc = chain == "BTC"

    if is_btc:
        # BTC：净流入=抛压🔴，净流出=吸筹🟢
        direction = "净流入🔴" if net >= 0 else "净流出🟢"
        amt_str = f"{abs(net):,.0f} BTC"
        in_str  = f"{inflow:,.0f}"
        out_str = f"{outflow:,.0f}"
        unit    = "BTC"
    else:
        # 稳定币：净流入=买盘弹药🟢，净流出=撤离🔴
        direction = "净流入🟢" if net >= 0 else "净流出🔴"
        amt_str = f"${abs(net) / 1e6:,.1f}M USDT/USDC"
        in_str  = f"${inflow / 1e6:,.1f}M"
        out_str = f"${outflow / 1e6:,.1f}M"
        unit    = "稳定币"

    return (
        f"🏦 {exchange} {chain} 近期{unit}流 {direction} {amt_str} "
        f"(流入{in_str}/流出{out_str}, {n_addr}址{n_tx}笔)"
    )


# ---------------------------------------------------------------------------
# 监控器主类
# ---------------------------------------------------------------------------

class ExchangeFlowMonitor:
    """交易所链上资金流监控器（BTC + EVM 稳定币）。

    registry 格式（来自 config/exchange_wallets.yaml 的 exchanges 字段）：
        {
          "Binance": {
            "BTC": [{"addr": "...", "label": "cold"}, ...],
            "ETH": [{"addr": "0x...", "label": "hot"}, ...]
          },
          ...
        }

    evm_cfg 格式（来自 yaml 的 evm 字段，可选）：
        {
          "rpc": "https://...",
          "block_window": 600,
          "chunk": 150,
          "stablecoins": [{"symbol": "USDT", "contract": "0x...", "decimals": 6}, ...],
          "threshold_usd": 2000000
        }

    threshold_btc：|net| 超过此值触发 BTC alert 标记（默认 500 BTC）。
    evm_cfg["threshold_usd"]：|net_usd| 超过此值触发 ETH 稳定币 alert（默认 $2M）。

    说明：地址为公开已知种子，可能不全；只计已确认 tx（BTC）或已上链 log（ETH）；
    poll_once 的 BTC 路径使用 address_txs_window 分页（最多约 max_pages×25 笔），
    对极端高频热钱包（24h >150 笔）仍可能低估，属已知局限。
    ETH 路径受公开 RPC 单次结果上限影响，窗口可通过 evm_cfg["block_window"] 调节。
    """

    def __init__(
        self,
        store: Any,
        registry: dict[str, dict[str, list[dict]]],
        *,
        threshold_btc: float = 500.0,
        client: BlockstreamClient | None = None,
        evm_cfg: dict | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.threshold_btc = threshold_btc
        self.client = client or BlockstreamClient()
        # EVM 稳定币流配置（可选，None 时跳过 ETH 路径）
        self.evm_cfg: dict | None = evm_cfg
        self._evm: EVMStableFlow | None = EVMStableFlow() if evm_cfg else None
        # 建表（自管，不写进 storage/db.py）
        store.conn.executescript(_SCHEMA)

    # ---- 落库 ----

    def _insert_row(
        self,
        ts: int,
        exchange: str,
        chain: str,
        inflow: float,
        outflow: float,
        net: float,
        n_tx: int,
        n_addr: int,
    ) -> None:
        self.store.conn.execute(
            "INSERT INTO exchange_flows"
            "(ts, dt, exchange, chain, inflow, outflow, net, n_tx, n_addr) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, fmt_ts(ts), exchange, chain,
             inflow, outflow, net, n_tx, n_addr),
        )

    # ---- 主轮询 ----

    async def poll_once(
        self,
        now_ms: int,
        session: aiohttp.ClientSession | None = None,
    ) -> list[dict]:
        """对 registry 中每个交易所的各链地址查资金流向，汇总落库。

        BTC 路径：每个地址查 24h 分页 tx，btc_flow_24h 汇总（单位 BTC）。
        ETH 路径（evm_cfg 存在时）：批量查所有 ETH 地址的 USDT/USDC Transfer（单位 USD）。

        返回：[{exchange, chain, inflow, outflow, net, n_tx, n_addr, alert}, ...]
              BTC 行：alert = |net_btc| >= threshold_btc
              ETH 行：alert = |net_usd| >= evm_cfg["threshold_usd"]

        地址间等待 0.3s 节流（BTC），避免触发公开 API 限流。
        单地址失败时 try/except continue，不中断整批。
        """
        own = session is None
        if own:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        results: list[dict] = []
        try:
            for exchange, chains in self.registry.items():
                # ---- BTC 路径 ----
                btc_addr_list = chains.get("BTC") or []
                if btc_addr_list:
                    agg_inflow:  float = 0.0
                    agg_outflow: float = 0.0
                    agg_n_tx:    int   = 0
                    n_addr_ok:   int   = 0

                    for entry in btc_addr_list:
                        addr = entry.get("addr", "")
                        label = entry.get("label", "")
                        if not addr:
                            continue
                        try:
                            txs = await self.client.address_txs_window(session, addr, now_ms)
                            flow = btc_flow_24h(txs, addr, now_ms)
                            agg_inflow  += flow["inflow_btc"]
                            agg_outflow += flow["outflow_btc"]
                            agg_n_tx    += flow["n_tx"]
                            n_addr_ok   += 1
                            log.debug(
                                "%s BTC [%s] %s inflow=%.2f outflow=%.2f n_tx=%d",
                                exchange, label, addr,
                                flow["inflow_btc"], flow["outflow_btc"], flow["n_tx"],
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "poll_once BTC 单地址失败 %s %s: %s",
                                exchange, addr, exc,
                            )
                            continue
                        await asyncio.sleep(0.3)

                    net = agg_inflow - agg_outflow
                    self._insert_row(
                        now_ms, exchange, "BTC",
                        agg_inflow, agg_outflow, net, agg_n_tx, n_addr_ok,
                    )
                    results.append({
                        "exchange": exchange,
                        "chain":    "BTC",
                        "inflow":   agg_inflow,
                        "outflow":  agg_outflow,
                        "net":      net,
                        "n_tx":     agg_n_tx,
                        "n_addr":   n_addr_ok,
                        "alert":    abs(net) >= self.threshold_btc,
                    })
                else:
                    log.debug("跳过 %s BTC（无注册地址）", exchange)

                # ---- ETH 稳定币路径 ----
                eth_addr_list = chains.get("ETH") or []
                if eth_addr_list and self._evm is not None and self.evm_cfg:
                    eth_addrs = [e["addr"] for e in eth_addr_list if e.get("addr")]
                    if not eth_addrs:
                        continue
                    try:
                        stable = await self._evm.exchange_stable_flow(
                            session=session,
                            rpc=self.evm_cfg["rpc"],
                            addrs=eth_addrs,
                            stablecoins=self.evm_cfg.get("stablecoins", []),
                            block_window=self.evm_cfg.get("block_window", 600),
                            chunk=self.evm_cfg.get("chunk", 150),
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("poll_once ETH stable 失败 %s: %s", exchange, exc)
                        continue

                    threshold_usd: float = float(
                        self.evm_cfg.get("threshold_usd", 2_000_000)
                    )
                    net_usd = stable["net"]
                    self._insert_row(
                        now_ms, exchange, "ETH",
                        stable["inflow"], stable["outflow"], net_usd,
                        stable["n_log"], len(eth_addrs),
                    )
                    results.append({
                        "exchange": exchange,
                        "chain":    "ETH",
                        "inflow":   stable["inflow"],
                        "outflow":  stable["outflow"],
                        "net":      net_usd,
                        "n_tx":     stable["n_log"],
                        "n_addr":   len(eth_addrs),
                        "alert":    abs(net_usd) >= threshold_usd,
                    })
                    log.debug(
                        "%s ETH stable inflow=%.2f outflow=%.2f net=%.2f n_log=%d",
                        exchange, stable["inflow"], stable["outflow"], net_usd, stable["n_log"],
                    )
                else:
                    if eth_addr_list and self._evm is None:
                        log.debug("跳过 %s ETH（evm_cfg 未配置）", exchange)

        finally:
            if own and session is not None:
                await session.close()

        return results

    # ---- 历史查询 ----

    def recent(
        self,
        exchange: str | None,
        since_ms: int,
    ) -> list[tuple]:
        """查询 exchange_flows 历史记录。

        exchange=None 时返回所有交易所；否则过滤指定交易所。
        返回按 ts DESC 排列的 tuple 列表。
        """
        if exchange is None:
            return self.store.conn.execute(
                "SELECT * FROM exchange_flows WHERE ts >= ? ORDER BY ts DESC",
                (since_ms,),
            ).fetchall()
        return self.store.conn.execute(
            "SELECT * FROM exchange_flows WHERE exchange=? AND ts >= ? ORDER BY ts DESC",
            (exchange, since_ms),
        ).fetchall()
