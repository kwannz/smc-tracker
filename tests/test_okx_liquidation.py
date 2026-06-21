"""OKX 强平级联监控单测（不联网）。

覆盖：
  1. OKXSub.to_arg() 的 instType firehose 分支 + instId 向后兼容；
  2. _on_liquidation per-coin 聚合（名义 = sz张 × ctVal × bkPx，posSide 分流）；
  3. 非监控 instId 过滤；
  4. okx_liquidations 表 insert → recent 查回 roundtrip。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# ---- 注入 fake ws/store（与 test_okx.py 同构，不连网）----

class _FakeWS:
    def __init__(self) -> None:
        self.subs: list = []

    def subscribe(self, sub: object, handler: object) -> None:
        self.subs.append((sub.channel, sub.inst_id, handler))  # type: ignore[attr-defined]


class _FakeStore:
    def __init__(self) -> None:
        self.liq_rows: list = []

    def insert_okx_perp(self, rows: object) -> None:
        pass

    def insert_okx_liquidations(self, rows: object) -> None:
        self.liq_rows.extend(rows)  # type: ignore[arg-type]


def _make_monitor(store: object = None):
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor
    ws = _FakeWS()
    m = OKXPerpMonitor(
        inst_ids=["BTC-USDT-SWAP"], inst_to_coin={"BTC-USDT-SWAP": "BTC"},
        ct_val={"BTC-USDT-SWAP": 0.01}, ws=ws, store=store)
    return m, ws


# ---- OKXSub.to_arg() ----

def test_okxsub_insttype_firehose_arg():
    """inst_id 空 + inst_type 非空 → {channel, instType}（firehose 全市场订阅）。"""
    from smc_tracker.okx.ws_client import OKXSub
    arg = OKXSub("liquidation-orders", inst_id="", inst_type="SWAP").to_arg()
    assert arg == {"channel": "liquidation-orders", "instType": "SWAP"}


def test_okxsub_instid_backward_compatible():
    """inst_id 非空 → {channel, instId}（现状不变，向后兼容）。"""
    from smc_tracker.okx.ws_client import OKXSub
    arg = OKXSub("trades", "BTC-USDT-SWAP").to_arg()
    assert arg == {"channel": "trades", "instId": "BTC-USDT-SWAP"}


# ---- _on_liquidation 聚合 ----

def test_on_liquidation_long_aggregation():
    """多头被平累计：notional = sz × ctVal × bkPx = 100×0.01×60000 = 60000。"""
    m, _ = _make_monitor()
    data = [{"instId": "BTC-USDT-SWAP", "details": [
        {"posSide": "long", "side": "sell", "sz": "100", "bkPx": "60000", "ts": "1"}]}]
    m._on_liquidation({"channel": "liquidation-orders"}, data, 1)
    assert abs(m.all_liquidations()["BTC"]["long_liq_usd"] - 60000.0) < 1e-6
    assert abs(m.all_liquidations()["BTC"]["short_liq_usd"] - 0.0) < 1e-6


def test_on_liquidation_short_aggregation():
    """空头被平累计到 short_liq_usd。"""
    m, _ = _make_monitor()
    data = [{"instId": "BTC-USDT-SWAP", "details": [
        {"posSide": "short", "side": "buy", "sz": "50", "bkPx": "60000", "ts": "2"}]}]
    m._on_liquidation({"channel": "liquidation-orders"}, data, 1)
    assert abs(m.all_liquidations()["BTC"]["short_liq_usd"] - 30000.0) < 1e-6
    assert abs(m.all_liquidations()["BTC"]["long_liq_usd"] - 0.0) < 1e-6


def test_on_liquidation_filters_unmonitored_inst():
    """非监控 instId（XRP-USDT-SWAP）不计入聚合。"""
    m, _ = _make_monitor()
    data = [{"instId": "XRP-USDT-SWAP", "details": [
        {"posSide": "long", "side": "sell", "sz": "100", "bkPx": "60000", "ts": "1"}]}]
    m._on_liquidation({"channel": "liquidation-orders"}, data, 1)
    assert "XRP" not in m.all_liquidations()
    assert m.all_liquidations() == {}


def test_on_liquidation_buffers_and_flushes():
    """强平行进缓冲，flush → store.insert_okx_liquidations(rows)。"""
    store = _FakeStore()
    m, _ = _make_monitor(store=store)
    data = [{"instId": "BTC-USDT-SWAP", "details": [
        {"posSide": "long", "side": "sell", "sz": "100", "bkPx": "60000", "ts": "1"}]}]
    m._on_liquidation({"channel": "liquidation-orders"}, data, 1)
    m.flush()
    assert len(store.liq_rows) == 1
    coin, pos_side, side, notional, bk_px, ts = store.liq_rows[0]
    assert coin == "BTC" and pos_side == "long" and side == "sell"
    assert abs(notional - 60000.0) < 1e-6 and abs(bk_px - 60000.0) < 1e-6 and ts == 1


def test_attach_subscribes_liquidation_firehose():
    """attach 订阅 liquidation-orders（firehose，inst_id 为空）一次。"""
    m, ws = _make_monitor()
    m.attach()
    liq = [(c, i) for c, i, _ in ws.subs if c == "liquidation-orders"]
    assert liq == [("liquidation-orders", "")]


# ---- 强平级联告警（on_liquidation_signal）----

def test_liquidation_cascade_signal_triggers_once_per_level():
    """累计跨 liq_signal_usd 整数倍触发一次；同向不跨新倍数不重复触发。

    ct_val=1.0、liq_signal_usd=10万：sz=2,bkPx=60000 → long 累计 12万（跨 1 倍）触发；
    再喂小量（sz=1,bkPx=10000 → +1万=13万，仍在 1 倍内）不重复触发。
    """
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor
    fired: list = []
    ws = _FakeWS()
    m = OKXPerpMonitor(
        inst_ids=["BTC-USDT-SWAP"], inst_to_coin={"BTC-USDT-SWAP": "BTC"},
        ct_val={"BTC-USDT-SWAP": 1.0}, ws=ws, store=None,
        liq_signal_usd=100000, on_liquidation_signal=fired.append)
    data = [{"instId": "BTC-USDT-SWAP", "details": [
        {"posSide": "long", "side": "sell", "sz": "2", "bkPx": "60000", "ts": "1"}]}]
    m._on_liquidation({"channel": "liquidation-orders"}, data, 1)
    assert len(fired) == 1
    assert fired[0]["coin"] == "BTC"
    assert fired[0]["liquidated_side"] == "long"
    assert abs(fired[0]["notional"] - 120000.0) < 1e-6
    # 再喂小量，累计 13万 仍在第 1 倍内 → 不重复触发
    data2 = [{"instId": "BTC-USDT-SWAP", "details": [
        {"posSide": "long", "side": "sell", "sz": "1", "bkPx": "10000", "ts": "2"}]}]
    m._on_liquidation({"channel": "liquidation-orders"}, data2, 1)
    assert len(fired) == 1


def test_liquidation_cascade_no_signal_when_callback_none():
    """on_liquidation_signal=None 时即使跨阈值也不触发（不报错）。"""
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor
    ws = _FakeWS()
    m = OKXPerpMonitor(
        inst_ids=["BTC-USDT-SWAP"], inst_to_coin={"BTC-USDT-SWAP": "BTC"},
        ct_val={"BTC-USDT-SWAP": 1.0}, ws=ws, store=None,
        liq_signal_usd=100000, on_liquidation_signal=None)
    data = [{"instId": "BTC-USDT-SWAP", "details": [
        {"posSide": "long", "side": "sell", "sz": "2", "bkPx": "60000", "ts": "1"}]}]
    m._on_liquidation({"channel": "liquidation-orders"}, data, 1)
    assert abs(m.all_liquidations()["BTC"]["long_liq_usd"] - 120000.0) < 1e-6


def test_fmt_liquidation_signals():
    """单条 long 1200000 含 BTC 与 多头；空列表 → "无"。"""
    from smc_tracker.okx.stream import fmt_liquidation_signals
    s = fmt_liquidation_signals(
        [{"coin": "BTC", "liquidated_side": "long", "notional": 1200000.0}])
    assert "BTC" in s and "多头" in s and "1,200,000" in s
    assert fmt_liquidation_signals([]) == "无"


# ---- okx_liquidations 表 roundtrip ----

def test_okx_liquidations_db_roundtrip():
    """insert_okx_liquidations → recent_okx_liquidations 查回。"""
    from smc_tracker.storage import Store
    s = Store(Path(tempfile.mkdtemp()) / "liq.db")
    s.insert_okx_liquidations([("BTC", "long", "sell", 60000.0, 60000.0, 1000)])
    rows = s.recent_okx_liquidations(500)
    assert len(rows) == 1
    ts, coin, pos_side, side, notional_usd, bk_px = rows[0]
    assert ts == 1000 and coin == "BTC" and pos_side == "long" and side == "sell"
    assert abs(notional_usd - 60000.0) < 1e-6 and abs(bk_px - 60000.0) < 1e-6
    # since 过滤：early 边界外的不返回
    assert s.recent_okx_liquidations(2000) == []
    s.close()
