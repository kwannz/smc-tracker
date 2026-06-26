"""Meme 币定义与跨交易所符号归一化。

需求：按 **Bitget 永续合约** 币种定义 meme 清单，在 **Hyperliquid 永续** 上监控。
难点：同一币在两所命名不同 —— Bitget 用数量前缀 `1000BONK`/`1000SATS`，
Hyperliquid 用 `k` 前缀 `kBONK`/`kPEPE`（k=1000）。需归一化到规范基础符号再求交集。

最终 meme 清单 = MEME_BASES ∩ Bitget永续基础币 ∩ Hyperliquid永续币（输出 Hyperliquid 币名）。
"""
from __future__ import annotations

# 规范化后的 meme 基础符号集合（大写、已去数量/k 前缀）。
# 过度包含无妨：最终会与 Bitget 永续 + Hyperliquid 永续求交集自动裁剪。
MEME_BASES: frozenset[str] = frozenset({
    # 经典 meme
    "DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "BOME", "MEW", "POPCAT",
    "BRETT", "MOG", "TURBO", "NEIRO", "PNUT", "GOAT", "MOODENG", "PENGU",
    "FARTCOIN", "PONKE", "SLERF", "MYRO", "WEN", "MEME", "BABYDOGE", "DEGEN",
    "TOSHI", "MUMU", "GIGA", "APU", "RETARDIO", "CHILLGUY", "CHILL", "SUNDOG",
    "DOGS", "HMSTR", "CAT", "RATS", "SATS", "USELESS", "TROLL", "FWOG",
    "BILLY", "WOJAK", "ANALOS", "MAGA", "BAN", "MICHI", "SPX",
    # AI-meme（交易所多归入 meme 板块）
    "AI16Z", "GRIFFAIN", "AIXBT", "ZEREBRO", "ACT", "ARC", "PIPPIN",
    # 政治 meme
    "TRUMP", "MELANIA",
    # 平台/发射台 meme
    "PUMP",
})

# Bitget 数量前缀（按长度降序匹配，避免 1000 误吃 1000000）
_QTY_PREFIXES = ("1000000", "100000", "10000", "1000")


def normalize(symbol: str) -> str:
    """归一化交易所币名到规范基础符号（大写、去 1000/k 前缀）。

    例：'kPEPE'->'PEPE', '1000BONK'->'BONK', '1000000MOG'->'MOG', 'WIF'->'WIF'
    """
    # Hyperliquid 的 k 前缀：小写 k + 大写名（kPEPE/kBONK），区别于全大写真名(KAITO)
    if len(symbol) > 1 and symbol[0] == "k" and symbol[1].isupper():
        return symbol[1:].upper()
    s = symbol.upper()
    for p in _QTY_PREFIXES:
        if s.startswith(p) and len(s) > len(p):
            return s[len(p):]
    return s
