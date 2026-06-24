"""asset_class 单测：TradFi/加密 分类函数。

TDD RED 阶段先写，asset_class.py 尚未实现时全部失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# ---- 导入（RED 阶段时模块不存在，test 会失败并报 ImportError / AttributeError）----
from smc_tracker.asset_class import asset_class, asset_badge, TRADFI_TICKERS


# ---------------------------------------------------------------------------
# TRADFI_TICKERS 集合内容
# ---------------------------------------------------------------------------

def test_tradfi_tickers_is_frozenset():
    """TRADFI_TICKERS 应为 frozenset[str]。"""
    assert isinstance(TRADFI_TICKERS, frozenset)


def test_tradfi_tickers_contains_metals():
    """金属商品 XAU/XAG 应在名单中。"""
    assert "XAU" in TRADFI_TICKERS
    assert "XAG" in TRADFI_TICKERS


def test_tradfi_tickers_contains_tokenized_stocks():
    """已知 Bitget 代币化股票/ETF 应在名单中。"""
    for ticker in ("SOXL", "MU", "SNDK", "AAPL", "TSLA", "NVDA", "MSFT",
                   "GOOGL", "GOOG", "AMZN", "META", "MSTR", "COIN", "HOOD",
                   "AMD", "NFLX"):
        assert ticker in TRADFI_TICKERS, f"{ticker} 应在 TRADFI_TICKERS"


def test_tradfi_tickers_contains_etfs():
    """ETF 标的 SPY/QQQ/GLD/SLV/TQQQ 应在名单中。"""
    for ticker in ("SPY", "QQQ", "GLD", "SLV", "TQQQ"):
        assert ticker in TRADFI_TICKERS, f"{ticker} 应在 TRADFI_TICKERS"


def test_tradfi_tickers_contains_pre_ipo():
    """pre-IPO 代币化标的 SPCX/CBRS/DRAM 应在名单中。"""
    for ticker in ("SPCX", "CBRS", "DRAM"):
        assert ticker in TRADFI_TICKERS, f"{ticker} 应在 TRADFI_TICKERS"


def test_tradfi_tickers_not_contains_crypto():
    """主流加密货币不应在 TRADFI_TICKERS。"""
    for coin in ("BTC", "ETH", "SOL", "HYPE", "XRP", "DOGE", "BNB"):
        assert coin not in TRADFI_TICKERS, f"{coin} 不应在 TRADFI_TICKERS"


# ---------------------------------------------------------------------------
# asset_class 函数
# ---------------------------------------------------------------------------

def test_asset_class_tradfi_known():
    """SOXL/XAU/MU 应返回 'tradfi'。"""
    assert asset_class("SOXL") == "tradfi"
    assert asset_class("XAU") == "tradfi"
    assert asset_class("MU") == "tradfi"


def test_asset_class_crypto_known():
    """BTC/ETH/HYPE/SOL/XRP 应返回 'crypto'。"""
    assert asset_class("BTC") == "crypto"
    assert asset_class("ETH") == "crypto"
    assert asset_class("HYPE") == "crypto"
    assert asset_class("SOL") == "crypto"
    assert asset_class("XRP") == "crypto"


def test_asset_class_case_insensitive():
    """大小写不敏感：soxl/xau → 'tradfi'，btc/eth → 'crypto'。"""
    assert asset_class("soxl") == "tradfi"
    assert asset_class("xau") == "tradfi"
    assert asset_class("btc") == "crypto"
    assert asset_class("Eth") == "crypto"
    assert asset_class("SOL") == "crypto"


def test_asset_class_unknown_defaults_to_crypto():
    """未知 coin（不在名单）默认返回 'crypto'。"""
    assert asset_class("FOOBARXYZ") == "crypto"
    assert asset_class("UNKNOWNCOIN") == "crypto"
    assert asset_class("") == "crypto"


def test_asset_class_etf_returns_tradfi():
    """ETF 标的 SPY/QQQ 也返回 'tradfi'。"""
    assert asset_class("SPY") == "tradfi"
    assert asset_class("QQQ") == "tradfi"


# ---------------------------------------------------------------------------
# asset_badge 函数
# ---------------------------------------------------------------------------

def test_asset_badge_tradfi():
    """TradFi 标的徽章应含「TradFi」字样（橙色提示）。"""
    badge = asset_badge("XAU")
    assert "TradFi" in badge, f"XAU 徽章应含 TradFi，实得: {badge!r}"


def test_asset_badge_crypto():
    """加密标的徽章应含「加密」字样（蓝色标记）。"""
    badge = asset_badge("BTC")
    assert "加密" in badge, f"BTC 徽章应含「加密」，实得: {badge!r}"


def test_asset_badge_tradfi_symbol():
    """TradFi 徽章应含「₿」或「🏦」标志字符（区分于加密）。"""
    tradfi_badge = asset_badge("SOXL")
    crypto_badge = asset_badge("ETH")
    # 两者内容不同
    assert tradfi_badge != crypto_badge, "TradFi 与加密徽章不应相同"


def test_asset_badge_case_insensitive():
    """asset_badge 大小写不敏感（复用 asset_class）。"""
    assert asset_badge("xau") == asset_badge("XAU")
    assert asset_badge("btc") == asset_badge("BTC")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
