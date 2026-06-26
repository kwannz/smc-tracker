"""谐波交易机器人回测（#201,no-repaint 增量重放）。

结合 freqtrade 架构(external/freqtrade 蓝本)+ 已验证谐波 edge(#165 +0.5R/笔):
  1. `HarmonicState` 逐根重放(只见过去 K 线=**no-repaint**,无未来泄漏),每根出当前 completed 形态。
  2. 每根新 completed(按 src_key 首现去重)经 `build_setups` 生成 TradeSetup(进场区/止损=X失效位/目标=target_rr 投射)。
  3. 转信号交 `Backtester.run_setups` 模拟成交(止损/目标先触判定)→ freqtrade 式绩效(胜率/期望/盈亏比/最大回撤)。

keyless(读已存 K 线,无实盘下单)。校验谐波 edge 的**历史可交易性**(对照 #165 forward-prediction 的 OOS edge)。
"""
from __future__ import annotations

from typing import Any

from ..indicators.harmonic_state import HarmonicState
from ..signals.trade_setup import build_setups
from .engine import Backtester, BacktestResult


def harmonic_backtest(
    coin: str,
    tf: str,
    candles: list[Any],
    *,
    target_rr: float = 2.0,
    min_conf: float = 0.0,
    max_wait_bars: int = 48,
    order: int = 2,
    tol: float = 0.07,
) -> BacktestResult:
    """对一段历史 K 线回测谐波 completed setup。返回 BacktestResult（含 freqtrade 式绩效）。

    min_conf：仅回测综合置信 ≥ 此阈值的 setup（对齐 #169 谐波推送 min_conf≥0.75 的减噪门控）。
    no-repaint：每个 setup 的 entry_idx = 其形态在重放中**首次完成的那根**,模拟从下一根起。
    """
    state = HarmonicState(order=order, tol=tol)
    seen: set[str] = set()
    signals: list[dict] = []
    for i, c in enumerate(candles):
        res = state.update(c)
        if not res.get("completed"):
            continue
        setups = build_setups(coin, tf, candles[:i + 1], res, target_rr=target_rr)
        for s in setups:
            # 仅 completed(src_key 前缀 'C|')、首现去重、置信达标
            if not s.src_key.startswith("C|") or s.src_key in seen:
                continue
            if s.confidence < min_conf:
                continue
            seen.add(s.src_key)
            entry = (s.entry_lo + s.entry_hi) / 2.0
            signals.append({
                "entry_idx": i, "direction": s.direction, "entry": entry,
                "stop": s.stop, "target": s.target1, "rr": s.rr or target_rr,
            })
    return Backtester(coin).run_setups(
        candles, signals, target_rr=target_rr, max_wait_bars=max_wait_bars)
