"""链上 meme 转账监控（纯公开 EVM RPC，零鉴权，无 web3 依赖）。

Bitget 那套系统监控「meme 对应 blockchain 地址」的「其他方式」：
用已存入 SQLite 的 meme 合约地址，去公开 EVM RPC 直查大额 Transfer，
捕获巨鲸转账地址，落 SQLite。
"""
from .evm import (
    TRANSFER_TOPIC0,
    EVMTransferWatcher,
    Transfer,
    parse_decimals,
    parse_transfer_log,
)
from .monitor import OnchainMemeMonitor
from .solana import SolanaRPC, SolanaSupplyMonitor, SupplyChange, detect_change
from .exchange_flow import (
    BlockstreamClient,
    ExchangeFlowMonitor,
    btc_flow_24h,
    fmt_flow_alert,
)

__all__ = [
    "TRANSFER_TOPIC0",
    "EVMTransferWatcher",
    "Transfer",
    "parse_decimals",
    "parse_transfer_log",
    "OnchainMemeMonitor",
    "SolanaRPC",
    "SolanaSupplyMonitor",
    "SupplyChange",
    "detect_change",
    "BlockstreamClient",
    "ExchangeFlowMonitor",
    "btc_flow_24h",
    "fmt_flow_alert",
]
