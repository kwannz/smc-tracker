"""谐波 setup 结构指纹去重单测（确定性）。

QA H3-dedup 修复：用 round(prz_mid,4) 做去重 key 会因浮点微抖每轮变化→去重退化成
每 15min 记一次→自相关虚增样本。改用**结构指纹**(coin,tf,pattern,direction,D_pivot_idx)——
D pivot 是稳定离散下标，setup 生命周期内不变，保证同一 setup 只记一次。
"""
from __future__ import annotations

from smc_tracker.signals.harmonic_dedup import setup_fingerprint, SetupDedup


def test_fingerprint_stable_for_same_setup():
    """同一 setup（含 prz 浮点微抖）→ 指纹相同（指纹不含浮点）。"""
    fp1 = setup_fingerprint("BTC", "4H", "Gartley", "long", d_idx=42)
    fp2 = setup_fingerprint("BTC", "4H", "Gartley", "long", d_idx=42)
    assert fp1 == fp2


def test_fingerprint_differs_on_structure():
    """结构不同（D 下标/方向/形态）→ 指纹不同。"""
    base = setup_fingerprint("BTC", "4H", "Gartley", "long", d_idx=42)
    assert setup_fingerprint("BTC", "4H", "Gartley", "long", d_idx=43) != base
    assert setup_fingerprint("BTC", "4H", "Gartley", "short", d_idx=42) != base
    assert setup_fingerprint("BTC", "4H", "Bat", "long", d_idx=42) != base


def test_dedup_first_record_allowed():
    """首次见到 → 允许记录。"""
    d = SetupDedup(ttl_ms=3_600_000)
    assert d.should_record("fp1", now_ms=1000) is True


def test_dedup_repeat_within_ttl_blocked():
    """TTL 内重复 → 拒绝（防自相关）。"""
    d = SetupDedup(ttl_ms=3_600_000)
    d.should_record("fp1", now_ms=1000)
    assert d.should_record("fp1", now_ms=1000 + 60_000) is False


def test_dedup_after_ttl_allowed_again():
    """超 TTL → 再次允许（setup 重新出现可重记）。"""
    d = SetupDedup(ttl_ms=3_600_000)
    d.should_record("fp1", now_ms=1000)
    assert d.should_record("fp1", now_ms=1000 + 3_700_000) is True


def test_dedup_distinct_fingerprints_independent():
    """不同指纹互不影响。"""
    d = SetupDedup(ttl_ms=3_600_000)
    assert d.should_record("fpA", now_ms=1000) is True
    assert d.should_record("fpB", now_ms=1000) is True
