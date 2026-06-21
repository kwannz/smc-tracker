"""链上 EVM Transfer 解码纯函数单测（无网络）。

验证 topics/data 能解出正确 from/to/value，以及 decimals 解析的健壮性。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.onchain.evm import (
    TRANSFER_TOPIC0,
    parse_decimals,
    parse_transfer_log,
)


def _log(from_topic: str, to_topic: str, data: str) -> dict:
    return {
        "address": "0x6982508145454ce325ddbe47a25d4ec3d2311933",
        "topics": [TRANSFER_TOPIC0, from_topic, to_topic],
        "data": data,
        "blockNumber": "0x1312d00",  # 20000000
        "transactionHash": "0xabc123",
    }


def test_parse_transfer_basic():
    # from = 0x1111...1111, to = 0x2222...2222, value = 1e18 wei = 1.0 (18 decimals)
    from_topic = "0x" + "00" * 12 + "11" * 20
    to_topic = "0x" + "00" * 12 + "22" * 20
    data = hex(10 ** 18)  # 1 token @ 18 decimals
    t = parse_transfer_log(_log(from_topic, to_topic, data),
                           chain="ETH", coin="PEPE", decimals=18)
    assert t is not None
    assert t.from_addr == "0x" + "11" * 20
    assert t.to_addr == "0x" + "22" * 20
    assert abs(t.amount - 1.0) < 1e-12
    assert t.block == 20000000
    assert t.chain == "ETH" and t.coin == "PEPE"
    assert t.contract == "0x6982508145454ce325ddbe47a25d4ec3d2311933"
    assert t.tx_hash == "0xabc123"


def test_parse_transfer_decimals_scaling():
    # USDC 风格 6 decimals：1_500_000 raw = 1.5 token
    ft = "0x" + "00" * 12 + "aa" * 20
    tt = "0x" + "00" * 12 + "bb" * 20
    t = parse_transfer_log(_log(ft, tt, hex(1_500_000)),
                           chain="BSC", coin="X", decimals=6)
    assert t is not None and abs(t.amount - 1.5) < 1e-12


def test_parse_transfer_real_pepe_value():
    # 取一个大额：123456.789 PEPE @ 18 decimals
    raw = int(123456.789 * 10 ** 18)
    ft = "0x000000000000000000000000c6cde7c39eb2f0f0095f41570af89efc2c1ea828"
    tt = "0x000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045"
    t = parse_transfer_log(_log(ft, tt, hex(raw)),
                           chain="ETH", coin="PEPE", decimals=18)
    assert t is not None
    assert t.from_addr == "0xc6cde7c39eb2f0f0095f41570af89efc2c1ea828"
    assert t.to_addr == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"  # vitalik.eth
    assert abs(t.amount - 123456.789) < 1e-3


def test_parse_transfer_wrong_topic0_returns_none():
    ft = "0x" + "00" * 12 + "11" * 20
    tt = "0x" + "00" * 12 + "22" * 20
    log = _log(ft, tt, hex(10 ** 18))
    log["topics"][0] = "0x" + "de" * 32  # 非 Transfer 签名
    assert parse_transfer_log(log, chain="ETH", coin="P", decimals=18) is None


def test_parse_transfer_insufficient_topics_returns_none():
    log = {"address": "0xabc", "topics": [TRANSFER_TOPIC0], "data": "0x1"}
    assert parse_transfer_log(log, chain="ETH", coin="P", decimals=18) is None


def test_parse_transfer_zero_value():
    ft = "0x" + "00" * 12 + "11" * 20
    tt = "0x" + "00" * 12 + "22" * 20
    t = parse_transfer_log(_log(ft, tt, "0x0"), chain="ETH", coin="P", decimals=18)
    assert t is not None and t.amount == 0.0


def test_parse_decimals():
    # 标准 18：32 字节里编码 0x12
    assert parse_decimals("0x" + "00" * 31 + "12") == 18
    assert parse_decimals("0x" + "00" * 31 + "06") == 6   # USDC
    assert parse_decimals("0x" + "00" * 32) == 0          # 合法的 0 decimals
    # 异常/空 → 默认 18
    assert parse_decimals(None) == 18
    assert parse_decimals("0x") == 18
    assert parse_decimals("notahex") == 18
    # 越界（>36）→ 视为异常，默认 18
    assert parse_decimals("0x" + "ff" * 32) == 18


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
