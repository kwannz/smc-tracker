"""KNN 预测器：用「技术指标 + 价格行为 + SFG 10因子」特征向量，找历史 K 个最相似状态，
按它们之后的涨跌投票预测当前方向（纯 numpy，低延迟，无 sklearn 依赖）。

第一性原理：相似的市场状态(指标组合)往往有相似的后续走向。
特征 11→21：
  [0-10]  原始特征（RSI, MACD柱, Stoch%K, ADX, CCI, 布林位置, ATR占比, 实体, 上影, 下影, 方向）
  [11-20] SFG 10 因子（lrsd, gpi, vap, pdbb, pivot, ami, atr2, msfvg, ai_st, dmha）
标签 = 未来 horizon 根后的涨/跌（固定 horizon，本轮只变特征不变标签）。

诚实标注（CLAUDE.md §二）：
  - KNN 方向预测项目自承约等于随机基线（已知 ≈50%）；SFG 特征是有依据的升级
    但不预设提升 PnL，需通过 review.py 闭环实测后才能下结论。
  - SFG 因子（pdbb/pivot/msfvg/lrsd）在无相应市场结构时为 NaN（fail-closed 设计）。
    本模块对 SFG 列的 NaN 做 0.0 中性替换（imputation）而非丢弃整行，避免因
    结构性 NaN 导致训练集完全为空。0.0 = "该因子本 bar 无弃权表态"（符合 SFG 语义）。
    原有 11 个技术特征仍按旧逻辑过滤（warmup 行 NaN → 整行跳过）。
  - 若 SFG 长窗因子（ami ~40根, ai_st ~109根）warmup 导致行数 < k，
    fit 优雅返回 False（KNN 降级不崩溃）。
  - 本轮只变特征，不变标签（固定 horizon sign label），单变量隔离验证。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .price_action import pa_features
from .technical import adx, atr, bollinger, cci, macd, ohlcv_arrays, rsi, stochastic
from .sfg import (
    lrsd_series, gpi_series, vap_series, pdbb_series, pivot_series,
    ami_series, atr2_series, msfvg_series, ai_st_series, dmha_series,
)

# 特征名称：11 个原有特征 + 10 个 SFG 因子 = 21 维
FEATURE_NAMES = [
    # ── 原有 11 个（技术指标 + 价格行为）────────────────────────────────────
    "rsi", "macd_hist", "stoch_k", "adx", "cci", "bb_pos",
    "atr_pct", "body", "upper_wick", "lower_wick", "dir",
    # ── SFG 10 因子（反转+趋势，向量化 series，warmup/缺结构=nan→impute 0）────
    "sfg_lrsd", "sfg_gpi", "sfg_vap", "sfg_pdbb", "sfg_pivot",
    "sfg_ami", "sfg_atr2", "sfg_msfvg", "sfg_ai_st", "sfg_dmha",
]

# SFG 列起始索引（列 11-20 为 SFG，列 0-10 为原有技术特征）
_SFG_START = 11
_SFG_END = 21  # exclusive


def feature_matrix(candles: list[Any]) -> np.ndarray:
    """逐根 K 线的特征矩阵 (n, 21)。

    前 11 列（技术面）：warmup 段为 nan。
    后 10 列（SFG）：warmup 段或无市场结构时为 nan（fail-closed），
        fit/predict 时以 0.0 中性值替换（imputation），详见注释。

    实现策略（per sfg_knn_map.json integration_notes option-a）：
      1. 先计算全部 10 个 SFG *_series(candles)，各返回长度 n 的数组，warmup=nan。
      2. 在 per-row 循环内按 [i] 切片追加到特征行 —— 不逐行调标量版（低效且不对齐）。
      3. 保留 z-score（fit 对 21 列各自标准化），确保异构尺度不影响欧氏距离。
    """
    a = ohlcv_arrays(candles)
    h, l, c = a["h"], a["l"], a["c"]
    n = len(c)

    # ── 原有 11 个指标序列（技术面 + OHLCV）──────────────────────────────────
    rsi_s = rsi(c, 14)
    _, _, hist = macd(c)
    k_s, _ = stochastic(h, l, c)
    adx_s = adx(h, l, c, 14)
    cci_s = cci(h, l, c, 20)
    up, _mid, low = bollinger(c, 20)
    atr_s = atr(h, l, c, 14)

    # ── SFG 10 个因子序列（一次全量计算，各返回 shape (n,)，warmup/无结构=nan）──
    sfg_lrsd_s = lrsd_series(candles)    # LRSD 供需反转（分形+量能门控）
    sfg_gpi_s = gpi_series(candles)      # GPI EMA 网格反转
    sfg_vap_s = vap_series(candles)      # VAP 成交量分布反转
    sfg_pdbb_s = pdbb_series(candles)    # PDBB 破板反转（ZigZag+MSS，无结构→nan）
    sfg_pivot_s = pivot_series(candles)  # Pivot 支撑压力反转（需足够摆动→nan）
    sfg_ami_s = ami_series(candles)      # AMI MLMI 动量反转（~40根warmup）
    sfg_atr2_s = atr2_series(candles)    # ATR2 均值回归反转（~46根warmup）
    sfg_msfvg_s = msfvg_series(candles)  # MSFVG FVG 反转（无FVG事件→nan）
    sfg_ai_st_s = ai_st_series(candles)  # AI SuperTrend 趋势（~109根warmup）
    sfg_dmha_s = dmha_series(candles)    # DMHA HA 趋势（~7根warmup）

    # SFG 序列聚合为 (n, 10) 矩阵（供切片）
    sfg_mat = np.column_stack([
        sfg_lrsd_s, sfg_gpi_s, sfg_vap_s, sfg_pdbb_s, sfg_pivot_s,
        sfg_ami_s, sfg_atr2_s, sfg_msfvg_s, sfg_ai_st_s, sfg_dmha_s,
    ])  # shape (n, 10)，nan = warmup/无结构

    # ── 特征矩阵组装（n, 21）──────────────────────────────────────────────────
    feats = np.full((n, len(FEATURE_NAMES)), np.nan)
    for i in range(n):
        # 原有 11 个特征（保留 nan：warmup 行整行跳过）
        denom = up[i] - low[i]
        bb_pos = (c[i] - low[i]) / denom if denom and np.isfinite(denom) else 0.5
        pf = pa_features(candles[i])
        feats[i, :11] = [
            rsi_s[i], hist[i], k_s[i], adx_s[i], cci_s[i], bb_pos,
            atr_s[i] / c[i] if c[i] else 0.0,
            pf["body"], pf["upper_wick"], pf["lower_wick"], pf["dir"],
        ]
        # SFG 10 个因子：nan → 0.0 中性替换（imputation）
        # 语义：nan = "该因子本 bar 无法表态（暖机/无结构/fail-closed）" → 中性贡献 0
        # 注意：这不改变原有 11 列的 nan 行为，warmup 行（原有列含 nan）仍会被 fit 跳过
        sfg_row = sfg_mat[i]  # shape (10,)
        sfg_row_clean = np.where(np.isfinite(sfg_row), sfg_row, 0.0)
        feats[i, 11:] = sfg_row_clean

    return feats


class KNNPredictor:
    def __init__(self, k: int = 15, horizon: int = 5) -> None:
        self.k = k
        self.horizon = horizon
        self._X: np.ndarray | None = None
        self._y: np.ndarray | None = None
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None

    def fit(self, candles: list[Any]) -> bool:
        """训练 KNN 模型。

        行过滤：np.all(isfinite) 检查 21 列。
          - 原有 11 列的 nan（技术指标 warmup）→ 整行跳过（与旧行为一致）。
          - SFG 列已在 feature_matrix 中 impute 为 0.0，不再触发整行过滤。
        若有效行数 < k，优雅返回 False（KNN 降级，不崩溃）。
        """
        feats = feature_matrix(candles)
        c = np.array([cd.c for cd in candles], dtype=float)
        rows, ys = [], []
        for i in range(len(c) - self.horizon):
            if not np.all(np.isfinite(feats[i])):
                continue
            rows.append(feats[i])
            ys.append(1 if c[i + self.horizon] > c[i] else 0)
        if len(rows) < self.k:
            # 优雅降级：数据不足时返回 False，KNN 不崩溃
            return False
        X = np.array(rows)
        # z-score 标准化：21 列各自标准化，消除 SFG 异构尺度（magnified±3 vs body[0,1]）
        self._mean = X.mean(axis=0)
        self._std = X.std(axis=0) + 1e-9
        self._X = (X - self._mean) / self._std
        self._y = np.array(ys)
        return True

    def predict(self, feat: np.ndarray) -> dict[str, Any] | None:
        if self._X is None or self._y is None:
            return None
        feat = np.asarray(feat, dtype=float)
        # predict 时也需要 SFG nan → 0.0（与 feature_matrix 中的 impute 一致）
        feat_clean = np.where(np.isfinite(feat), feat, 0.0)
        # 但如果原有 11 列本身有 nan（非 SFG 列），则不能预测
        if not np.all(np.isfinite(feat_clean[:11])):
            return None
        if self._mean is None or self._std is None:
            return None
        fn = (feat_clean - self._mean) / self._std
        dist = np.sqrt(((self._X - fn) ** 2).sum(axis=1))
        idx = np.argsort(dist)[:self.k]
        w = 1.0 / (dist[idx] + 1e-9)
        p_up = float((w * self._y[idx]).sum() / w.sum())
        return {
            "direction": "long" if p_up > 0.5 else "short",
            "p_up": p_up,
            "confidence": abs(p_up - 0.5) * 2.0,
            "k": self.k, "samples": int(len(self._X)),
        }

    def predict_latest(self, candles: list[Any]) -> dict[str, Any] | None:
        feats = feature_matrix(candles)
        return self.predict(feats[-1])
