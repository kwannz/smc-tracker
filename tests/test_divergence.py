"""三源背离信号单测（合成数据，无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals import DivergenceDetector
from smc_tracker.storage import Store


def test_bearish_divergence():
    """多头拥挤(funding>0) + 聪明钱净卖 → 看跌分销背离。"""
    d = DivergenceDetector()
    sig = d.evaluate("kPEPE", funding=0.0003, oi_change_pct=0.03,
                     dex_flow_usd=-150_000, now_ms=1000)
    assert sig is not None and sig.direction == "bearish" and sig.score > 0


def test_bullish_divergence():
    """空头拥挤(funding<0) + 聪明钱净买 → 看涨吸筹背离。"""
    d = DivergenceDetector()
    sig = d.evaluate("WIF", funding=-0.0003, oi_change_pct=0.03,
                     dex_flow_usd=150_000, now_ms=1000)
    assert sig is not None and sig.direction == "bullish"


def test_no_divergence_when_aligned():
    """多头拥挤 + 聪明钱也在买 → 同向，无背离。"""
    d = DivergenceDetector()
    assert d.evaluate("DOGE", funding=0.0003, oi_change_pct=0.03,
                      dex_flow_usd=150_000, now_ms=1000) is None


def test_below_threshold():
    """资金费/流向都很小 → 分数不足。"""
    d = DivergenceDetector()
    assert d.evaluate("BOME", funding=0.00004, oi_change_pct=0.0,
                      dex_flow_usd=-31_000, now_ms=1000) is None


def test_oi_amplifies_score():
    d = DivergenceDetector()
    s_no = d.evaluate("A", funding=0.0003, oi_change_pct=0.0,
                      dex_flow_usd=-150_000, now_ms=1)
    s_oi = d.evaluate("B", funding=0.0003, oi_change_pct=0.05,
                      dex_flow_usd=-150_000, now_ms=1)
    assert s_oi.score > s_no.score


def test_persist():
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    d = DivergenceDetector(store=store)
    d.evaluate("kPEPE", funding=0.0003, oi_change_pct=0.03,
               dex_flow_usd=-150_000, now_ms=1000)
    assert store.count("divergence") == 1
    store.close()


def test_oi_decrease_attenuates_score():
    """OI 下降(去杠杆/拥挤瓦解)时，score 应低于 OI 中性时，不应高估背离强度。"""
    d = DivergenceDetector()
    # OI 中性（不变）
    s_neutral = d.evaluate("C", funding=0.0003, oi_change_pct=0.0,
                           dex_flow_usd=-150_000, now_ms=1)
    # OI 大幅下降（去杠杆）
    s_dec = d.evaluate("D", funding=0.0003, oi_change_pct=-0.05,
                       dex_flow_usd=-150_000, now_ms=1)
    # 去杠杆场景背离强度应被衰减
    assert s_dec is not None, "OI 下降时背离信号应仍触发"
    assert s_dec.score < s_neutral.score, (
        f"OI 下降应衰减 score: dec={s_dec.score:.4f} >= neutral={s_neutral.score:.4f}"
    )


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
