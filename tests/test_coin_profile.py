"""每币信号画像 CoinSignalProfile 单测（确定性合成数据）。

分类归类：每币按资产类（crypto / tradfi_commodity / tradfi_stock）+ 信号可用性
（has_oi / has_funding / has_taker / has_l2）分类，驱动"该币该算哪些前瞻信号 + 诚实标注"。

funding 实证（Bitget 公开 API）：纯股票代币（TSLA/AAPL/META…）fundingRate 恒 0；
商品类（XAU/XAG）与高量代币 funding 非零 → has_funding 取自实测值，非资产类猜测。
"""
from __future__ import annotations

from smc_tracker.signals.coin_profile import CoinSignalProfile, build_profile


def test_crypto_coin_profile():
    """加密主流币（BTC）：crypto，OI/ funding 均有数据。"""
    p = build_profile("BTC", "BTCUSDT", oi=33152.0, funding=0.000039)
    assert isinstance(p, CoinSignalProfile)
    assert p.coin == "BTC"
    assert p.symbol == "BTCUSDT"
    assert p.asset_class == "crypto"
    assert p.has_oi is True
    assert p.has_funding is True


def test_tradfi_commodity_profile():
    """商品代币（XAU 黄金）：tradfi_commodity，funding 非零→has_funding True。"""
    p = build_profile("XAU", "XAUUSDT", oi=11001.0, funding=0.000026)
    assert p.asset_class == "tradfi_commodity"
    assert p.has_oi is True
    assert p.has_funding is True


def test_tradfi_stock_funding_zero():
    """纯股票代币（TSLA）funding 恒 0：tradfi_stock，has_funding False（实测铁律）。"""
    p = build_profile("TSLA", "TSLAUSDT", oi=24335.0, funding=0.0)
    assert p.asset_class == "tradfi_stock"
    assert p.has_oi is True
    assert p.has_funding is False  # funding=0 → funding 信号对该币物理无效


def test_oi_zero_has_oi_false():
    """OI=0 → has_oi False（OI 速度信号对该币不可用）。"""
    p = build_profile("FOO", "FOOUSDT", oi=0.0, funding=0.0)
    assert p.has_oi is False


def test_subscription_flags_drive_taker_l2():
    """has_taker/has_l2 反映订阅状态（trade/books WS 是否订阅该币）。"""
    sub = build_profile("BTC", "BTCUSDT", oi=1.0, funding=0.0,
                        subscribed_taker=True, subscribed_l2=True)
    assert sub.has_taker is True
    assert sub.has_l2 is True
    # 默认未订阅
    nosub = build_profile("BTC", "BTCUSDT", oi=1.0, funding=0.0)
    assert nosub.has_taker is False
    assert nosub.has_l2 is False


def test_negative_funding_is_available():
    """负 funding（空头拥挤）也算有数据：has_funding 取 != 0，非 > 0。"""
    p = build_profile("ETH", "ETHUSDT", oi=5000.0, funding=-0.00012)
    assert p.has_funding is True


def test_unknown_coin_defaults_crypto():
    """未在 TradFi 名单的 coin 默认 crypto（与 asset_class 一致）。"""
    p = build_profile("DOGE", "DOGEUSDT", oi=100.0, funding=0.0001)
    assert p.asset_class == "crypto"
