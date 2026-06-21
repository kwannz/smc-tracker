"""Hyperliquid API 常量。参考官方文档 https://hyperliquid.gitbook.io/hyperliquid-docs"""

MAINNET_REST = "https://api.hyperliquid.xyz"
MAINNET_WS = "wss://api.hyperliquid.xyz/ws"
TESTNET_REST = "https://api.hyperliquid-testnet.xyz"
TESTNET_WS = "wss://api.hyperliquid-testnet.xyz/ws"

# 有效 K 线周期（candle_snapshot 用于数据质量校验）
VALID_INTERVALS = (
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "8h", "12h",
    "1d", "3d", "1w", "1M",
)
