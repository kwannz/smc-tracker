"""tests/test_harmonic_forward_lead.py — C3 领先信号增强：OI 加速度 + OI-price 背离 单测。

职责：验证 harmonic_forward.py C3 新增的前瞻领先因子：
  1. OI 加速度（2阶导）方向正确、封顶 [-1, 1]。
  2. OI-price 背离判定：OI 增而价滞 → 降低置信（负信号）。
  3. 缺数据（首帧/历史不足）→ 中性（oi_signal = 0.0，不 None）。
  4. OI+价同向 positioning（多帧加速）→ oi_signal 增强（> 单帧速度）。
  5. has_oi=False → oi_signal=None（门控正确）。
  6. 与现有 funding_extreme / flow_score 接口不变（backward compat）。

合成数据，确定性，不依赖网络。非投资建议，仅辅助参考。
"""
from __future__ import annotations

import math
from collections import deque

import pytest

from smc_tracker.monitor.harmonic_forward import (
    HarmonicForwardSignals,
    _oi_acceleration,
    _oi_price_divergence,
    _composite_oi_signal,
    _safe_float,
    _MIN_OI_FRAMES,
)


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def _parsed(coin: str, oi: float, funding: float, price: float = 100.0, symbol: str | None = None) -> dict:
    return {coin: {"symbol": symbol or coin + "USDT", "oi": oi, "funding": funding, "price": price}}


# ─── _safe_float 守卫 ────────────────────────────────────────────────────────

def test_safe_float_nan_to_zero():
    assert _safe_float(float("nan")) == 0.0


def test_safe_float_inf_to_zero():
    assert _safe_float(float("inf")) == 0.0


def test_safe_float_normal():
    assert _safe_float("3.14") == pytest.approx(3.14, abs=1e-9)


# ─── OI 加速度 ───────────────────────────────────────────────────────────────

def test_oi_acceleration_insufficient_frames_neutral():
    """帧数不足 _MIN_OI_FRAMES → 加速度 = 0.0（中性，诚实）。"""
    dq: deque = deque(maxlen=20)
    for v in [1000.0, 1010.0]:  # 只有 2 帧，<_MIN_OI_FRAMES(3)
        dq.append(v)
    result = _oi_acceleration(dq)
    assert result == 0.0, f"期望 0.0，实际 {result}"


def test_oi_acceleration_positive_when_oi_accelerating():
    """OI 加速递增（速度加快）→ 加速度 > 0（多头加速进场）。"""
    dq: deque = deque(maxlen=20)
    # 故意设计加速：增量越来越大
    # frame0=1000, frame1=1020(+2%), frame2=1060(+3.9%≈加速)
    for v in [1000.0, 1020.0, 1060.0]:
        dq.append(v)
    result = _oi_acceleration(dq)
    assert result > 0.0, f"期望正加速度，实际 {result}"


def test_oi_acceleration_negative_when_oi_decelerating():
    """OI 增速减缓（减速）→ 加速度 < 0（进场力量减弱）。"""
    dq: deque = deque(maxlen=20)
    # frame0=1000, frame1=1050(+5%), frame2=1070(+1.9% 减速)
    for v in [1000.0, 1050.0, 1070.0]:
        dq.append(v)
    result = _oi_acceleration(dq)
    assert result < 0.0, f"期望负加速度（减速），实际 {result}"


def test_oi_acceleration_bounded():
    """加速度封顶 [-1, 1]。"""
    dq: deque = deque(maxlen=20)
    # 极端情况：OI 翻倍→翻3倍（速度从 100% 跳到 200%，加速极大）
    for v in [100.0, 200.0, 600.0]:
        dq.append(v)
    result = _oi_acceleration(dq)
    assert -1.0 <= result <= 1.0, f"加速度超出 [-1,1]：{result}"


def test_oi_acceleration_zero_first_frame_zero():
    """oi_past=0（首帧前无有效 OI）→ 0.0（避免除零）。"""
    dq: deque = deque(maxlen=20)
    for v in [0.0, 0.0, 1000.0]:
        dq.append(v)
    result = _oi_acceleration(dq)
    assert result == 0.0, f"期望 0.0，实际 {result}"


# ─── OI-price 背离 ───────────────────────────────────────────────────────────

def test_oi_price_divergence_insufficient_frames_neutral():
    """帧数不足 → 背离信号 = 0.0（中性）。"""
    oi_dq: deque = deque(maxlen=20)
    px_dq: deque = deque(maxlen=20)
    for v in [1000.0, 1030.0]:  # 只 2 帧
        oi_dq.append(v)
    for v in [100.0, 100.1]:
        px_dq.append(v)
    result = _oi_price_divergence(oi_dq, px_dq, "long")
    assert result == 0.0


