"""asset_class —— Bitget 永续合约 TradFi/加密 分类。

Bitget API 不提供资产类别字段（symbolType 全为 perpetual），故用 curated 名单
区分代币化传统金融标的（股票/ETF/商品）与原生加密货币。

注意：本名单为人工维护的快照，Bitget 增新代币化标的后需手动补充；
未在名单中的 coin 默认归入「加密」，不影响已知标的的正确分类。

导出：TRADFI_TICKERS, asset_class, asset_badge
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Curated TradFi 名单（大写 baseCoin）
# ---------------------------------------------------------------------------
# 金属商品
_METALS: frozenset[str] = frozenset({"XAU", "XAG"})

# 已知 Bitget 代币化股票
_STOCKS: frozenset[str] = frozenset({
    "AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "GOOG",
    "AMZN", "META", "MSTR", "COIN", "HOOD", "AMD", "NFLX",
    # 代币化 ETF/杠杆产品（非加密）
    "SOXL",
    # 半导体公司
    "MU", "SNDK",
    # pre-IPO 代币化标的（Bitget 特有）
    "SPCX", "CBRS", "DRAM",
})

# ETF 标的
_ETFS: frozenset[str] = frozenset({"SPY", "QQQ", "GLD", "SLV", "TQQQ"})

#: 完整 TradFi curated 名单（大写）——curated 快照，Bitget 增新标的需更新；未在名单默认加密。
TRADFI_TICKERS: frozenset[str] = _METALS | _STOCKS | _ETFS


def asset_class(coin: str) -> str:
    """返回 coin 所属资产类别。

    coin 规范化为大写后查 TRADFI_TICKERS：
      - 命中 → "tradfi"（代币化传统金融资产：股票/ETF/商品）
      - 未命中 → "crypto"（原生加密货币，默认）

    大小写不敏感；空串/未知 coin → "crypto"。

    参数：
        coin: 合约 baseCoin（如 "BTC"、"XAU"、"SOXL"）

    返回：
        "tradfi" 或 "crypto"
    """
    return "tradfi" if coin.upper() in TRADFI_TICKERS else "crypto"


def asset_badge(coin: str) -> str:
    """返回 coin 的显示徽章字符串。

    TradFi → "🏦TradFi"（橙色语义，传统金融标的）
    加密   → "₿加密"（蓝色语义，原生加密货币）

    大小写不敏感（复用 asset_class）。
    """
    if asset_class(coin) == "tradfi":
        return "🏦TradFi"
    return "₿加密"
