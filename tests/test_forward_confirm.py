"""前瞻置信合成器 forward_confirm 单测（确定性，纯函数）。

QA 修复内建：
- 防双重计数：flow_score（已含加速度+挂单失衡+OI 三合一）作唯一"流/盘口/OI"分量，
  只额外叠加 funding 极值（flow_score 不含）。
- 按 profile 门控：funding 分量仅在 has_funding 时计入（纯股票代币 funding=0 跳过）。
- 缺数据=中性（mult=1.0），不对无数据币佯装确认。
- 有界：mult ∈ [0.80, 1.30]。
"""
from __future__ import annotations

from smc_tracker.signals.coin_profile import build_profile
from smc_tracker.signals.forward_confirm import forward_mult


def _crypto():
    return build_profile("BTC", "BTCUSDT", oi=1.0, funding=0.0001)


def _stock_no_funding():
    return build_profile("TSLA", "TSLAUSDT", oi=1.0, funding=0.0)


def test_no_signals_neutral():
    """无任何前瞻信号 → 乘子 1.0（中性，不佯装）。"""
    mult, note = forward_mult("long", _crypto(), flow_score=None, funding_extreme=None)
    assert mult == 1.0
    assert isinstance(note, str)


def test_flow_aligned_boosts():
    """看多 + flow_score 看涨（+）→ 乘子 > 1.0。"""
    mult, _ = forward_mult("long", _crypto(), flow_score=0.8, funding_extreme=None)
    assert mult > 1.0


def test_flow_opposed_penalizes():
    """看多 + flow_score 看跌（−）→ 乘子 < 1.0。"""
    mult, _ = forward_mult("long", _crypto(), flow_score=-0.8, funding_extreme=None)
    assert mult < 1.0


def test_short_direction_alignment():
    """看空 + flow_score 看跌（−）→ 同向 → 乘子 > 1.0。"""
    mult, _ = forward_mult("short", _crypto(), flow_score=-0.7, funding_extreme=None)
    assert mult > 1.0


def test_funding_gated_off_for_zero_funding_coin():
    """has_funding False（纯股票）→ funding 分量被忽略 → 仅 flow 决定（此处 None → 1.0）。"""
    mult, note = forward_mult("long", _stock_no_funding(), flow_score=None, funding_extreme=0.9)
    assert mult == 1.0


def test_funding_gated_on_for_funding_coin():
    """has_funding True + funding_extreme 同向 → 乘子 > 1.0。"""
    mult, _ = forward_mult("long", _crypto(), flow_score=None, funding_extreme=0.9)
    assert mult > 1.0


def test_mult_clamped_upper():
    """极端同向信号 → 乘子封顶 1.30。"""
    mult, _ = forward_mult("long", _crypto(), flow_score=1.0, funding_extreme=1.0)
    assert mult <= 1.30


def test_mult_clamped_lower():
    """极端反向信号 → 乘子封底 0.80。"""
    mult, _ = forward_mult("long", _crypto(), flow_score=-1.0, funding_extreme=-1.0)
    assert mult >= 0.80


def test_unknown_direction_neutral():
    """方向未知 → 无法对齐 → 中性 1.0。"""
    mult, _ = forward_mult("", _crypto(), flow_score=0.9, funding_extreme=0.9)
    assert mult == 1.0


def test_note_reports_components():
    """note 诚实标注用了哪些分量（含跳过原因）。"""
    _, note = forward_mult("long", _stock_no_funding(), flow_score=0.5, funding_extreme=0.9)
    assert "funding" in note.lower() or "资金费" in note  # 应说明 funding 因 has_funding=False 跳过


# ── apply_forward：对 completed+forming 都施加 forward_mult（解除 completed 门控） ──
from smc_tracker.signals.forward_confirm import apply_forward


class _StubSetup:
    """duck-typed setup（含 forward_mult 需要的属性）。"""
    def __init__(self, coin, direction, confidence, completed):
        self.coin = coin
        self.direction = direction
        self.confidence = confidence
        self.completed = completed
        self.forward = None


def test_apply_forward_boosts_both_completed_and_forming():
    """关键 QA 修复：forming 也享前瞻 boost（不再被 completed 门控）。"""
    prof = _crypto()
    completed = _StubSetup("BTC", "long", 0.60, True)
    forming = _StubSetup("BTC", "long", 0.50, False)
    apply_forward([completed, forming], lambda c, d: (prof, 0.8, None, None))
    assert completed.confidence > 0.60
    assert forming.confidence > 0.50      # forming 同样被 boost
    assert completed.forward is not None
    assert forming.forward is not None


def test_apply_forward_no_data_leaves_unchanged():
    """provider 返回 None（无数据）→ 置信不变、forward 保持 None（诚实，不佯装）。"""
    s = _StubSetup("XYZ", "long", 0.60, True)
    apply_forward([s], lambda c, d: None)
    assert s.confidence == 0.60
    assert s.forward is None


def test_apply_forward_caps_confidence():
    """boost 后置信封顶 max_conf。"""
    prof = _crypto()
    s = _StubSetup("BTC", "long", 0.89, True)
    apply_forward([s], lambda c, d: (prof, 1.0, 1.0, 1.0), max_conf=0.90)
    assert s.confidence <= 0.90


# ── oi_signal 分量（C2：方向化 OI 接进 forward_mult） ──
def test_oi_aligned_boosts():
    """看多 + OI 新多 positioning（+）→ 乘子 > 1.0。"""
    mult, _ = forward_mult("long", _crypto(), oi_signal=0.6)
    assert mult > 1.0


def test_oi_gated_by_has_oi():
    """has_oi False（OI=0）→ OI 分量忽略 → 中性 1.0。"""
    no_oi = build_profile("FOO", "FOOUSDT", oi=0.0, funding=0.0)
    mult, _ = forward_mult("long", no_oi, oi_signal=0.6)
    assert mult == 1.0