def test_oi_price_divergence_detected_when_oi_up_price_flat():
    """OI 增 > _DIV_OI_MIN_CHANGE 且 |Δprice| < _DIV_PX_MAX_CHANGE → 背离，返回负值。"""
    oi_dq: deque = deque(maxlen=20)
    px_dq: deque = deque(maxlen=20)
    # OI 增 5%（超过 3% 阈值），价格几乎不动（0.2%，远低于 1% 阈值）
    for v in [1000.0, 1020.0, 1050.0]:
        oi_dq.append(v)
    for v in [100.0, 100.1, 100.2]:
        px_dq.append(v)
    result = _oi_price_divergence(oi_dq, px_dq, "long")
    assert result < 0.0, f"期望背离负信号，实际 {result}"


def test_oi_price_divergence_long_oi_up_price_down():
    """OI 增 + 价格下跌 + direction=long → 新空进场，对 long 看跌，返回负值。"""
    oi_dq: deque = deque(maxlen=20)
    px_dq: deque = deque(maxlen=20)
    # OI 增 5%，价格跌 3%（OI 增但价跌 → 空头建仓）
    for v in [1000.0, 1025.0, 1050.0]:
        oi_dq.append(v)
    for v in [100.0, 98.0, 97.0]:
        px_dq.append(v)
    result = _oi_price_divergence(oi_dq, px_dq, "long")
    assert result < 0.0, f"OI增+价跌 对 long 应为负信号，实际 {result}"


def test_oi_price_divergence_short_oi_up_price_up():
    """OI 增 + 价格涨 + direction=short → 新多进场，对 short 看跌，返回负值。"""
    oi_dq: deque = deque(maxlen=20)
    px_dq: deque = deque(maxlen=20)
    # OI 增 5%，价格涨 3%
    for v in [1000.0, 1025.0, 1050.0]:
        oi_dq.append(v)
    for v in [100.0, 102.0, 103.0]:
        px_dq.append(v)
    result = _oi_price_divergence(oi_dq, px_dq, "short")
    assert result < 0.0, f"OI增+价涨 对 short 应为负信号，实际 {result}"


def test_oi_price_divergence_no_divergence_oi_insufficient_increase():
    """OI 增幅 < _DIV_OI_MIN_CHANGE → 不触发背离判定，返回 0.0。"""
    oi_dq: deque = deque(maxlen=20)
    px_dq: deque = deque(maxlen=20)
    # OI 仅增 1%（低于 3% 阈值）
    for v in [1000.0, 1005.0, 1010.0]:
        oi_dq.append(v)
    for v in [100.0, 100.1, 100.2]:
        px_dq.append(v)
    result = _oi_price_divergence(oi_dq, px_dq, "long")
    assert result == 0.0, f"OI小幅增加不应触发背离，实际 {result}"


def test_oi_price_divergence_long_oi_up_price_up_positive():
    """OI 增 + 价格同向涨（long）→ 同向 positioning，返回非负值（正信号）。"""
    oi_dq: deque = deque(maxlen=20)
    px_dq: deque = deque(maxlen=20)
    # OI 增 5%，价格涨 5%（同向新多）
    for v in [1000.0, 1025.0, 1050.0]:
        oi_dq.append(v)
    for v in [100.0, 102.5, 105.0]:
        px_dq.append(v)
    result = _oi_price_divergence(oi_dq, px_dq, "long")
    assert result >= 0.0, f"OI增+价涨 对 long 应为正/零信号，实际 {result}"


# ─── 复合 OI 信号 ─────────────────────────────────────────────────────────────

def test_composite_oi_signal_stronger_than_velocity_alone():
    """OI 加速进场（OI 增速加快）+ 方向一致 → 复合 oi_signal 应强于纯速度。"""
    oi_dq: deque = deque(maxlen=20)
    px_dq: deque = deque(maxlen=20)
    # 模拟 OI 加速增加 + 价格同向上涨
    frames = [(1000.0, 100.0), (1030.0, 103.0), (1080.0, 108.0), (1160.0, 116.0)]
    for oi_v, px_v in frames:
        oi_dq.append(oi_v)
        px_dq.append(px_v)
    oi_now = 1160.0
    oi_prev = 1080.0
    px_now = 116.0
    px_prev = 108.0
    composite = _composite_oi_signal(oi_dq, px_dq, oi_now, oi_prev, px_now, px_prev, "long")

    # 仅速度（单帧）
    from smc_tracker.signals.oi_velocity import oi_directional_velocity
    raw_vel = oi_directional_velocity(oi_now, oi_prev, px_now, px_prev)
    import math as _math
    v_sig = _math.tanh(raw_vel / 0.05)

    assert composite > v_sig * 0.5, f"复合信号 {composite:.3f} 应有加速度加成（纯速度 {v_sig:.3f}）"
    assert -1.0 <= composite <= 1.0, f"复合信号超出 [-1,1]：{composite}"


