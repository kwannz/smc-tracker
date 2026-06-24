"""谐波前瞻信号 provider —— 把 Bitget ticker 快照转成 forward_mult 的输入。

设计（设计 v2 §2/§3；QA 宇宙错配修复的自包含实现）：
谐波宇宙（Bitget 成交额 top-N + TradFi）的 OI/funding/price 是 Bitget ticker **现成字段**。
本 provider 每轮 refresh 接收 harmonic_monitor 取的 tickers 快照，构建每币 CoinSignalProfile +
算三个**互不重叠**的前瞻分量供 apply_forward 施加置信乘子。

三分量（防双计：各自独立来源）：
- **flow_score**：来自 BitgetTradeMonitor 的资金流加速度（tanh(accel)，**仅加速度一项**，
  非"三合一"——不含盘口失衡、不含 OI）。无 flow_source/无样本 → None（中性）。
- **oi_signal**：方向化 OI 复合信号 ∈[-1,1]（C2/C3 加强版）——
    · oi_directional_velocity: 当帧速度（OI↑+价↑=新多）；
    · oi_acceleration: 2阶导（OI 速度变化，加速建仓=强信号）；
    · oi_price_divergence: OI 增而价滞（乖离=潜在反转/排空头，有别于速度）。
  三子分量加权合成（权重可调），最终 tanh 归一；按 profile.has_oi 门控。
  缺历史（首帧/样本不足）→ 各子分量中性 0；整体 oi_signal 输出 0.0。
- **funding_extreme**：funding z-score 极值反转。**变化才采样**（C1 修复：Bitget funding 8h 才变，
  按 refresh 采样会灌满重复值致 z 失真）；z 计算排除当前值；按 profile.has_funding 门控
  （纯股票 funding=0 跳过，不臆测）。

C3 新增 OI 加速度/背离分量（本文件）：
- 维护每币定长 OI/price 时序 deque（默认 20 帧，内存有界，非阻塞）。
- OI 加速度：速度序列 diff 的 2阶导，归一化；样本不足 → 0.0（中性，诚实）。
- OI-price 背离：OI 显著增加但价格停滞/反向（拥挤/清空头信号）；funding 也作背离参照。
- 所有分量封顶 [-1, 1]；合成后 tanh 封顶保证有界。
- 不改变 __call__ 返回接口（仍返回 4-tuple），新因子嵌入 oi_signal 分量内。
- 不动 app.py / forward_confirm.py（文件边界严格遵守）。

不动 OI 监控/不重排 app 启动序：所有数据自 harmonic_monitor 已有的 BitgetREST 会话取得。
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Callable

from ..signals.coin_profile import CoinSignalProfile, build_profile
from ..signals.funding_extreme import funding_extreme_signal
from ..signals.oi_velocity import oi_directional_velocity

# ── OI 信号归一化基（5% 方向化 OI 变化 → tanh≈0.76）
_OI_SCALE = 0.05

# ── OI 复合信号子分量权重（合计=1.0；C3 加速度/背离各 0.3，速度保留 0.4）
_W_VELOCITY: float = 0.40      # 当帧方向化速度
_W_ACCEL: float = 0.30         # 2阶导加速度
_W_DIVERGENCE: float = 0.30    # OI-price 背离

# ── 最少 OI 历史帧数：少于此则加速度/背离分量均=0（中性，诚实）
_MIN_OI_FRAMES: int = 3

# ── OI-price 背离判定阈值（OI 涨幅超此且价滞 → 背离）
_DIV_OI_MIN_CHANGE: float = 0.03   # OI 至少涨 3%
_DIV_PX_MAX_CHANGE: float = 0.01   # 同期价格涨幅 < 1% 算"滞"

# ── 背离信号强度（背离确认后固定权重；可扩展为可调）
_DIVERGENCE_SIGNAL: float = 0.80


def _safe_float(v: object) -> float:
    """安全转 float，无效值（None/NaN/inf）→ 0.0（util.to_float 逻辑，不引入循环依赖）。"""
    try:
        f = float(v)  # type: ignore[arg-type]
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f
    except (TypeError, ValueError):
        return 0.0


def _oi_acceleration(oi_deque: deque, scale: float = _OI_SCALE) -> float:
    """OI 加速度（2阶导）：基于短时 OI 序列计算速度变化。

    算法（第一性原理，perp 标准）：
        速度 v_i = (oi[i] - oi[i-1]) / oi[i-1]（分数率）
        加速度 a = (v_last - v_prev) / 1           # 时间单位 = 1 帧
    返回 tanh(a / scale)，封顶 [-1, 1]。
    样本不足（< _MIN_OI_FRAMES）→ 0.0（中性）。

    注意：仅此函数计算，不重复 velocity（velocity 在调用处单独算）。
    """
    frames = list(oi_deque)
    if len(frames) < _MIN_OI_FRAMES:
        return 0.0

    # 只取最后 3 帧算加速度（短窗口，响应快）
    o0, o1, o2 = frames[-3], frames[-2], frames[-1]
    if o0 <= 0.0 or o1 <= 0.0:
        return 0.0
    v_prev = (o1 - o0) / o0
    v_last = (o2 - o1) / o1 if o1 > 0.0 else 0.0
    accel = v_last - v_prev
    return math.tanh(accel / scale)


def _oi_price_divergence(
    oi_deque: deque,
    px_deque: deque,
    direction: str,
) -> float:
    """OI-price 背离信号：OI 增而价滞/反向。

    业界定义（perp 微观结构）：
      - OI 快速增加但价格不跟（Δoi > _DIV_OI_MIN_CHANGE 且 |Δprice| < _DIV_PX_MAX_CHANGE）
        → 表示大量反方向持仓建立（可能是对冲/空头布局），不是净多头 positioning。
      - 对 forming 谐波：PRZ 方向的 OI-price 背离 → 反转前兆（前瞻信号）。
      - 对 completed 谐波：价格未跟 OI → setup 方向的确认减弱。

    返回信号方向化：
      - 如果 direction="long"（看涨）且检测到 OI-price 背离（OI 增价滞/下），
        说明 OI 增加但方向相反（看跌），对 long 看跌，返回负值（-_DIVERGENCE_SIGNAL）。
      - 如果 direction="short"（看跌）且检测到 OI-price 背离（OI 增价涨而方向是空），
        返回负值（同理减弱置信）。
      - 无背离 → 0.0（中性）。
      - 缺数据 → 0.0（诚实）。

    注：仅诊断"OI 增量"方向与 setup 方向的一致性；与 velocity 分量互补（velocity 关注
    最近一帧，divergence 关注短窗口累积变化率）。
    """
    oi_frames = list(oi_deque)
    px_frames = list(px_deque)
    n = min(len(oi_frames), len(px_frames))
    if n < _MIN_OI_FRAMES:
        return 0.0

    # 取最近 3 帧的起止值（与加速度窗口一致）
    oi_start = oi_frames[-3]
    oi_end = oi_frames[-1]
    px_start = px_frames[-3]
    px_end = px_frames[-1]

    if oi_start <= 0.0 or px_start <= 0.0:
        return 0.0

    d_oi = (oi_end - oi_start) / oi_start     # OI 分数率变化（可正可负）
    d_px = (px_end - px_start) / px_start      # 价格分数率变化

    # 只在 OI 显著增加时判断背离（OI 减少是去杠杆，另一维度，不在此处理）
    if d_oi < _DIV_OI_MIN_CHANGE:
        return 0.0

    # OI 增而价滞：多方向建仓但价格未跟 → 潜在反转（不论哪个方向）
    if abs(d_px) < _DIV_PX_MAX_CHANGE:
        # 价格停滞 + OI 快速增加：可能是双向挂单成交或大量对冲，setup 置信降低
        # 与 setup direction 无关，统一返回负信号（降低置信）
        return -_DIVERGENCE_SIGNAL

    # OI 增且价涨/跌明显：检查方向一致性（与 velocity 互补，此处更长窗口）
    if direction == "long" and d_px < 0.0:
        # OI 增 + 价跌 = 新空进场，对 long 看跌
        return -_DIVERGENCE_SIGNAL * min(1.0, abs(d_px) / 0.05)
    if direction == "short" and d_px > 0.0:
        # OI 增 + 价涨 = 新多进场，对 short 看跌
        return -_DIVERGENCE_SIGNAL * min(1.0, abs(d_px) / 0.05)

    # OI 增且价格方向与 setup 一致 → 同向 positioning，给正值
    align_strength = min(1.0, abs(d_px) / 0.05) * 0.5
    return align_strength


def _composite_oi_signal(
    oi_deque: deque,
    px_deque: deque,
    oi_now: float,
    oi_prev: float,
    px_now: float,
    px_prev: float,
    direction: str,
) -> float:
    """复合 OI 信号：speed + acceleration + divergence 加权合成，tanh 归一 → [-1, 1]。

    方向性：
    - velocity：(Δoi/oi_prev) × sign(Δprice)，OI↑价↑=看涨=正。
    - accel：速度在加速=同向强化，减速=弱化。OI 加速建仓=看涨+; 加速减仓=不明，
      此处用 abs 加速度 × velocity_sign（方向由 velocity 决定，accel 放大/衰减）。
    - divergence：独立背离判定（OI 增价滞 → 降低置信）。

    缺数据（oi_prev <= 0 / px 不变）→ velocity=0；frames<3 → accel=0, div=0。
    """
    # velocity（单帧，原有逻辑）
    raw_vel = oi_directional_velocity(oi_now, oi_prev, px_now, px_prev)
    v_sig = math.tanh(raw_vel / _OI_SCALE) if _OI_SCALE > 0.0 else 0.0

    # acceleration（2阶导；方向化：vel_sign × |accel|，加速同向=正，减速=负）
    a_raw = _oi_acceleration(oi_deque, scale=_OI_SCALE)
    # velocity 方向符号：有速度则与 vel 同向；无速度则中性
    vel_sign = 1.0 if v_sig > 0.0 else (-1.0 if v_sig < 0.0 else 0.0)
    # 加速度本身有方向（加速建仓 vs 减速），保留其符号并按 vel_sign 调制
    # 若 vel=0（首帧/价无变化），accel 也中性
    a_sig = a_raw * (abs(vel_sign) if vel_sign != 0.0 else 0.0)

    # divergence（独立，方向化）
    d_sig = _oi_price_divergence(oi_deque, px_deque, direction)

    # 加权合成（三分量之和，已各自 ∈ [-1,1]，加权后封顶）
    combined = _W_VELOCITY * v_sig + _W_ACCEL * a_sig + _W_DIVERGENCE * d_sig
    return max(-1.0, min(1.0, combined))


class HarmonicForwardSignals:
    """谐波前瞻信号缓存：profile + funding 历史 + OI/price 时序 + flow_score 源。callable 作 provider。

    C3 增强（本迭代）：
    - _oi_hist / _px_hist：每币定长 OI/价格时序 deque（内存有界 = maxlen=oi_hist_maxlen）。
    - 复合 oi_signal 包含：速度（原有）+ OI 加速度（2阶导）+ OI-price 背离三子分量。
    - 接口不变：__call__ 仍返回 (profile, flow_score, oi_signal, funding_extreme) 4-tuple。
    """

    __slots__ = (
        "min_funding_samples", "_profile", "_funding_hist", "_flow_source",
        "_oi_signal", "_last_oi", "_last_px",
        "_oi_hist", "_px_hist",         # C3：时序 deque（OI 加速度/背离用）
        "_oi_hist_maxlen",
    )

    def __init__(
        self,
        min_funding_samples: int = 20,
        hist_maxlen: int = 300,
        flow_source: Callable[[str], float | None] | None = None,
        oi_hist_maxlen: int = 20,
    ) -> None:
        self.min_funding_samples = min_funding_samples
        self._profile: dict[str, CoinSignalProfile] = {}
        self._funding_hist: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=hist_maxlen)
        )
        self._flow_source = flow_source
        self._oi_signal: dict[str, float] = {}   # coin -> 最近一帧复合 OI 信号 ∈[-1,1]
        self._last_oi: dict[str, float] = {}     # 上一帧 OI（算速度）
        self._last_px: dict[str, float] = {}     # 上一帧 price（算速度方向）
        # C3：时序 deque（OI 加速度+背离需要多帧历史）
        self._oi_hist_maxlen: int = oi_hist_maxlen
        self._oi_hist: dict[str, deque] = {}     # coin -> deque[float](OI 历史帧)
        self._px_hist: dict[str, deque] = {}     # coin -> deque[float](price 历史帧)

    def _get_oi_deque(self, coin: str) -> deque:
        """懒建 OI 时序 deque（内存有界）。"""
        if coin not in self._oi_hist:
            self._oi_hist[coin] = deque(maxlen=self._oi_hist_maxlen)
        return self._oi_hist[coin]

    def _get_px_deque(self, coin: str) -> deque:
        """懒建 price 时序 deque（内存有界）。"""
        if coin not in self._px_hist:
            self._px_hist[coin] = deque(maxlen=self._oi_hist_maxlen)
        return self._px_hist[coin]

    def update(self, parsed: dict[str, dict], now_ms: int) -> None:
        """用一轮 ticker 快照更新。

        parsed: {coin: {"symbol":str, "oi":float, "funding":float, "price":float}}。
        """
        for coin, d in parsed.items():
            oi = _safe_float(d.get("oi", 0.0))
            funding = _safe_float(d.get("funding", 0.0))
            price = _safe_float(d.get("price", 0.0))
            symbol = str(d.get("symbol", coin))
            self._profile[coin] = build_profile(coin, symbol, oi=oi, funding=funding)

            # funding：仅在值变化时采样（C1：避免 8h 不变期内灌满重复值致 z 失真）
            hist = self._funding_hist[coin]
            if not hist or hist[-1] != funding:
                hist.append(funding)

            # C3：追加 OI/price 到时序 deque（每帧都追加，保持时间均匀间隔）
            oi_dq = self._get_oi_deque(coin)
            px_dq = self._get_px_deque(coin)
            if oi > 0:
                oi_dq.append(oi)
            if price > 0:
                px_dq.append(price)

            # 复合 OI 信号：velocity + acceleration + divergence
            # 需上一帧 OI+price（首帧无前值→0），时序 deque 供加速度/背离使用
            prev_oi = self._last_oi.get(coin)
            prev_px = self._last_px.get(coin)
            if prev_oi is not None and prev_px is not None:
                # 注意：此处 direction 暂用中性"long"（后续 __call__ 时会按方向重算）
                # 实际上 velocity/accel 已含方向，divergence 在 __call__ 时再传入真实方向
                # 这里先存无方向的基础值，__call__ 时结合方向返回
                raw_vel = oi_directional_velocity(oi, prev_oi, price, prev_px)
                v_sig = math.tanh(raw_vel / _OI_SCALE) if _OI_SCALE > 0.0 else 0.0
                self._oi_signal[coin] = v_sig   # 暂存速度分量，__call__ 时合成完整信号
            else:
                self._oi_signal[coin] = 0.0
            if oi > 0:
                self._last_oi[coin] = oi
            if price > 0:
                self._last_px[coin] = price

    def __call__(
        self, coin: str, direction: str
    ) -> tuple[CoinSignalProfile, float | None, float | None, float | None] | None:
        """apply_forward 回调：返回 (profile, flow_score, oi_signal, funding_extreme) 或 None。

        C3：oi_signal 在此处完整合成（含加速度+背离），以便接收真实 direction 参数。
        """
        profile = self._profile.get(coin)
        if profile is None:
            return None
        # flow_score：BitgetTradeMonitor 资金流加速度（仅此一项）；无源/无样本→None
        flow_score: float | None = self._flow_source(coin) if self._flow_source else None
        # oi_signal：复合 OI（velocity + acceleration + divergence），按 has_oi 门控
        if profile.has_oi:
            oi_dq = self._oi_hist.get(coin, deque())
            px_dq = self._px_hist.get(coin, deque())
            prev_oi = self._last_oi.get(coin)
            prev_px = self._last_px.get(coin)
            # 取最新 OI/price（deque 末尾）
            oi_now = list(oi_dq)[-1] if oi_dq else 0.0
            px_now = list(px_dq)[-1] if px_dq else 0.0
            # prev_oi/prev_px 在 update() 里已设为"上上帧"（update 末尾才更新）
            # 实际此时 _last_oi 存的是当前帧（已在 update 中更新），
            # 需从 deque 倒数第2帧取前值
            oi_list = list(oi_dq)
            px_list = list(px_dq)
            if len(oi_list) >= 2 and len(px_list) >= 2:
                oi_prev_frame = oi_list[-2]
                px_prev_frame = px_list[-2]
                oi_signal: float | None = _composite_oi_signal(
                    oi_dq, px_dq,
                    oi_now, oi_prev_frame,
                    px_now, px_prev_frame,
                    direction,
                )
            else:
                # 首帧：无前值，速度/加速/背离均 0（中性，诚实）
                oi_signal = 0.0
        else:
            oi_signal = None
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
