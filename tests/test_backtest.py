"""回测引擎单测（合成 K 线，确定性结果，无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Candle
from smc_tracker.backtest import Backtester, BacktestResult, Trade

# 产生 BOS bull 的基础构造（o,h,l,c；o=c）。idx10 收 21 突破 swing high 20，ref_low=idx8 低点 11
_BOS_BULL = [(11, 12, 10, 11), (12, 13, 11, 12), (9, 11, 8, 9), (13, 14, 10, 13),
             (15, 16, 12, 15), (19, 20, 16, 19), (15, 18, 14, 15), (14, 17, 13, 14),
             (12, 16, 11, 12), (18, 19, 15, 18), (21, 22, 18, 21), (22, 23, 19, 22)]


def _candles(bars):
    return [Candle(coin="X", interval="1m", open_time_ms=i * 60000,
                   close_time_ms=i * 60000 + 59999, o=o, h=h, l=l, c=c, v=1, n=1)
            for i, (o, h, l, c) in enumerate(bars)]


def test_winning_trade():
    # 突破后价格冲到目标(≈41) → 赢
    cs = _candles(_BOS_BULL + [(40, 42, 39, 41)])
    res = Backtester("X").run(cs, lookback=2, max_stop_pct=1.0, target_rr=2.0)
    assert res.wins == 1 and res.losses == 0
    assert res.win_rate == 1.0 and abs(res.avg_r - 2.0) < 1e-9


def test_losing_trade():
    # 突破后价格跌破止损(≈10.99) → 输
    cs = _candles(_BOS_BULL + [(15, 16, 10, 10.5)])
    res = Backtester("X").run(cs, lookback=2, max_stop_pct=1.0, target_rr=2.0)
    assert res.losses == 1 and res.wins == 0
    assert abs(res.avg_r + 1.0) < 1e-9


def test_open_trade_excluded():
    # 无后续触及 → 未平仓，不计入胜负
    cs = _candles(_BOS_BULL)
    res = Backtester("X").run(cs, lookback=2, max_stop_pct=1.0)
    assert res.wins == 0 and res.losses == 0
    assert len(res.trades) == 1 and res.trades[0].outcome == "open"


def test_rejected_when_stop_too_far():
    # 默认 max_stop_pct=8%，止损 47% → 信号被风险过滤，无交易
    cs = _candles(_BOS_BULL + [(40, 42, 39, 41)])
    res = Backtester("X").run(cs, lookback=2)   # 默认 max_stop_pct=0.08
    assert len(res.trades) == 0


def test_metrics_aggregation():
    res = BacktestResult("X")
    res.trades = [
        Trade("X", "long", 100, 95, 110, 1, outcome="win", r=2.0),
        Trade("X", "long", 100, 95, 110, 5, outcome="win", r=2.0),
        Trade("X", "long", 100, 95, 110, 9, outcome="loss", r=-1.0),
    ]
    assert abs(res.win_rate - 2 / 3) < 1e-9
    assert abs(res.avg_r - 1.0) < 1e-9
    assert abs(res.profit_factor - 4.0) < 1e-9


def test_zone_filter_reduces_or_equal_trades():
    cs = _candles(_BOS_BULL + [(40, 42, 39, 41)])
    base = Backtester("X").run(cs, lookback=2, max_stop_pct=1.0)
    filt = Backtester("X").run(cs, lookback=2, max_stop_pct=1.0, require_zone=True)
    assert len(filt.trades) <= len(base.trades)


def test_retrace_triggers_then_wins():
    # retrace 限价 entry=100/stop=95/target=110；价格回撤触及 100 后冲到 110
    t = Trade("X", "long", 100, 95, 110, entry_idx=0, entry_mode="retrace")
    cs = _candles([(105, 106, 103, 104), (104, 105, 99, 101),
                   (101, 108, 100, 107), (107, 112, 106, 111)])
    Backtester._simulate(cs, [t], 2.0, max_wait_bars=12)
    assert t.outcome == "win" and t.triggered_idx == 1


def test_retrace_expires_without_pullback():
    t = Trade("X", "long", 100, 95, 110, entry_idx=0, entry_mode="retrace")
    cs = _candles([(105, 106, 103, 104)] + [(106, 107, 104, 105)] * 5)  # 从不回撤到 100
    Backtester._simulate(cs, [t], 2.0, max_wait_bars=3)
    assert t.outcome == "expired"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