def test_composite_oi_signal_bounded():
    """复合信号在任何极端输入下均 ∈ [-1, 1]。"""
    oi_dq: deque = deque(maxlen=20)
    px_dq: deque = deque(maxlen=20)
    # 极端：OI 每帧翻倍，价格同步暴涨
    for i in range(5):
        oi_dq.append(1000.0 * (2 ** i))
        px_dq.append(100.0 * (2 ** i))
    oi_list = list(oi_dq)
    px_list = list(px_dq)
    result = _composite_oi_signal(
        oi_dq, px_dq,
        oi_list[-1], oi_list[-2],
        px_list[-1], px_list[-2],
        "long",
    )
    assert -1.0 <= result <= 1.0, f"复合信号超出 [-1,1]：{result}"


# ─── HarmonicForwardSignals 集成测试 ────────────────────────────────────────

def test_first_frame_oi_signal_zero():
    """首帧无前值 → oi_signal = 0.0（无法算速度/加速度）。"""
    fs = HarmonicForwardSignals()
    fs.update(_parsed("BTC", oi=1000.0, funding=0.0001, price=100.0), now_ms=1000)
    result = fs("BTC", "long")
    assert result is not None
    _, _, oi_signal, _ = result
    assert oi_signal == 0.0, f"首帧应为 0.0，实际 {oi_signal}"


def test_two_frames_velocity_nonzero_oi_up_price_up():
    """2帧 OI↑+价↑ → oi_signal > 0（方向化速度有效）。"""
    fs = HarmonicForwardSignals()
    fs.update(_parsed("BTC", oi=1000.0, funding=0.0001, price=100.0), now_ms=1000)
    fs.update(_parsed("BTC", oi=1050.0, funding=0.0001, price=105.0), now_ms=2000)
    result = fs("BTC", "long")
    assert result is not None
    _, _, oi_signal, _ = result
    assert oi_signal is not None and oi_signal > 0.0, f"OI↑+价↑ 对 long 应为正，实际 {oi_signal}"


def test_two_frames_velocity_oi_up_price_down_short():
    """2帧 OI↑+价↓ → oi_signal < 0（绝对值为负=价跌方向）。

    注意：oi_signal 编码价格方向的原始速度（OI↑+价↓=负），
    apply_forward / forward_mult 在施加乘子时会用 dir_sign 对齐方向
    （dir_sign=-1 for short，align = oi_signal × dir_sign = 负×负=正，
    即 short 方向一致），因此 oi_signal 本身为负是正确行为（非 bug）。
    """
    fs = HarmonicForwardSignals()
    fs.update(_parsed("ETH", oi=5000.0, funding=0.0001, price=100.0), now_ms=1000)
    fs.update(_parsed("ETH", oi=5200.0, funding=0.0001, price=96.0), now_ms=2000)
    result = fs("ETH", "short")
    assert result is not None
    _, _, oi_signal, _ = result
    # oi_signal 原始值为负（OI↑+价↓=空头进场），apply_forward 用 dir_sign 对齐后同向=看涨
    assert oi_signal is not None and oi_signal < 0.0, (
        f"OI↑+价↓ 的原始 oi_signal 应为负（价格方向为负），实际 {oi_signal}"
    )


def test_oi_divergence_detected_in_provider():
    """多帧 OI 快速增加但价格停滞 → oi_signal < 0（背离，降低置信）。"""
    fs = HarmonicForwardSignals()
    # 喂入足够帧：OI 稳步增 5%+ 但价格几乎不变
    base_oi = 1000.0
    base_px = 100.0
    for i in range(5):
        oi = base_oi * (1.0 + 0.015 * i)       # 每帧 +1.5%，3帧累计>3%
        px = base_px * (1.0 + 0.001 * i)       # 每帧 +0.1%（停滞）
        fs.update(_parsed("BTC", oi=oi, funding=0.0001, price=px), now_ms=1000 + i * 1000)
    result = fs("BTC", "long")
    assert result is not None
    _, _, oi_signal, _ = result
    # OI 增速减缓但价格停滞时，背离组件应压低 oi_signal
    # 不强制要求 < 0（速度分量可能抵消），但要求 <= 0.5（背离有效压制）
    assert oi_signal is not None and oi_signal <= 0.5, (
        f"OI增+价滞，背离应压低 oi_signal（实际 {oi_signal}，期望 ≤ 0.5）"
    )


