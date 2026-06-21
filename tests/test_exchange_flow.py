"""交易所链上资金流单测（确定性，无网络）。

测试覆盖：
  1. btc_flow_24h 纯函数：合成 txs 验证 inflow/outflow/net/n_tx。
  2. ExchangeFlowMonitor.poll_once：假 client 注入，验证落库、净流聚合、alert 阈值。
  3. fmt_flow_alert 文本格式验证（BTC 和 ETH 稳定币两种单位/语义）。
  4. 注册表结构校验（dict 构造，无需读 yaml）。
  5. EVMStableFlow 相关：
     5a. sum_stable_logs 纯函数（按 decimals 换算/多合约聚合）。
     5b. poll_once 带 evm_cfg + 假 EVMStableFlow 产出 ETH 行、alert 阈值、落库。
     5c. _pad_addr 地址填充格式正确。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.onchain.exchange_flow import (
    BlockstreamClient,
    EVMStableFlow,
    ExchangeFlowMonitor,
    _pad_addr,
    btc_flow_24h,
    fmt_flow_alert,
    sum_stable_logs,
)
from smc_tracker.storage import Store

# ---------------------------------------------------------------------------
# 辅助：构造合成 tx
# ---------------------------------------------------------------------------

_NOW_MS  = 1_700_000_000_000   # 固定"当前"时间戳，便于断言
_WIN_MS  = 86_400_000          # 24h 窗口
_ADDR    = "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo"   # 目标地址（Binance cold）


def _confirmed_tx(
    block_time_s: int,
    vout_to_addr: int = 0,     # sats 流入 _ADDR
    vin_from_addr: int = 0,    # sats 从 _ADDR 流出
) -> dict:
    """构造一笔已确认 tx（block_time 单位：秒）。"""
    tx: dict = {
        "txid": f"tx_{block_time_s}_{vout_to_addr}_{vin_from_addr}",
        "status": {"confirmed": True, "block_height": 800000, "block_time": block_time_s},
        "vin": [],
        "vout": [],
    }
    if vout_to_addr:
        tx["vout"].append({"scriptpubkey_address": _ADDR, "value": vout_to_addr})
    if vin_from_addr:
        tx["vin"].append({"prevout": {"scriptpubkey_address": _ADDR, "value": vin_from_addr}})
    return tx


def _unconfirmed_tx(vout_to_addr: int = 0, vin_from_addr: int = 0) -> dict:
    """构造一笔未确认 tx（应被 btc_flow_24h 过滤）。"""
    tx: dict = {
        "txid": "unconfirmed_tx",
        "status": {"confirmed": False},
        "vin": [],
        "vout": [],
    }
    if vout_to_addr:
        tx["vout"].append({"scriptpubkey_address": _ADDR, "value": vout_to_addr})
    if vin_from_addr:
        tx["vin"].append({"prevout": {"scriptpubkey_address": _ADDR, "value": vin_from_addr}})
    return tx


# ---------------------------------------------------------------------------
# 1. btc_flow_24h 纯函数测试
# ---------------------------------------------------------------------------

def test_btc_flow_24h_basic():
    """一笔流入 + 一笔流出，验证 inflow/outflow/net/n_tx 换算正确。"""
    now_s = _NOW_MS // 1000
    txs = [
        _confirmed_tx(now_s - 3600, vout_to_addr=100_000_000),   # 1 BTC 流入
        _confirmed_tx(now_s - 7200, vin_from_addr=50_000_000),   # 0.5 BTC 流出
    ]
    result = btc_flow_24h(txs, _ADDR, _NOW_MS)
    assert abs(result["inflow_btc"]  - 1.0) < 1e-9
    assert abs(result["outflow_btc"] - 0.5) < 1e-9
    assert abs(result["net_btc"]     - 0.5) < 1e-9
    assert result["n_tx"] == 2


def test_btc_flow_24h_excludes_old_tx():
    """25h 前的已确认 tx 应被窗口过滤（不计入统计）。"""
    now_s = _NOW_MS // 1000
    old_block_time_s = now_s - 90_000   # 25h 前，超出 24h 窗口
    txs = [
        _confirmed_tx(old_block_time_s, vout_to_addr=200_000_000),   # 2 BTC，应排除
        _confirmed_tx(now_s - 3600, vout_to_addr=50_000_000),        # 0.5 BTC，应计入
    ]
    result = btc_flow_24h(txs, _ADDR, _NOW_MS)
    assert abs(result["inflow_btc"] - 0.5) < 1e-9
    assert result["n_tx"] == 1


def test_btc_flow_24h_excludes_unconfirmed():
    """未确认 tx（status.confirmed=False）必须排除。"""
    now_s = _NOW_MS // 1000
    txs = [
        _unconfirmed_tx(vout_to_addr=300_000_000),           # 3 BTC 未确认，应排除
        _confirmed_tx(now_s - 1800, vout_to_addr=20_000_000),  # 0.2 BTC 已确认
    ]
    result = btc_flow_24h(txs, _ADDR, _NOW_MS)
    assert abs(result["inflow_btc"] - 0.2) < 1e-9
    assert result["n_tx"] == 1


def test_btc_flow_24h_wrong_addr_ignored():
    """vout/vin 中非目标地址的条目不参与计算。"""
    now_s = _NOW_MS // 1000
    other_addr = "1ABCxyz"
    tx: dict = {
        "txid": "t1",
        "status": {"confirmed": True, "block_time": now_s - 100},
        "vout": [
            {"scriptpubkey_address": other_addr, "value": 999_000_000},  # 其它地址，忽略
            {"scriptpubkey_address": _ADDR,      "value": 10_000_000},   # 0.1 BTC 流入
        ],
        "vin": [
            {"prevout": {"scriptpubkey_address": other_addr, "value": 500_000_000}},  # 忽略
        ],
    }
    result = btc_flow_24h([tx], _ADDR, _NOW_MS)
    assert abs(result["inflow_btc"] - 0.1) < 1e-9
    assert result["outflow_btc"] == 0.0


def test_btc_flow_24h_empty():
    """空列表 → 全零结果。"""
    result = btc_flow_24h([], _ADDR, _NOW_MS)
    assert result == {"inflow_btc": 0.0, "outflow_btc": 0.0, "net_btc": 0.0, "n_tx": 0}


def test_btc_flow_24h_missing_fields():
    """缺少 vout/vin/prevout 字段时不 crash（.get 防御）。"""
    now_s = _NOW_MS // 1000
    tx_no_vin: dict = {
        "txid": "t_novin",
        "status": {"confirmed": True, "block_time": now_s - 60},
        # vin 缺失
        "vout": [{"scriptpubkey_address": _ADDR, "value": 5_000_000}],
    }
    tx_no_vout: dict = {
        "txid": "t_novout",
        "status": {"confirmed": True, "block_time": now_s - 60},
        "vin": [{"prevout": {"scriptpubkey_address": _ADDR, "value": 2_000_000}}],
        # vout 缺失
    }
    result = btc_flow_24h([tx_no_vin, tx_no_vout], _ADDR, _NOW_MS)
    assert abs(result["inflow_btc"]  - 0.05) < 1e-9
    assert abs(result["outflow_btc"] - 0.02) < 1e-9
    assert result["n_tx"] == 2


# ---------------------------------------------------------------------------
# 2. ExchangeFlowMonitor.poll_once 测试（假 client，无网络）
# ---------------------------------------------------------------------------

class _FakeClient:
    """按地址返回预设 txs，模拟 blockstream API（不联网）。

    address_txs_window 直接返回 addr_txs_map[addr]（合并后的完整列表），
    模拟分页已完成的结果，供 poll_once 调用验证。
    """

    def __init__(self, addr_txs_map: dict[str, list[dict]]) -> None:
        """addr_txs_map: {地址: tx列表}，未注册地址返回 []。"""
        self._map = addr_txs_map

    async def address_txs(
        self,
        session: object,
        addr: str,
    ) -> list[dict]:
        return self._map.get(addr, [])

    async def address_txs_window(
        self,
        session: object,
        addr: str,
        now_ms: int,
        window_ms: int = 86_400_000,
        max_pages: int = 6,
    ) -> list[dict]:
        """返回预设的完整 tx 列表（模拟分页已完成）。"""
        return self._map.get(addr, [])

    async def address_stats(
        self,
        session: object,
        addr: str,
    ) -> dict | None:
        return None


def _make_store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "test_ef.db")


# 构造测试用 registry（直接用 dict，无需读 yaml）
_REGISTRY: dict[str, dict[str, list[dict]]] = {
    "Binance": {
        "BTC": [
            {"addr": "addr_cold", "label": "cold"},
            {"addr": "addr_hot",  "label": "hot"},
        ]
    },
    "OKX": {
        "BTC": [
            {"addr": "addr_okx", "label": "cold"},
        ]
    },
    "Bitget": {
        "BTC": []   # 空列表，应被跳过，不产生结果行
    },
}


def _make_txs(now_ms: int, inflow_sats: int = 0, outflow_sats: int = 0) -> list[dict]:
    """生成含一笔 inflow 和一笔 outflow 的 txs，block_time 在窗口内。"""
    now_s = now_ms // 1000
    txs = []
    if inflow_sats:
        txs.append({
            "txid": f"in_{inflow_sats}",
            "status": {"confirmed": True, "block_time": now_s - 3600},
            "vin": [],
            "vout": [{"scriptpubkey_address": "addr_cold", "value": inflow_sats}],
        })
    if outflow_sats:
        txs.append({
            "txid": f"out_{outflow_sats}",
            "status": {"confirmed": True, "block_time": now_s - 7200},
            "vin": [{"prevout": {"scriptpubkey_address": "addr_cold", "value": outflow_sats}}],
            "vout": [],
        })
    return txs


def test_poll_once_inserts_rows():
    """poll_once 应为 Binance/OKX 各落库一行（Bitget 无地址，跳过）。"""
    store = _make_store()
    now_ms = _NOW_MS

    client = _FakeClient({
        "addr_cold": _make_txs(now_ms, inflow_sats=100_000_000, outflow_sats=0),
        "addr_hot":  [],
        "addr_okx":  [],
    })
    mon = ExchangeFlowMonitor(store, _REGISTRY, threshold_btc=500.0, client=client)
    results = asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))

    # Bitget 无地址，只有 Binance + OKX 两条
    assert len(results) == 2

    exchanges = {r["exchange"] for r in results}
    assert "Binance" in exchanges
    assert "OKX" in exchanges
    assert "Bitget" not in exchanges

    # 落库行数也应为 2
    count = store.conn.execute("SELECT COUNT(*) FROM exchange_flows").fetchone()[0]
    assert count == 2

    store.close()


def test_poll_once_net_aggregation():
    """多地址 inflow/outflow 汇总正确：Binance cold+hot 累加。"""
    store = _make_store()
    now_ms = _NOW_MS
    now_s = now_ms // 1000

    # cold: 1 BTC 流入；hot: 0.5 BTC 流出
    cold_txs = [{
        "txid": "cold_in",
        "status": {"confirmed": True, "block_time": now_s - 1800},
        "vin": [],
        "vout": [{"scriptpubkey_address": "addr_cold", "value": 100_000_000}],
    }]
    hot_txs = [{
        "txid": "hot_out",
        "status": {"confirmed": True, "block_time": now_s - 3600},
        "vin": [{"prevout": {"scriptpubkey_address": "addr_hot", "value": 50_000_000}}],
        "vout": [],
    }]

    client = _FakeClient({"addr_cold": cold_txs, "addr_hot": hot_txs, "addr_okx": []})
    mon = ExchangeFlowMonitor(store, _REGISTRY, threshold_btc=500.0, client=client)
    results = asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))

    binance_row = next(r for r in results if r["exchange"] == "Binance")
    assert abs(binance_row["inflow"]  - 1.0) < 1e-9
    assert abs(binance_row["outflow"] - 0.5) < 1e-9
    assert abs(binance_row["net"]     - 0.5) < 1e-9
    assert binance_row["n_tx"]   == 2
    assert binance_row["n_addr"] == 2

    store.close()


def test_poll_once_alert_threshold():
    """alert 当且仅当 |net| >= threshold_btc。"""
    store = _make_store()
    now_ms = _NOW_MS
    now_s = now_ms // 1000

    # 净流入 1000 BTC（超过默认阈值 500）
    big_inflow_txs = [{
        "txid": "big",
        "status": {"confirmed": True, "block_time": now_s - 100},
        "vin": [],
        "vout": [{"scriptpubkey_address": "addr_cold", "value": 100_000_000_000}],  # 1000 BTC
    }]

    client = _FakeClient({"addr_cold": big_inflow_txs, "addr_hot": [], "addr_okx": []})
    mon = ExchangeFlowMonitor(store, _REGISTRY, threshold_btc=500.0, client=client)
    results = asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))

    binance_row = next(r for r in results if r["exchange"] == "Binance")
    assert binance_row["alert"] is True   # 1000 BTC > 500 BTC 阈值

    okx_row = next(r for r in results if r["exchange"] == "OKX")
    assert okx_row["alert"] is False      # OKX 无 tx → net=0 < 500

    store.close()


def test_poll_once_below_threshold_no_alert():
    """低于阈值时 alert=False。"""
    store = _make_store()
    now_ms = _NOW_MS
    now_s = now_ms // 1000

    small_txs = [{
        "txid": "small",
        "status": {"confirmed": True, "block_time": now_s - 60},
        "vin": [],
        "vout": [{"scriptpubkey_address": "addr_cold", "value": 1_000_000}],  # 0.01 BTC
    }]

    client = _FakeClient({"addr_cold": small_txs, "addr_hot": [], "addr_okx": []})
    mon = ExchangeFlowMonitor(store, _REGISTRY, threshold_btc=500.0, client=client)
    results = asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))

    binance_row = next(r for r in results if r["exchange"] == "Binance")
    assert binance_row["alert"] is False

    store.close()


def test_recent_query():
    """recent() 过滤 exchange 与 since_ms 正确。"""
    store = _make_store()
    now_ms = _NOW_MS

    client = _FakeClient({"addr_cold": [], "addr_hot": [], "addr_okx": []})
    mon = ExchangeFlowMonitor(store, _REGISTRY, threshold_btc=500.0, client=client)
    asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))

    # 查全部（exchange=None）
    all_rows = mon.recent(None, since_ms=0)
    assert len(all_rows) == 2  # Binance + OKX

    # 只查 Binance
    binance_rows = mon.recent("Binance", since_ms=0)
    assert len(binance_rows) == 1
    assert binance_rows[0][3] == "Binance"   # exchange 字段（第4列，idx=3，0-based: id,ts,dt,exchange...）

    # since_ms 在未来 → 无结果
    future_rows = mon.recent(None, since_ms=now_ms + 1_000_000)
    assert future_rows == []

    store.close()


# ---------------------------------------------------------------------------
# 3. fmt_flow_alert 格式验证
# ---------------------------------------------------------------------------

def test_fmt_flow_alert_net_inflow():
    """净流入（正 net）→ 含 🔴 + 潜在抛压语义 + 交易所名 + BTC。"""
    row = {
        "exchange": "Binance",
        "chain":    "BTC",
        "inflow":   1000.0,
        "outflow":  200.0,
        "net":      800.0,
        "n_addr":   3,
        "n_tx":     12,
        "alert":    True,
    }
    text = fmt_flow_alert(row)
    assert "Binance" in text
    assert "BTC" in text
    assert "🔴" in text
    assert "800" in text
    assert "1,000" in text   # inflow 千分位
    assert "200" in text


def test_fmt_flow_alert_net_outflow():
    """净流出（负 net）→ 含 🟢（吸筹语义）。"""
    row = {
        "exchange": "OKX",
        "chain":    "BTC",
        "inflow":   50.0,
        "outflow":  600.0,
        "net":      -550.0,
        "n_addr":   1,
        "n_tx":     5,
        "alert":    True,
    }
    text = fmt_flow_alert(row)
    assert "OKX" in text
    assert "🟢" in text
    assert "550" in text


# ---------------------------------------------------------------------------
# 4. 注册表结构校验（dict 构造，无需读 yaml）
# ---------------------------------------------------------------------------

def test_registry_structure():
    """验证 registry 格式：exchange → chain → [{addr, label}]。"""
    registry = {
        "Binance": {
            "BTC": [
                {"addr": "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo", "label": "cold"},
                {"addr": "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h", "label": "hot"},
            ]
        },
        "OKX": {
            "BTC": [
                {"addr": "3LQUu4v9z6KNch71j7kbj8GPeAGUo1FW6a", "label": "cold"},
            ]
        },
        "Bitget": {
            "BTC": []   # 待补
        },
    }

    # 基本结构断言
    assert "Binance" in registry
    assert "OKX" in registry
    assert "Bitget" in registry

    # Binance BTC 有 3 个地址（cold/hot/agg；此 mini-registry 只有 2 个，测结构即可）
    for entry in registry["Binance"]["BTC"]:
        assert "addr" in entry
        assert "label" in entry
        assert entry["addr"]   # 非空

    # Bitget 显式留空
    assert registry["Bitget"]["BTC"] == []

    # OKX cold 地址格式（P2SH 以 3 开头）
    okx_addr = registry["OKX"]["BTC"][0]["addr"]
    assert okx_addr.startswith("3")


# ---------------------------------------------------------------------------
# 5. poll_once 走分页路径（address_txs_window）测试
# ---------------------------------------------------------------------------

class _PaginatedFakeClient:
    """模拟分页：第一页返回窗口内 txs，第二页返回超 24h 的旧 tx。
    address_txs_window 把两页合并返回，供 btc_flow_24h 过滤超窗口的旧 tx。
    """

    def __init__(
        self,
        addr: str,
        page1_txs: list[dict],
        page2_txs: list[dict],
    ) -> None:
        self._addr = addr
        self._page1 = page1_txs
        self._page2 = page2_txs
        # 记录调用次数，验证 poll_once 调用的是新方法而非旧方法
        self.address_txs_window_calls: int = 0
        self.address_txs_calls: int = 0

    async def address_txs(self, session: object, addr: str) -> list[dict]:
        self.address_txs_calls += 1
        return []

    async def address_txs_window(
        self,
        session: object,
        addr: str,
        now_ms: int,
        window_ms: int = 86_400_000,
        max_pages: int = 6,
    ) -> list[dict]:
        """返回两页合并后的 tx 列表（含超窗口的旧 tx，由 btc_flow_24h 过滤）。"""
        self.address_txs_window_calls += 1
        if addr == self._addr:
            return self._page1 + self._page2
        return []

    async def address_stats(self, session: object, addr: str) -> dict | None:
        return None


def test_poll_once_uses_address_txs_window():
    """poll_once 必须调用 address_txs_window（而非旧的 address_txs）。"""
    store = _make_store()
    now_ms = _NOW_MS
    now_s = now_ms // 1000

    # 页1：窗口内 tx（1 BTC 流入 addr_cold）
    page1 = [{
        "txid": "p1_in",
        "status": {"confirmed": True, "block_time": now_s - 3600},
        "vin": [],
        "vout": [{"scriptpubkey_address": "addr_cold", "value": 100_000_000}],
    }]
    # 页2：超 24h 的旧 tx（应被 btc_flow_24h 过滤，不计入统计）
    page2 = [{
        "txid": "p2_old",
        "status": {"confirmed": True, "block_time": now_s - 90_000},  # 25h 前
        "vin": [],
        "vout": [{"scriptpubkey_address": "addr_cold", "value": 200_000_000}],
    }]

    registry: dict = {
        "Binance": {"BTC": [{"addr": "addr_cold", "label": "cold"}]},
    }
    client = _PaginatedFakeClient("addr_cold", page1, page2)
    mon = ExchangeFlowMonitor(store, registry, threshold_btc=500.0, client=client)
    results = asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))

    # 确认 poll_once 走了新路径
    assert client.address_txs_window_calls >= 1, (
        "poll_once 应调用 address_txs_window（分页路径），实际未调用"
    )
    assert client.address_txs_calls == 0, (
        "poll_once 不应再调用旧的 address_txs"
    )

    # btc_flow_24h 只计窗口内的页1 tx（1 BTC），页2 旧 tx 被过滤
    assert len(results) == 1
    row = results[0]
    assert row["exchange"] == "Binance"
    assert abs(row["inflow"] - 1.0) < 1e-9, f"inflow 期望 1.0，实得 {row['inflow']}"
    assert row["n_tx"] == 1, f"n_tx 期望 1（超窗口旧 tx 被过滤），实得 {row['n_tx']}"

    store.close()


def test_poll_once_paginated_two_page_aggregation():
    """分页两页窗口内 tx 均计入（模拟分页正常累积场景）。"""
    store = _make_store()
    now_ms = _NOW_MS
    now_s = now_ms // 1000

    # 页1：1 BTC 流入
    page1 = [{
        "txid": "pg1",
        "status": {"confirmed": True, "block_time": now_s - 3600},
        "vin": [],
        "vout": [{"scriptpubkey_address": "addr_cold", "value": 100_000_000}],
    }]
    # 页2：0.5 BTC 流入（仍在 24h 窗口内）
    page2 = [{
        "txid": "pg2",
        "status": {"confirmed": True, "block_time": now_s - 7200},
        "vin": [],
        "vout": [{"scriptpubkey_address": "addr_cold", "value": 50_000_000}],
    }]

    registry: dict = {
        "Binance": {"BTC": [{"addr": "addr_cold", "label": "cold"}]},
    }
    client = _PaginatedFakeClient("addr_cold", page1, page2)
    mon = ExchangeFlowMonitor(store, registry, threshold_btc=500.0, client=client)
    results = asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))

    row = results[0]
    # 两页 tx 都在窗口内，合计 1.5 BTC 流入
    assert abs(row["inflow"] - 1.5) < 1e-9, f"inflow 期望 1.5，实得 {row['inflow']}"
    assert row["n_tx"] == 2, f"n_tx 期望 2（两页各一笔），实得 {row['n_tx']}"

    store.close()


# ---------------------------------------------------------------------------
# 6. address_txs_window 停止条件（用真实 BlockstreamClient + 假 session）
# ---------------------------------------------------------------------------

class _MockResponse:
    """模拟 aiohttp response 的 async context manager。"""
    def __init__(self, status: int, data: list) -> None:
        self.status = status
        self._data = data

    async def json(self, content_type: str | None = None) -> list:
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _MockSession:
    """按 URL 序列返回预设 response，模拟分页请求。"""

    def __init__(self, responses: list[tuple[int, list]]) -> None:
        """responses: [(status, data), ...]，按请求顺序消费。"""
        self._queue = list(responses)
        self.called_urls: list[str] = []

    def get(self, url: str, timeout: object = None) -> _MockResponse:
        self.called_urls.append(url)
        if not self._queue:
            return _MockResponse(200, [])
        status, data = self._queue.pop(0)
        return _MockResponse(status, data)


def test_address_txs_window_stops_at_boundary():
    """address_txs_window 在最后一笔已确认 tx 超过 24h 时停止分页。"""
    now_ms = _NOW_MS
    now_s = now_ms // 1000

    # 首页：1 笔在窗口内，1 笔已超 24h（block_time = now_s - 90_000，即 25h 前）
    # 分页锚点的 block_time 超窗口，不应再发第二页请求
    page1_data = [
        {
            "txid": "tx_recent",
            "status": {"confirmed": True, "block_time": now_s - 3600},
            "vin": [], "vout": [],
        },
        {
            "txid": "tx_old_anchor",
            "status": {"confirmed": True, "block_time": now_s - 90_000},  # 25h 前
            "vin": [], "vout": [],
        },
    ]

    session = _MockSession([(200, page1_data)])
    client = BlockstreamClient(base_url="https://fake.test/api")

    result = asyncio.run(
        client.address_txs_window(session, _ADDR, now_ms, window_ms=86_400_000, max_pages=6)
    )

    # 只应请求了首页，未发分页请求（锚点超窗口即停止）
    assert len(session.called_urls) == 1, (
        f"期望只发 1 次请求（停止分页），实际发 {len(session.called_urls)} 次"
    )
    assert len(result) == 2  # 首页两条 tx 都返回（过滤由 btc_flow_24h 完成）


def test_address_txs_window_graceful_on_error():
    """address_txs_window 在分页请求失败时降级返回已积累的 acc。"""
    now_ms = _NOW_MS
    now_s = now_ms // 1000

    # 首页正常，内含一笔在窗口内的已确认 tx（block_time = now_s - 3600）
    page1_data = [{
        "txid": "tx_p1",
        "status": {"confirmed": True, "block_time": now_s - 3600},
        "vin": [], "vout": [],
    }]
    # 第二个请求返回 500（模拟服务器故障）
    session = _MockSession([(200, page1_data), (500, [])])
    client = BlockstreamClient(base_url="https://fake.test/api")

    result = asyncio.run(
        client.address_txs_window(session, _ADDR, now_ms, window_ms=86_400_000, max_pages=6)
    )

    # 首页 tx 应保留（acc 中的内容），分页失败后优雅降级而不丢弃已积累数据
    assert len(result) == 1, f"期望 1 条 tx（首页积累），实得 {len(result)}"
    assert result[0]["txid"] == "tx_p1"


def test_address_txs_window_empty_first_page():
    """首页为空时直接返回空列表，不发分页请求。"""
    session = _MockSession([(200, [])])
    client = BlockstreamClient(base_url="https://fake.test/api")

    result = asyncio.run(
        client.address_txs_window(session, _ADDR, _NOW_MS, window_ms=86_400_000, max_pages=6)
    )

    assert result == []
    assert len(session.called_urls) == 1  # 只请求了首页


# ---------------------------------------------------------------------------
# 5a. sum_stable_logs 纯函数测试（无网络）
# ---------------------------------------------------------------------------

# USDT/USDC 合约地址（小写，与 log["address"] 一致）
_USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
_CONTRACT_DECIMALS = {_USDT: 6, _USDC: 6}


def _make_stable_log(contract: str, value_raw: int) -> dict:
    """构造一条合成 eth_getLogs 返回的 Transfer log。"""
    return {
        "address": contract,
        "data": hex(value_raw),
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x000000000000000000000000f977814e90da44bfa03b6295a0616a897441acec",
            "0x000000000000000000000000461249076b88189f8ac9418de28b365859e46bfd",
        ],
    }


def test_sum_stable_logs_usdt():
    """USDT decimals=6，value=1_000_000_000 → 1000 美元。"""
    logs = [_make_stable_log(_USDT, 1_000_000_000)]   # 1000 USDT
    total = sum_stable_logs(logs, _CONTRACT_DECIMALS)
    assert abs(total - 1000.0) < 1e-6


def test_sum_stable_logs_multi_contract():
    """USDT + USDC 两个合约 log 各 $500，合计 $1000。"""
    logs = [
        _make_stable_log(_USDT, 500_000_000),   # $500 USDT
        _make_stable_log(_USDC, 500_000_000),   # $500 USDC
    ]
    total = sum_stable_logs(logs, _CONTRACT_DECIMALS)
    assert abs(total - 1000.0) < 1e-6


def test_sum_stable_logs_empty():
    """空 log 列表 → 0。"""
    assert sum_stable_logs([], _CONTRACT_DECIMALS) == 0.0


def test_sum_stable_logs_unknown_contract_uses_default():
    """未知合约 decimals 默认 6，不崩溃。"""
    log = {"address": "0xunknown", "data": hex(1_000_000)}
    total = sum_stable_logs([log], {})
    assert abs(total - 1.0) < 1e-9  # 1e6 / 1e6 = 1.0（默认 6 位）


def test_sum_stable_logs_bad_data():
    """data 字段缺失或无效时跳过，不崩溃。"""
    logs = [
        {"address": _USDT, "data": ""},           # 空字符串
        {"address": _USDT, "data": "not_hex"},    # 非法 hex
        {"address": _USDT, "data": hex(2_000_000)},  # 正常 $2
    ]
    total = sum_stable_logs(logs, _CONTRACT_DECIMALS)
    assert abs(total - 2.0) < 1e-9


# ---------------------------------------------------------------------------
# 5b. _pad_addr 格式测试
# ---------------------------------------------------------------------------

def test_pad_addr_format():
    """_pad_addr 应输出 0x + 24个0 + 40位小写地址。"""
    addr = "0xF977814e90dA44bFA03b6295A0616a897441aceC"
    padded = _pad_addr(addr)
    # 总长度：2(0x) + 24(零) + 40(地址) = 66
    assert len(padded) == 66
    assert padded.startswith("0x")
    # 中间 24 个字符全是 0
    assert padded[2:26] == "0" * 24
    # 后 40 位是小写地址
    assert padded[26:] == "f977814e90da44bfa03b6295a0616a897441acec"


def test_pad_addr_already_lower():
    """输入小写地址也能正确填充。"""
    addr = "0x461249076b88189f8ac9418de28b365859e46bfd"
    padded = _pad_addr(addr)
    assert len(padded) == 66
    assert padded[26:] == "461249076b88189f8ac9418de28b365859e46bfd"


# ---------------------------------------------------------------------------
# 5c. EVMStableFlow 注入假 session 测试
# ---------------------------------------------------------------------------

class _FakeEVMSession:
    """注入假 session，返回预设 JSON-RPC 响应（按调用顺序消费）。

    每次 .post(...) 返回一个包含 preset 响应的 async context manager。
    """

    def __init__(self, responses: list[dict]) -> None:
        """responses: 按顺序消费的 JSON-RPC body dict 列表。"""
        self._queue = list(responses)

    def post(self, url: str, **kwargs) -> "_FakeEVMContext":
        if not self._queue:
            body: dict = {"jsonrpc": "2.0", "id": 1, "result": []}
        else:
            body = self._queue.pop(0)
        return _FakeEVMContext(body)


class _FakeEVMContext:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.status = 200

    async def json(self, content_type=None) -> dict:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


def _make_block_number_resp(block_hex: str = "0x1000") -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": block_hex}


def _make_logs_resp(logs: list) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": logs}


def test_exchange_stable_flow_basic():
    """exchange_stable_flow 流入/流出/net 换算正确（纯假 session，无网络）。"""
    # 一个 chunk 窗口：block_window=10, chunk=10 → 1次 in + 1次 out
    # 响应顺序：eth_blockNumber, getLogs(in), getLogs(out)
    in_log = _make_stable_log(_USDT, 5_000_000_000)   # $5000 USDT 流入
    out_log = _make_stable_log(_USDC, 2_000_000_000)  # $2000 USDC 流出

    responses = [
        _make_block_number_resp("0x1000"),    # blockNumber = 4096
        _make_logs_resp([in_log]),            # getLogs(in)
        _make_logs_resp([out_log]),           # getLogs(out)
    ]
    session = _FakeEVMSession(responses)
    stablecoins = [
        {"symbol": "USDT", "contract": _USDT, "decimals": 6},
        {"symbol": "USDC", "contract": _USDC, "decimals": 6},
    ]
    evm = EVMStableFlow()
    result = asyncio.run(
        evm.exchange_stable_flow(
            session=session,
            rpc="https://fake-rpc/",
            addrs=["0xF977814e90dA44bFA03b6295A0616a897441aceC"],
            stablecoins=stablecoins,
            block_window=10,
            chunk=10,
        )
    )
    assert abs(result["inflow"] - 5000.0) < 1e-3
    assert abs(result["outflow"] - 2000.0) < 1e-3
    assert abs(result["net"] - 3000.0) < 1e-3
    assert result["n_log"] == 2   # 1 in log + 1 out log


def test_exchange_stable_flow_block_number_failure():
    """eth_blockNumber 失败时返回全零结果（优雅降级）。"""
    # blockNumber 返回 status=200 但 error
    responses = [
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "internal error"}},
    ]

    class _FailBlockSession:
        """blockNumber 直接返回错误，触发降级路径。"""
        def post(self, url: str, **kwargs):
            body = responses[0]
            return _FakeEVMContext(body)

    evm = EVMStableFlow()
    result = asyncio.run(
        evm.exchange_stable_flow(
            session=_FailBlockSession(),
            rpc="https://fake-rpc/",
            addrs=["0xF977814e90dA44bFA03b6295A0616a897441aceC"],
            stablecoins=[{"symbol": "USDT", "contract": _USDT, "decimals": 6}],
            block_window=10,
            chunk=10,
        )
    )
    # blockNumber 返回 error → int("0x...", 16) 失败或 result=None → head=0 → 降级
    # 注：_block_number 取 body.get("result")，error body 无 result → "0x0" → head=0
    assert result["blocks"] == 0 or result["inflow"] == 0.0


# ---------------------------------------------------------------------------
# 5d. poll_once 带 evm_cfg + 假 EVMStableFlow 产出 ETH 行
# ---------------------------------------------------------------------------

class _FakeEVMStableFlow:
    """注入假 EVMStableFlow，直接返回预设结果（不联网）。"""

    def __init__(self, result: dict) -> None:
        self._result = result
        self.call_count = 0

    async def exchange_stable_flow(self, **kwargs) -> dict:
        self.call_count += 1
        return dict(self._result)


def _make_store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "test_ef.db")


# EVM 配置（测试用）
_EVM_CFG: dict = {
    "rpc": "https://fake-rpc/",
    "block_window": 600,
    "chunk": 150,
    "stablecoins": [
        {"symbol": "USDT", "contract": _USDT, "decimals": 6},
        {"symbol": "USDC", "contract": _USDC, "decimals": 6},
    ],
    "threshold_usd": 2_000_000,   # $2M
}

# registry 含 BTC(空) + ETH 地址
_REGISTRY_EVM: dict[str, dict[str, list[dict]]] = {
    "Binance": {
        "BTC": [],
        "ETH": [
            {"addr": "0xF977814e90dA44bFA03b6295A0616a897441aceC", "label": "hot8"},
        ],
    },
}


def test_poll_once_eth_row_produced():
    """poll_once 带 evm_cfg 时，应为 ETH 链产出一行结果并落库。"""
    store = _make_store()
    now_ms = _NOW_MS

    # 假 BTC client（空）
    fake_btc = _FakeClient({})
    # 假 EVM：net=$3M
    fake_evm_result = {"inflow": 5_000_000.0, "outflow": 2_000_000.0,
                       "net": 3_000_000.0, "n_log": 15, "blocks": 600}

    mon = ExchangeFlowMonitor(
        store, _REGISTRY_EVM,
        threshold_btc=500.0, client=fake_btc, evm_cfg=_EVM_CFG
    )
    # 替换内部 EVMStableFlow 为假对象（跳过真实网络）
    fake_evm = _FakeEVMStableFlow(fake_evm_result)
    mon._evm = fake_evm

    results = asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))

    eth_rows = [r for r in results if r["chain"] == "ETH"]
    assert len(eth_rows) == 1, f"期望 1 条 ETH 行，实得 {len(eth_rows)}"

    row = eth_rows[0]
    assert row["exchange"] == "Binance"
    assert abs(row["inflow"] - 5_000_000.0) < 1.0
    assert abs(row["outflow"] - 2_000_000.0) < 1.0
    assert abs(row["net"] - 3_000_000.0) < 1.0
    assert row["n_tx"] == 15
    assert row["n_addr"] == 1

    # 落库验证
    count = store.conn.execute(
        "SELECT COUNT(*) FROM exchange_flows WHERE chain='ETH'"
    ).fetchone()[0]
    assert count == 1

    store.close()


def test_poll_once_eth_alert_threshold():
    """ETH 稳定币 alert 当且仅当 |net_usd| >= threshold_usd。"""
    store = _make_store()
    now_ms = _NOW_MS

    fake_btc = _FakeClient({})

    # 场景1：net=$3M > $2M → alert=True
    result_big = {"inflow": 5_000_000.0, "outflow": 2_000_000.0,
                  "net": 3_000_000.0, "n_log": 5, "blocks": 600}
    mon = ExchangeFlowMonitor(
        store, _REGISTRY_EVM,
        threshold_btc=500.0, client=fake_btc, evm_cfg=_EVM_CFG
    )
    mon._evm = _FakeEVMStableFlow(result_big)
    results = asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))
    eth_row = next(r for r in results if r["chain"] == "ETH")
    assert eth_row["alert"] is True, "net=$3M > $2M 阈值，应 alert=True"

    store.close()

    # 场景2：net=$500K < $2M → alert=False
    store2 = _make_store()
    result_small = {"inflow": 1_000_000.0, "outflow": 500_000.0,
                    "net": 500_000.0, "n_log": 3, "blocks": 600}
    mon2 = ExchangeFlowMonitor(
        store2, _REGISTRY_EVM,
        threshold_btc=500.0, client=fake_btc, evm_cfg=_EVM_CFG
    )
    mon2._evm = _FakeEVMStableFlow(result_small)
    results2 = asyncio.run(mon2.poll_once(now_ms=now_ms, session=object()))
    eth_row2 = next(r for r in results2 if r["chain"] == "ETH")
    assert eth_row2["alert"] is False, "net=$500K < $2M 阈值，应 alert=False"

    store2.close()


def test_poll_once_no_evm_cfg_skips_eth():
    """evm_cfg=None 时，ETH 路径被完全跳过（不产生 ETH 行）。"""
    store = _make_store()
    now_ms = _NOW_MS

    fake_btc = _FakeClient({})
    mon = ExchangeFlowMonitor(
        store, _REGISTRY_EVM,
        threshold_btc=500.0, client=fake_btc, evm_cfg=None
    )
    results = asyncio.run(mon.poll_once(now_ms=now_ms, session=object()))
    eth_rows = [r for r in results if r["chain"] == "ETH"]
    assert eth_rows == [], "evm_cfg=None 时不应产生 ETH 行"

    store.close()


# ---------------------------------------------------------------------------
# 5e. fmt_flow_alert ETH 稳定币单位/语义测试（补充 BTC 已有测试）
# ---------------------------------------------------------------------------

def test_fmt_flow_alert_eth_net_inflow():
    """ETH 稳定币净流入 → 买盘弹药🟢，单位 $M USDT/USDC。"""
    row = {
        "exchange": "Binance",
        "chain":    "ETH",
        "inflow":   5_000_000.0,
        "outflow":  2_000_000.0,
        "net":      3_000_000.0,
        "n_addr":   4,
        "n_tx":     15,
        "alert":    True,
    }
    text = fmt_flow_alert(row)
    assert "Binance" in text
    assert "ETH" in text
    assert "🟢" in text       # 稳定币净流入=买盘弹药🟢
    assert "3.0M" in text     # $3M，保留 1 位小数
    assert "USDT/USDC" in text
    # BTC 语义不应出现
    assert "🔴" not in text


def test_fmt_flow_alert_eth_net_outflow():
    """ETH 稳定币净流出 → 资金撤离🔴。"""
    row = {
        "exchange": "OKX",
        "chain":    "ETH",
        "inflow":   1_000_000.0,
        "outflow":  4_000_000.0,
        "net":      -3_000_000.0,
        "n_addr":   1,
        "n_tx":     8,
        "alert":    True,
    }
    text = fmt_flow_alert(row)
    assert "OKX" in text
    assert "🔴" in text       # 稳定币净流出=撤离🔴
    assert "3.0M" in text
    assert "USDT/USDC" in text
    assert "🟢" not in text


def test_fmt_flow_alert_btc_semantics_unchanged():
    """BTC 行：原有语义不变（净流入🔴=抛压，净流出🟢=吸筹），不受稳定币扩展影响。"""
    row_in = {
        "exchange": "Binance", "chain": "BTC",
        "inflow": 1000.0, "outflow": 200.0, "net": 800.0,
        "n_addr": 3, "n_tx": 12, "alert": True,
    }
    text_in = fmt_flow_alert(row_in)
    assert "🔴" in text_in     # BTC 净流入 = 抛压🔴
    assert "BTC" in text_in
    assert "800" in text_in

    row_out = {
        "exchange": "OKX", "chain": "BTC",
        "inflow": 50.0, "outflow": 600.0, "net": -550.0,
        "n_addr": 1, "n_tx": 5, "alert": True,
    }
    text_out = fmt_flow_alert(row_out)
    assert "🟢" in text_out    # BTC 净流出 = 吸筹🟢
    assert "550" in text_out


# ---------------------------------------------------------------------------
# 主入口（直接运行时）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_btc_flow_24h_basic()
    test_btc_flow_24h_excludes_old_tx()
    test_btc_flow_24h_excludes_unconfirmed()
    test_btc_flow_24h_wrong_addr_ignored()
    test_btc_flow_24h_empty()
    test_btc_flow_24h_missing_fields()
    test_poll_once_inserts_rows()
    test_poll_once_net_aggregation()
    test_poll_once_alert_threshold()
    test_poll_once_below_threshold_no_alert()
    test_recent_query()
    test_fmt_flow_alert_net_inflow()
    test_fmt_flow_alert_net_outflow()
    test_registry_structure()
    test_poll_once_uses_address_txs_window()
    test_poll_once_paginated_two_page_aggregation()
    test_address_txs_window_stops_at_boundary()
    test_address_txs_window_graceful_on_error()
    test_address_txs_window_empty_first_page()
    # EVM 稳定币新增测试
    test_sum_stable_logs_usdt()
    test_sum_stable_logs_multi_contract()
    test_sum_stable_logs_empty()
    test_sum_stable_logs_unknown_contract_uses_default()
    test_sum_stable_logs_bad_data()
    test_pad_addr_format()
    test_pad_addr_already_lower()
    test_exchange_stable_flow_basic()
    test_exchange_stable_flow_block_number_failure()
    test_poll_once_eth_row_produced()
    test_poll_once_eth_alert_threshold()
    test_poll_once_no_evm_cfg_skips_eth()
    test_fmt_flow_alert_eth_net_inflow()
    test_fmt_flow_alert_eth_net_outflow()
    test_fmt_flow_alert_btc_semantics_unchanged()
    print("✅ 全部通过")
