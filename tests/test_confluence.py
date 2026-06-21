"""多信号叠加共振单测（合成 DB 数据，无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals import ConfluenceAggregator
from smc_tracker.storage import Store


def _store():
    return Store(Path(tempfile.mkdtemp()) / "s.db")


def test_two_source_confluence():
    s = _store()
    # 共识 + 背离 都看多 PEPE（背离用 bullish；coin 命名不一 kPEPE/PEPE 归一化后一致）
    s.insert_consensus((100, "kPEPE", "long", 3, 1, 1e6, 1.0, "庄"))
    s.insert_divergence((100, "PEPE", "bullish", 0.3, 0.0001, 0.03, 100000, "x"))
    out = ConfluenceAggregator(s, min_sources=2).scan(now_ms=200)
    assert len(out) == 1
    sig = out[0]
    assert sig.coin == "PEPE" and sig.direction == "long" and sig.n_sources == 2
    assert set(sig.sources) == {"共识", "背离"}
    s.close()


def test_single_source_no_confluence():
    s = _store()
    s.insert_consensus((100, "BTC", "short", 3, 0, 1e6, 1.0, "庄"))
    assert ConfluenceAggregator(s, min_sources=2).scan(now_ms=200) == []
    s.close()


def test_conflict_no_confluence():
    s = _store()
    # 共识看多、背离看空 → 1对1 矛盾，不出
    s.insert_consensus((100, "DOGE", "long", 3, 1, 1e6, 1.0, "庄"))
    s.insert_divergence((100, "DOGE", "bearish", 0.3, 0.0001, 0.03, -100000, "x"))
    assert ConfluenceAggregator(s, min_sources=2).scan(now_ms=200) == []
    s.close()


def test_three_source_higher_score():
    s = _store()
    s.insert_consensus((100, "WIF", "long", 3, 0, 1e6, 1.0, "庄"))
    s.insert_divergence((100, "WIF", "bullish", 0.3, 0.0001, 0.03, 100000, "x"))
    s.insert_whale_signal((100, "0xA", "庄#1", "WIF", "OPEN", "long", 1e5, 1, 5, 1))
    out = ConfluenceAggregator(s, min_sources=2).scan(now_ms=200)
    assert len(out) == 1 and out[0].n_sources == 3
    assert out[0].score > 0.8 and s.count("confluence_signals") == 1
    s.close()


def test_window_excludes_old():
    s = _store()
    s.insert_consensus((100, "SOL", "long", 3, 0, 1e6, 1.0, "庄"))
    s.insert_divergence((100, "SOL", "bullish", 0.3, 0.0001, 0.03, 100000, "x"))
    # 窗口只看最近 50ms，100 太旧 → 无
    out = ConfluenceAggregator(s, window_ms=50, min_sources=2).scan(now_ms=1000)
    assert out == []
    s.close()


def test_flow_prediction_as_confluence_source():
    """前瞻预测作为独立共振源：flow_predictions + consensus 同向 → 超级信号，sources 含 '前瞻'。"""
    s = _store()
    # 前瞻 long + 共识 long → 2 源同向，应出超级信号
    s.insert_flow_prediction((100, "PEPE", "long", 0.70, 4000.0, 1200.0, 0.30))
    s.insert_consensus((100, "PEPE", "long", 3, 0, 1e6, 1.0, "庄"))
    out = ConfluenceAggregator(s, min_sources=2).scan(now_ms=200)
    assert len(out) == 1
    sig = out[0]
    assert sig.coin == "PEPE"
    assert sig.direction == "long"
    assert sig.n_sources == 2
    assert "前瞻" in sig.sources
    assert "共识" in sig.sources
    s.close()


def test_flow_prediction_conflict_no_signal():
    """前瞻 long + 跟庄 short 矛盾 → 不出超级信号。"""
    s = _store()
    s.insert_flow_prediction((100, "DOGE", "long",  0.60, 3000.0, 900.0, 0.25))
    s.insert_whale_signal((100, "0xA", "庄#1", "DOGE", "OPEN", "short", 1e5, 0.1, 5, 1))
    out = ConfluenceAggregator(s, min_sources=2).scan(now_ms=200)
    # 1 源 long、1 源 short → 矛盾，不出
    assert out == []
    s.close()


def test_flow_prediction_three_source_with_forecast():
    """前瞻 + 共识 + 跟庄 三源同向 → 高分超级信号，sources 含 '前瞻'。"""
    s = _store()
    s.insert_flow_prediction((100, "WIF", "long", 0.75, 5000.0, 1500.0, 0.40))
    s.insert_consensus((100, "WIF", "long", 4, 0, 2e6, 1.0, "庄"))
    s.insert_whale_signal((100, "0xB", "庄#2", "WIF", "OPEN", "long", 2e5, 2.0, 10, 1))
    out = ConfluenceAggregator(s, min_sources=2).scan(now_ms=200)
    assert len(out) == 1
    sig = out[0]
    assert sig.n_sources == 3
    assert "前瞻" in sig.sources
    assert sig.score > 0.8
    s.close()


class _FakeEfficacy:
    """stub efficacy：共识→1.5、跟庄→0.4、其余→1.0。"""

    def weight_of(self, kind: str) -> float:
        return {"共识": 1.5, "跟庄": 0.4}.get(kind, 1.0)

    def is_contrarian(self, kind: str) -> bool:
        return False


def test_efficacy_none_backward_compat():
    """efficacy=None 时 score == 原公式值(向后兼容)。"""
    s = _store()
    s.insert_consensus((100, "ETH", "long", 3, 0, 1e6, 1.0, "庄"))
    s.insert_divergence((100, "ETH", "bullish", 0.3, 0.0001, 0.03, 100000, "x"))
    agg = ConfluenceAggregator(s, min_sources=2)
    # efficacy 默认 None → score 按纯数量公式 = min(0.5 + 0.2*2 - 0.15*0, 1.0) = 0.9
    out = agg.scan(now_ms=200)
    assert len(out) == 1
    expected = min(0.5 + 0.2 * 2 - 0.15 * 0, 1.0)
    assert abs(out[0].score - expected) < 1e-9
    s.close()


def test_set_efficacy_injects_correctly():
    """set_efficacy 注入后 agg.efficacy 不再为 None。"""
    s = _store()
    agg = ConfluenceAggregator(s, min_sources=2)
    assert agg.efficacy is None
    agg.set_efficacy(_FakeEfficacy())
    assert agg.efficacy is not None
    s.close()


def test_efficacy_high_weight_source_scores_higher():
    """高权重源(共识→1.5)使 score 高于纯数量计算。"""
    s = _store()
    # 共识(weight=1.5) + 背离(weight=1.0) 同向 long
    s.insert_consensus((100, "AVAX", "long", 3, 0, 1e6, 1.0, "庄"))
    s.insert_divergence((100, "AVAX", "bullish", 0.3, 0.0001, 0.03, 100000, "x"))
    agg_no_eff = ConfluenceAggregator(s, min_sources=2)
    out_no = agg_no_eff.scan(now_ms=200)
    baseline_score = out_no[0].score  # 纯数量 n_agree=2 → 0.9

    s2 = _store()
    s2.insert_consensus((100, "AVAX", "long", 3, 0, 1e6, 1.0, "庄"))
    s2.insert_divergence((100, "AVAX", "bullish", 0.3, 0.0001, 0.03, 100000, "x"))
    agg_eff = ConfluenceAggregator(s2, min_sources=2)
    agg_eff.set_efficacy(_FakeEfficacy())
    out_eff = agg_eff.scan(now_ms=200)
    # weighted_agree = 1.5(共识)+1.0(背离)=2.5 → score = min(0.5+0.2*2.5, 1.0) = 1.0
    assert out_eff[0].score > baseline_score
    # n_sources/sources 仍报原始数量，展示不变
    assert out_eff[0].n_sources == 2
    s.close()
    s2.close()


def test_efficacy_low_weight_source_scores_lower():
    """低权重源(跟庄→0.4)使 score 低于纯数量计算。"""
    s = _store()
    # 跟庄(weight=0.4) + 背离(weight=1.0) 同向 short
    s.insert_whale_signal((100, "0xA", "庄#1", "LINK", "OPEN", "short", 1e5, 1, 5, 1))
    s.insert_divergence((100, "LINK", "bearish", 0.3, 0.0001, 0.03, -100000, "x"))
    agg_no_eff = ConfluenceAggregator(s, min_sources=2)
    out_no = agg_no_eff.scan(now_ms=200)
    baseline_score = out_no[0].score  # 纯数量 n_agree=2 → 0.9

    s2 = _store()
    s2.insert_whale_signal((100, "0xA", "庄#1", "LINK", "OPEN", "short", 1e5, 1, 5, 1))
    s2.insert_divergence((100, "LINK", "bearish", 0.3, 0.0001, 0.03, -100000, "x"))
    agg_eff = ConfluenceAggregator(s2, min_sources=2)
    agg_eff.set_efficacy(_FakeEfficacy())
    out_eff = agg_eff.scan(now_ms=200)
    # weighted_agree = 0.4(跟庄)+1.0(背离)=1.4 → score = min(0.5+0.2*1.4, 1.0) = 0.78
    assert out_eff[0].score < baseline_score
    s.close()
    s2.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
