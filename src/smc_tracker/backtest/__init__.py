"""回测/重放：用历史 K 线校验 SMC 结构信号 + 谐波 setup 的胜率/期望/盈亏比/最大回撤。"""
from .engine import Backtester, BacktestResult, Trade
from .harmonic import harmonic_backtest

__all__ = ["Backtester", "BacktestResult", "Trade", "harmonic_backtest"]
