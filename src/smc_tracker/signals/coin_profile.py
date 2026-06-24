"""每币信号画像 CoinSignalProfile —— 把"每个币种的独立计算"分类归类。

设计动机（QA 实证驱动）：谐波宇宙（Bitget 成交额 top-N + TradFi 代币）与各前瞻信号
的数据可用性**逐币不同**——纯股票代币（TSLA/AAPL/META…）Bitget fundingRate 恒 0，
而 OI/逐笔 taker/L2 盘口对全部 Bitget 永续（含 TradFi，本质是加密永续）都可采。

本模块给每币算一个画像：资产类（crypto / tradfi_commodity / tradfi_stock）+ 信号可用性
（has_oi / has_funding / has_taker / has_l2）。下游 forward_confirm 据此**门控**——
该币缺数据的信号分量直接跳过（缺=中性，不佯装确认），并据此诚实标注，避免对零值代币
产生虚假"前瞻确认"（CLAUDE.md §二 诚实铁律）。

职责解耦：本模块只做分类，接收**已解析**的 oi/funding 浮点（解析 Bitget schema 是
bitget 层职责），故可纯函数单测、不耦合交易所字段名。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..asset_class import asset_class

# 商品类 TradFi（金属 + 商品 ETF）——其余 TradFi 归股票类。
# 与 asset_class.py 的 curated 名单一致：金属 XAU/XAG + 商品 ETF GLD/SLV。
_COMMODITY: frozenset[str] = frozenset({"XAU", "XAG", "GLD", "SLV"})


@dataclass(slots=True)
class CoinSignalProfile:
    """单币信号画像（值对象，可序列化）。"""

    coin: str            # baseCoin（大写口径，如 "BTC"/"XAU"）
    symbol: str          # 交易所 symbol（如 "BTCUSDT"）
    asset_class: str     # 'crypto' | 'tradfi_commodity' | 'tradfi_stock'
    has_oi: bool         # OI 可采（持仓量 > 0）
    has_funding: bool    # funding 可用（fundingRate != 0；纯股票代币恒 0 → False）
    has_taker: bool      # 逐笔 taker 资金流已订阅该币（trade WS）
    has_l2: bool         # L2 盘口已订阅该币（books WS）


def signal_asset_class(coin: str) -> str:
    """三类资产细分：crypto / tradfi_commodity / tradfi_stock。

    复用 asset_class()（crypto/tradfi 二分），再把 tradfi 细分商品/股票。
    大小写不敏感；未知 coin → crypto。
    """
    if asset_class(coin) == "crypto":
        return "crypto"
    return "tradfi_commodity" if coin.upper() in _COMMODITY else "tradfi_stock"


def build_profile(
    coin: str,
    symbol: str,
    *,
    oi: float,
    funding: float,
    subscribed_taker: bool = False,
    subscribed_l2: bool = False,
) -> CoinSignalProfile:
    """构建单币信号画像。

    参数：
        coin/symbol         — baseCoin 与交易所 symbol。
        oi                  — 已解析的持仓量（holdingAmount）；>0 → has_oi。
        funding             — 已解析的资金费率（fundingRate）；!=0 → has_funding。
        subscribed_taker    — 该币是否已订阅 trade WS（逐笔 taker）。
        subscribed_l2       — 该币是否已订阅 books WS（L2 盘口）。
    """
    return CoinSignalProfile(
        coin=coin,
        symbol=symbol,
        asset_class=signal_asset_class(coin),
        has_oi=oi > 0.0,
        has_funding=funding != 0.0,
        has_taker=bool(subscribed_taker),
        has_l2=bool(subscribed_l2),
    )
