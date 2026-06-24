"""谐波前瞻信号 provider —— 把 Bitget ticker 快照转成 forward_mult 的输入。

设计（设计 v2 §2/§3；QA 宇宙错配修复的自包含实现）：
谐波宇宙（Bitget 成交额 top-N + TradFi）的 OI/funding/price 是 Bitget ticker **现成字段**。
本 provider 每轮 refresh 接收 harmonic_monitor 取的 tickers 快照，构建每币 CoinSignalProfile +
算三个**互不重叠**的前瞻分量供 apply_forward 施加置信乘子。

三分量（防双计：各自独立来源）：
- **flow_score**：来自 BitgetTradeMonitor 的资金流加速度（tanh(accel)，**仅加速度一项**，
  非"三合一"——不含盘口失衡、不含 OI）。无 flow_source/无样本 → None（中性）。
- **oi_signal**：方向化 OI 速度 oi_directional_velocity（OI↑+价↑=新多，C2 修复：真接线非孤儿），
  tanh 归一；按 profile.has_oi 门控。
- **funding_extreme**：funding z-score 极值反转。**变化才采样**（C1 修复：Bitget funding 8h 才变，
  按 refresh 采样会灌满重复值致 z 失真）；z 计算排除当前值；按 profile.has_funding 门控
  （纯股票 funding=0 跳过，不臆测）。

不动 OI 监控/不重排 app 启动序：所有数据自 harmonic_monitor 已有的 BitgetREST 会话取得。
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Callable

from ..signals.coin_profile import CoinSignalProfile, build_profile
from ..signals.funding_extreme import funding_extreme_signal
from ..signals.oi_velocity import oi_directional_velocity

_OI_SCALE = 0.05  # OI 速度归一基（5% 方向化 OI 变化 → tanh≈0.76）


class HarmonicForwardSignals:
    """谐波前瞻信号缓存：profile + funding 历史 + OI/price 前帧 + flow_score 源。callable 作 provider。"""

    __slots__ = (
        "min_funding_samples", "_profile", "_funding_hist", "_flow_source",
        "_oi_signal", "_last_oi", "_last_px",
    )

    def __init__(
        self,
        min_funding_samples: int = 20,
        hist_maxlen: int = 300,
        flow_source: Callable[[str], float | None] | None = None,
    ) -> None:
        self.min_funding_samples = min_funding_samples
        self._profile: dict[str, CoinSignalProfile] = {}
        self._funding_hist: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=hist_maxlen)
        )
        self._flow_source = flow_source
        self._oi_signal: dict[str, float] = {}   # coin -> 最近一帧方向化 OI 信号 ∈[-1,1]
        self._last_oi: dict[str, float] = {}     # 上一帧 OI（算速度）
        self._last_px: dict[str, float] = {}     # 上一帧 price（算速度方向）

    def update(self, parsed: dict[str, dict], now_ms: int) -> None:
        """用一轮 ticker 快照更新。

        parsed: {coin: {"symbol":str, "oi":float, "funding":float, "price":float}}。
        """
        for coin, d in parsed.items():
            oi = float(d.get("oi", 0.0) or 0.0)
            funding = float(d.get("funding", 0.0) or 0.0)
            price = float(d.get("price", 0.0) or 0.0)
            symbol = str(d.get("symbol", coin))
            self._profile[coin] = build_profile(coin, symbol, oi=oi, funding=funding)

            # funding：仅在值变化时采样（C1：避免 8h 不变期内灌满重复值致 z 失真）
            hist = self._funding_hist[coin]
            if not hist or hist[-1] != funding:
                hist.append(funding)

            # 方向化 OI 信号：需上一帧 OI+price（首帧无前值→0）
            prev_oi = self._last_oi.get(coin)
            prev_px = self._last_px.get(coin)
            if prev_oi is not None and prev_px is not None:
                raw = oi_directional_velocity(oi, prev_oi, price, prev_px)
                self._oi_signal[coin] = math.tanh(raw / _OI_SCALE)
            else:
                self._oi_signal[coin] = 0.0
            if oi > 0:
                self._last_oi[coin] = oi
            if price > 0:
                self._last_px[coin] = price

    def __call__(
        self, coin: str, direction: str
    ) -> tuple[CoinSignalProfile, float | None, float | None, float | None] | None:
        """apply_forward 回调：返回 (profile, flow_score, oi_signal, funding_extreme) 或 None。"""
        profile = self._profile.get(coin)
        if profile is None:
            return None
        # flow_score：BitgetTradeMonitor 资金流加速度（仅此一项）；无源/无样本→None
        flow_score: float | None = self._flow_source(coin) if self._flow_source else None
        # oi_signal：方向化 OI（按 has_oi 门控）
        oi_signal: float | None = self._oi_signal.get(coin, 0.0) if profile.has_oi else None
        # funding_extreme：z 极值，排除当前值（hist[:-1]），按 has_funding 门控
        if profile.has_funding:
            hist = list(self._funding_hist.get(coin, ()))
            funding_now = hist[-1] if hist else 0.0
            funding_extreme: float | None = funding_extreme_signal(
                funding_now, hist[:-1], min_samples=self.min_funding_samples
            )
        else:
            funding_extreme = None
        return (profile, flow_score, oi_signal, funding_extreme)
