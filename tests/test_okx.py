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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
