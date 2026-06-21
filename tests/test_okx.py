"""OKX V5 REST 客户端单测：纯解析函数 + fake session 注入(确定性，不联网)。

测试数据取自真实 curl 样本(www.okx.com，2026-06-22 BTC-USDT-SWAP)，验证：
  - ticker 24h 涨幅自算(OKX ticker 无该字段)
  - OI 取 oiCcy(币数)/oiUsd(美元)
  - funding-rate 解析
  - candles 倒序→正序 reverse + vol 用 volCcy(币数)
  - OKXClient._get 包装解析(code!=0 抛错) via fake session
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncio

import orjson


# ---- 纯解析函数（无网络，确定性）----

def test_okx_parse_ticker_self_computes_chg24():
    """OKX ticker 无 24h 涨幅字段 → parse_ticker 用 (last-open24h)/open24h 自算。"""
    from smc_tracker.okx.client import parse_ticker
    d = {"instId": "BTC-USDT-SWAP", "last": "64145", "open24h": "63931.5",
         "volCcy24h": "42376.1657", "ts": "1782061482265"}
    out = parse_ticker(d)
    assert out["inst_id"] == "BTC-USDT-SWAP"
    assert out["price"] == 64145.0
    assert abs(out["chg24"] - (64145.0 - 63931.5) / 63931.5) < 1e-12
    assert out["ts"] == 1782061482265


def test_okx_parse_ticker_zero_open_no_div0():
    """open24h<=0 → chg24=0(不除零崩溃)。"""
    from smc_tracker.okx.client import parse_ticker
    out = parse_ticker({"instId": "X-USDT-SWAP", "last": "1", "open24h": "0", "ts": "0"})
    assert out["chg24"] == 0.0


def test_okx_parse_oi_uses_ccy_and_usd():
    """OI 取 oiCcy(币数) + oiUsd(美元)，不用 oi(合约张数)。"""
    from smc_tracker.okx.client import parse_oi
    d = {"instId": "BTC-USDT-SWAP", "oi": "3033189.2", "oiCcy": "30331.892",
         "oiUsd": "1945451154.6", "ts": "1782061483511"}
    out = parse_oi(d)
    assert out["oi_ccy"] == 30331.892
    assert out["oi_usd"] == 1945451154.6
    assert out["ts"] == 1782061483511


def test_okx_parse_funding():
    from smc_tracker.okx.client import parse_funding
    d = {"instId": "BTC-USDT-SWAP", "fundingRate": "-0.0000141418487422",
         "nextFundingTime": "1782115200000", "premium": "-0.0005407216374360"}
    out = parse_funding(d)
    assert abs(out["funding_rate"] - (-0.0000141418487422)) < 1e-18
    assert out["next_funding_time"] == 1782115200000
    assert abs(out["premium"] - (-0.0005407216374360)) < 1e-15


def test_okx_parse_candles_reverse_and_volccy():
    """OKX candles 倒序(最新在前) → reverse 成正序；vol 取 volCcy(index 6，币数)。"""
    from smc_tracker.okx.client import parse_candles
    rows = [
        ["1782061200000", "64111.2", "64145", "64075.9", "64145", "13787.63", "137.8763", "8838494", "0"],
        ["1782060900000", "64105.2", "64130.9", "64104.5", "64111.2", "5139.79", "51.3979", "3295360", "1"],
    ]
    out = parse_candles(rows)
    # 正序：更早的 ts 在前
    assert out[0][0] == 1782060900000
    assert out[1][0] == 1782061200000
    # OHLC + vol(volCcy 币数)
    assert out[0][1] == 64105.2 and out[0][4] == 64111.2
    assert out[0][5] == 51.3979      # volCcy(index 6)
    assert out[1][5] == 137.8763


# ---- OKXClient（fake session 注入，不联网）----

class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *a: object) -> None:
        pass

    def raise_for_status(self) -> None:
        pass

    async def read(self) -> bytes:
        return orjson.dumps(self._payload)


class _FakeSession:
    """最小 fake aiohttp session：get() 恒返回构造时给定的 payload。"""
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> _FakeResp:
        return _FakeResp(self._payload)

    async def close(self) -> None:
        pass


def test_okx_client_ticker_via_fake_session():
    """OKXClient.ticker 经 fake session 返回解析后的归一 dict。"""
    from smc_tracker.okx.client import OKXClient
    payload = {"code": "0", "msg": "", "data": [
        {"instId": "BTC-USDT-SWAP", "last": "64145", "open24h": "63931.5",
         "volCcy24h": "42376", "ts": "1782061482265"}]}

    async def run() -> dict:
        c = OKXClient()
        c._session = _FakeSession(payload)  # type: ignore[assignment]
        return await c.ticker("BTC-USDT-SWAP")

    out = asyncio.run(run())
    assert out["price"] == 64145.0
    assert out["inst_id"] == "BTC-USDT-SWAP"


def test_okx_client_raises_on_error_code():
    """code!=0 → _get 抛 RuntimeError(不静默吞错)。"""
    from smc_tracker.okx.client import OKXClient
    payload = {"code": "50014", "msg": "Parameter instId can not be empty", "data": []}

    async def run() -> None:
        c = OKXClient()
        c._session = _FakeSession(payload)  # type: ignore[assignment]
        await c.ticker("X")

    try:
        asyncio.run(run())
        assert False, "应抛 RuntimeError"
    except RuntimeError as e:
        assert "50014" in str(e)


def test_okx_client_all_open_interest_maps_by_instid():
    """all_open_interest 全市场 → {inst_id: parsed_oi}。"""
    from smc_tracker.okx.client import OKXClient
    payload = {"code": "0", "msg": "", "data": [
        {"instId": "BTC-USDT-SWAP", "oiCcy": "30331.8", "oiUsd": "1945451154", "ts": "1"},
        {"instId": "ETH-USDT-SWAP", "oiCcy": "100000", "oiUsd": "300000000", "ts": "1"}]}

    async def run() -> dict:
        c = OKXClient()
        c._session = _FakeSession(payload)  # type: ignore[assignment]
        return await c.all_open_interest()

    out = asyncio.run(run())
    assert set(out) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    assert out["BTC-USDT-SWAP"]["oi_usd"] == 1945451154.0


# ---- OKXWSClient（消息分发逻辑，不连真实 WS）----

def test_okx_sub_to_arg():
    """OKXSub → {channel, instId}（OKX 订阅 arg 无 instType，区别于 Bitget）。"""
    from smc_tracker.okx.ws_client import OKXSub
    assert OKXSub("trades", "BTC-USDT-SWAP").to_arg() == {
        "channel": "trades", "instId": "BTC-USDT-SWAP"}


def test_okx_ws_dispatch_routes_to_channel_handler():
    """data 推送按 arg.channel 路由到对应 handler。"""
    from smc_tracker.okx.ws_client import OKXSub, OKXWSClient
    received: list = []
    c = OKXWSClient()
    c.subscribe(OKXSub("trades", "BTC-USDT-SWAP"),
                lambda arg, data, ns: received.append((arg, data)))
    raw = orjson.dumps({"arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
                        "data": [{"px": "64000", "sz": "1", "side": "buy"}]})
    asyncio.run(c._handle_raw(raw, 123))
    assert len(received) == 1
    assert received[0][1][0]["side"] == "buy"


def test_okx_ws_pong_feeds_watchdog():
    """文本 pong → 喂看门狗(_last_pong_ns)，不当数据处理。"""
    from smc_tracker.okx.ws_client import OKXWSClient
    c = OKXWSClient()
    asyncio.run(c._handle_raw("pong", 999))
    assert c._last_pong_ns == 999


def test_okx_ws_event_ack_not_dispatched():
    """{event:subscribe} 订阅确认 → 不触发 handler。"""
    from smc_tracker.okx.ws_client import OKXSub, OKXWSClient
    received: list = []
    c = OKXWSClient()
    c.subscribe(OKXSub("trades", "X"), lambda a, d, n: received.append(1))
    asyncio.run(c._handle_raw(orjson.dumps({"event": "subscribe", "arg": {"channel": "trades"}}), 1))
    assert received == []


def test_okx_ws_async_handler_awaited():
    """async handler 返回 Awaitable → _handle_raw 会 await 它。"""
    from smc_tracker.okx.ws_client import OKXSub, OKXWSClient
    seen: list = []

    async def ah(arg: dict, data: list, ns: int) -> None:
        seen.append(data)

    c = OKXWSClient()
    c.subscribe(OKXSub("open-interest", "BTC-USDT-SWAP"), ah)
    raw = orjson.dumps({"arg": {"channel": "open-interest", "instId": "BTC-USDT-SWAP"},
                        "data": [{"oiUsd": "1e9"}]})
    asyncio.run(c._handle_raw(raw, 5))
    assert len(seen) == 1


# ---- OKXClient.swap_meta（ctVal 合约面值映射）----

def test_okx_swap_meta_ctval_usdt_only():
    """swap_meta → {inst_id: {ct_val, ct_val_ccy}}，仅保留 USDT 本位(过滤币本位)。"""
    from smc_tracker.okx.client import OKXClient
    payload = {"code": "0", "data": [
        {"instId": "BTC-USDT-SWAP", "ctVal": "0.01", "ctValCcy": "BTC"},
        {"instId": "DOGE-USDT-SWAP", "ctVal": "1000", "ctValCcy": "DOGE"},
        {"instId": "BTC-USD-SWAP", "ctVal": "100", "ctValCcy": "USD"}]}

    async def run() -> dict:
        c = OKXClient()
        c._session = _FakeSession(payload)  # type: ignore[assignment]
        return await c.swap_meta()

    out = asyncio.run(run())
    assert out["BTC-USDT-SWAP"]["ct_val"] == 0.01
    assert out["DOGE-USDT-SWAP"]["ct_val"] == 1000.0
    assert "BTC-USD-SWAP" not in out   # 币本位被过滤


# ---- OKXPerpMonitor（注入 fake ws/store，不连网）----

class _FakeWS:
    def __init__(self) -> None:
        self.subs: list = []

    def subscribe(self, sub: object, handler: object) -> None:
        self.subs.append((sub.channel, sub.inst_id, handler))  # type: ignore[attr-defined]


class _FakeStore:
    def __init__(self) -> None:
        self.rows: list = []

    def insert_okx_perp(self, rows: object) -> None:
        self.rows.extend(rows)  # type: ignore[arg-type]


def _make_perp_monitor(store: object = None, on_surge: object = None):
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor
    ws = _FakeWS()
    m = OKXPerpMonitor(
        inst_ids=["BTC-USDT-SWAP"], inst_to_coin={"BTC-USDT-SWAP": "BTC"},
        ct_val={"BTC-USDT-SWAP": 0.01}, ws=ws, store=store,
        surge_pct=0.05, on_surge=on_surge)
    return m, ws


def test_okx_perp_net_flow_signed_by_ctval():
    """trades 净流向：名义 = sz张 × ctVal × px，buy 正/sell 负。"""
    m, _ = _make_perp_monitor()
    arg = {"channel": "trades", "instId": "BTC-USDT-SWAP"}
    m._on_trades(arg, [{"side": "buy", "sz": "100", "px": "64000"}], 1)   # 100×0.01×64000=$64000
    m._on_trades(arg, [{"side": "sell", "sz": "50", "px": "64000"}], 2)   # 50×0.01×64000=$32000
    assert abs(m.net_flow("BTC") - 32000.0) < 1e-6


def test_okx_perp_oi_surge_triggers_callback():
    """OI(oiCcy) 相对变化越 surge_pct → on_surge 触发，evt 带 coin/change。"""
    seen: list = []
    m, _ = _make_perp_monitor(on_surge=lambda e: seen.append(e))
    arg = {"channel": "open-interest", "instId": "BTC-USDT-SWAP"}
    m._on_oi(arg, [{"oiCcy": "1000", "oiUsd": "64000000", "ts": "1"}], 1)   # 基线
    m._on_oi(arg, [{"oiCcy": "1100", "oiUsd": "70400000", "ts": "2"}], 2)   # +10% > 5%
    assert len(seen) == 1
    assert seen[0]["coin"] == "BTC"
    assert abs(seen[0]["change"] - 0.1) < 1e-6


def test_okx_perp_flush_inserts_rows():
    """flush → store.insert_okx_perp(rows)；row 首列 inst_id/coin。"""
    store = _FakeStore()
    m, _ = _make_perp_monitor(store=store)
    arg = {"channel": "open-interest", "instId": "BTC-USDT-SWAP"}
    m._on_oi(arg, [{"oiCcy": "1000", "oiUsd": "64000000", "ts": "5"}], 1)
    n = m.flush()
    assert n == 1 and len(store.rows) == 1
    assert store.rows[0][0] == "BTC-USDT-SWAP" and store.rows[0][1] == "BTC"


def test_okx_perp_attach_subscribes_channels():
    """attach 为每个 inst 订阅 trades + open-interest + tickers。"""
    m, ws = _make_perp_monitor()
    m.attach()
    channels = {c for c, _, _ in ws.subs}
    assert {"trades", "open-interest", "tickers"} <= channels


def test_okx_perp_db_roundtrip():
    """okx_perp 表真实 roundtrip：insert_okx_perp → 查回。"""
    import tempfile
    from smc_tracker.storage import Store
    s = Store(Path(tempfile.mkdtemp()) / "okx.db")
    s.insert_okx_perp([("BTC-USDT-SWAP", "BTC", 30000.0, 1.9e9, 64000.0, -0.0001, 50000.0, 100)])
    row = s.conn.execute(
        "SELECT inst_id,coin,oi_ccy,oi_usd,net_flow,ts FROM okx_perp").fetchone()
    assert row[0] == "BTC-USDT-SWAP" and row[1] == "BTC" and row[5] == 100
    s.close()


# ---- CLI okx 子命令解析（不联网）----

def test_cli_okx_subcommand_parser():
    """parse_args(['okx','--top','5','--secs','8']) → top==5, secs==8.0, handler 存在。"""
    from smc_tracker.cli import build_parser
    ap = build_parser()
    args = ap.parse_args(["okx", "--top", "5", "--secs", "8"])
    assert args.top == 5
    assert args.secs == 8.0
    assert callable(getattr(args, "handler", None)), "okx 子命令应设置 handler"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
