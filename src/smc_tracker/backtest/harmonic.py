"""谐波交易机器人回测（#201,no-repaint 增量重放）。

结合 freqtrade 架构(external/freqtrade 蓝本)+ 已验证谐波 edge(#165 +0.5R/笔):
  1. `HarmonicState` 逐根重放(只见过去 K 线=**no-repaint**,无未来泄漏),每根出当前 completed 形态。
  2. 每根新 completed(按 src_key 首现去重)经 `build_setups` 生成 TradeSetup(进场区/止损=X失效位/目标=target_rr 投射)。
  3. 转信号交 `Backtester.run_setups` 模拟成交(止损/目标先触判定)→ freqtrade 式绩效(胜率/期望/盈亏比/最大回撤)。

keyless(读已存 K 线,无实盘下单)。校验谐波 edge 的**历史可交易性**(对照 #165 forward-prediction 的 OOS edge)。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ..indicators.harmonic_state import HarmonicState
from ..indicators.sfg import (lrsd_series, gpi_series, vap_series, pdbb_series,
                              pivot_series, ami_series, atr2_series, msfvg_series,
                              ai_st_series, dmha_series)
from ..signals.trade_setup import build_setups
from .engine import Backtester, BacktestResult

# 充分使用 SFG(用户#):10 因子各 [-1,+1],reversal 系与谐波反转入场对齐;ai_st 趋势系
_SFG_FACTORS = (lrsd_series, gpi_series, vap_series, pdbb_series, pivot_series,
                ami_series, atr2_series, msfvg_series, ai_st_series, dmha_series)
# build_setups 近窗上限(覆盖 SFG/KNN 暖机 ~46 + ATR + 余量);形态用绝对价格,bound 不影响正确性
_BT_WINDOW = 300


def sfg_consensus(candles: list[Any]) -> np.ndarray:
    """10 个 SFG 因子的 nan-safe **共识 bias** 序列(零前视尾对齐,长度 n)。>0 看多/<0 看空。

    充分使用全部 SFG 因子(非仅喂≈随机 KNN):作谐波 setup 入场确认,回测裁决其是否真提升 edge。
    """
    if len(candles) < 6:
        return np.zeros(len(candles))
    stack = np.vstack([np.asarray(f(candles), dtype=float) for f in _SFG_FACTORS])
    return np.nansum(stack, axis=0)         # 暖机 nan → 该因子贡献 0


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
    require_sfg: bool = False,
) -> BacktestResult:
    """对一段历史 K 线回测谐波 completed setup。返回 BacktestResult（含 freqtrade 式绩效）。

    min_conf：仅回测综合置信 ≥ 此阈值的 setup（对齐 #169 谐波推送 min_conf≥0.75 的减噪门控）。
    require_sfg：要求 **SFG 10 因子共识** 与 setup 方向一致才入场（充分使用 SFG;回测可对比是否提升 edge）。
    no-repaint：每个 setup 的 entry_idx = 其形态在重放中**首次完成的那根**,模拟从下一根起;SFG 共识零前视尾对齐。
    """
    state = HarmonicState(order=order, tol=tol)
    seen: set[str] = set()
    signals: list[dict] = []
    bias = sfg_consensus(candles) if require_sfg else None
    for i, c in enumerate(candles):
        res = state.update(c)
        if not res.get("completed"):
            continue
        # 性能:build_setups 只用近窗算 KNN/ATR(形态用绝对价格,非 candle 索引)→ bound 近 _BT_WINDOW 根,
        # 避免每个完成 bar 重算全增长窗的 O(n²)(KNN feature_matrix 是主瓶颈)。entry_idx 仍用全表 i 供模拟。
        win_lo = max(0, i + 1 - _BT_WINDOW)
        setups = build_setups(coin, tf, candles[win_lo:i + 1], res, target_rr=target_rr)
        for s in setups:
            # 仅 completed(src_key 前缀 'C|')、首现去重、置信达标
            if not s.src_key.startswith("C|") or s.src_key in seen:
                continue
            if s.confidence < min_conf:
                continue
            # SFG 共识确认:long 需 bias>0、short 需 bias<0(充分使用 SFG 作入场过滤)
            if bias is not None:
                b = float(bias[i])
                if (s.direction == "long" and b <= 0) or (s.direction == "short" and b >= 0):
                    continue
            seen.add(s.src_key)
            entry = (s.entry_lo + s.entry_hi) / 2.0
            signals.append({
                "entry_idx": i, "direction": s.direction, "entry": entry,
                "stop": s.stop, "target": s.target1, "rr": s.rr or target_rr,
            })
    return Backtester(coin).run_setups(
        candles, signals, target_rr=target_rr, max_wait_bars=max_wait_bars)
