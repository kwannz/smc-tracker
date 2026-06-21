"""回测/重放：用历史 K 线校验 SMC 结构信号的胜率与盈亏比。"""
from .engine import Backtester, BacktestResult, Trade

__all__ = ["Backtester", "BacktestResult", "Trade"]