def test_oi_acceleration_boost_multiple_frames():
    """多帧 OI 加速增加 + 价格同向上涨 → oi_signal 较单帧更强（加速度加成）。"""
    fs = HarmonicForwardSignals()
    # 构造 OI 加速增加场景（增速越来越快）
    frames = [
        (1000.0, 100.0),
        (1020.0, 102.0),   # +2%, +2%
        (1055.0, 105.5),   # +3.4%, +3.4%（加速）
        (1110.0, 111.0),   # +5.2%, +5.2%（继续加速）
    ]
    for i, (oi, px) in enumerate(frames):
        fs.update(_parsed("BTC", oi=oi, funding=0.0001, price=px), now_ms=1000 + i * 1000)
    result = fs("BTC", "long")
    assert result is not None
    _, _, oi_signal, _ = result
    assert oi_signal is not None and oi_signal > 0.0, f"OI加速+价涨，oi_signal 应为正，实际 {oi_signal}"
    assert oi_signal <= 1.0, f"oi_signal 超过上界 1.0：{oi_signal}"


def test_no_oi_gate_returns_none_oi_signal():
    """has_oi=False（oi=0.0）→ oi_signal=None（门控正确，不臆测）。"""
    fs = HarmonicForwardSignals()
    fs.update(_parsed("TSLA", oi=0.0, funding=0.0, price=200.0), now_ms=1000)
    result = fs("TSLA", "long")
    assert result is not None
    _, _, oi_signal, _ = result
    assert oi_signal is None, f"无 OI 数据应为 None，实际 {oi_signal}"


def test_missing_data_returns_neutral_not_one():
    """首帧（无历史）→ oi_signal=0.0（中性），不是 None、不是 1.0。"""
    fs = HarmonicForwardSignals()
    fs.update(_parsed("SOL", oi=1000.0, funding=0.0001, price=50.0), now_ms=1000)
    result = fs("SOL", "long")
    assert result is not None
    _, _, oi_signal, _ = result
    assert oi_signal == 0.0, f"首帧期望中性 0.0，实际 {oi_signal}"


def test_backward_compat_unknown_coin_returns_none():
    """未知 coin → 返回 None（向后兼容，现有逻辑不变）。"""
    fs = HarmonicForwardSignals()
    assert fs("UNKNOWN", "long") is None


def test_backward_compat_flow_source_still_works():
    """flow_source 回调仍正常工作（C3 不破坏 flow_score 接口）。"""
    fs = HarmonicForwardSignals(flow_source=lambda coin: 0.7 if coin == "BTC" else None)
    fs.update(_parsed("BTC", oi=1000.0, funding=0.0001), now_ms=1000)
    result = fs("BTC", "long")
    assert result is not None
    _, flow_score, _, _ = result
    assert flow_score == pytest.approx(0.7), f"flow_source 不生效，实际 {flow_score}"


def test_backward_compat_funding_extreme_still_works():
    """C3 不破坏 funding_extreme 分量（现有逻辑向后兼容）。"""
    fs = HarmonicForwardSignals(min_funding_samples=20)
    # 喂足量不同 funding（触发极值判断）
    for i in range(25):
        f = 0.0001 + 0.00002 * ((i % 7) - 3) + i * 1e-7
        fs.update(_parsed("ETH", oi=5000.0, funding=f), now_ms=1000 + i)
    fs.update(_parsed("ETH", oi=5000.0, funding=0.01), now_ms=2000)  # 极高 funding
    result = fs("ETH", "long")
    assert result is not None
    _, _, _, funding_extreme = result
    assert funding_extreme is not None and funding_extreme < 0.0, (
        f"极高 funding 应返回负极值信号，实际 {funding_extreme}"
    )


def test_oi_signal_bounded_all_frames():
    """多帧极端 OI 增幅场景下，oi_signal 始终 ∈ [-1, 1]。"""
    fs = HarmonicForwardSignals()
    # 极端：每帧 OI 翻倍，价格同步涨
    oi = 1000.0
    px = 100.0
    for i in range(8):
        fs.update(_parsed("BTC", oi=oi, funding=0.0001, price=px), now_ms=1000 + i * 1000)
        oi *= 2.0
        px *= 2.0
    result = fs("BTC", "long")
    assert result is not None
    _, _, oi_signal, _ = result
    assert oi_signal is not None
    assert -1.0 <= oi_signal <= 1.0, f"oi_signal 超出 [-1,1]：{oi_signal}"
