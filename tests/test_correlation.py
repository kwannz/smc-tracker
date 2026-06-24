"""地址关联性单测：co_movers / counterparties / clusters（合成 hl_meme_trades，无网络）。

注：B2 升级后 clusters/clusters_detailed 加了显著性门(二项 null model)。
小样本合成测试传 CorrelationCfg(min_lift=0.0, max_p=1.0) 等价旧行为（全放行），
保证功能正确性测试不被统计阈值干扰。显著性过滤的独立测试在 test_cooccur_stats.py。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import CorrelationCfg
from smc_tracker.monitor.address_correlation import AddressCorrelation
from smc_tracker.storage import Store

# 全放行配置：等价 B2 前旧行为（无显著性过滤），用于小样本功能测试
_NO_FILTER = CorrelationCfg(min_lift=0.0, max_p=1.0, min_shared=3, min_coins=1)


def _trade(coin, side, buyer, seller, taker, t):
    # (coin,px,sz,notional,taker_side,buyer,seller,taker,hash,tid,time_ms)
    return (coin, 1.0, 1.0, 100.0, side, buyer, seller, taker, "h", t, t)


def _store_with_comovers():
    s = Store(Path(tempfile.mkdtemp()) / "s.db")
    rows = []
    for k in range(4):                       # 4 个不同时间窗
        t = k * 120_000
        rows.append(_trade("kPEPE", "B", "0xA", "0xM", "0xA", t))        # A 主动买
        rows.append(_trade("kPEPE", "B", "0xB", "0xM", "0xB", t + 1000))  # B 同窗主动买
    s.insert_hl_meme_trades(rows)
    return s


def test_co_movers_finds_pair():
    s = _store_with_comovers()
    # co_movers 不受显著性门影响（只按 lift 排序，min_shared 过滤）
    cm = AddressCorrelation(s, cfg=_NO_FILTER).co_movers(since_ms=0, window_sec=60, min_shared=3)
    assert any({a, b} == {"0xA", "0xB"} and c == 4 for a, b, c in cm)
    s.close()


def test_correlated_with():
    s = _store_with_comovers()
    rel = AddressCorrelation(s, cfg=_NO_FILTER).correlated_with("0xA", since_ms=0, min_shared=3)
    assert rel and rel[0][0] == "0xB"
    s.close()


def test_clusters_union():
    s = _store_with_comovers()
    # 传 min_coins=1 + 全放行配置：等价旧行为
    groups = AddressCorrelation(s, cfg=_NO_FILTER).clusters(since_ms=0, min_shared=3, min_coins=1)
    assert any(set(g) == {"0xA", "0xB"} for g in groups)
    s.close()


def test_co_movers_boundary_fix():
    """边界用例：相隔 2s 但跨固定分桶边界，滑窗算法仍应判为协同(旧 t//w 会漏)。"""
    s = Store(Path(tempfile.mkdtemp()) / "s.db")
    w = 60_000
    rows = []
    for k in range(4):                       # A 在桶末、B 在下一桶初，相隔仅 2s
        base = k * 600_000 + (w - 1000)      # 距桶边界 1s
        rows.append(_trade("kPEPE", "B", "0xA", "0xM", "0xA", base))
        rows.append(_trade("kPEPE", "B", "0xB", "0xM", "0xB", base + 2000))  # 跨桶边界
    s.insert_hl_meme_trades(rows)
    cm = AddressCorrelation(s, cfg=_NO_FILTER).co_movers(since_ms=0, window_sec=60, min_shared=3)
    assert any({a, b} == {"0xA", "0xB"} and c == 4 for a, b, c in cm)
    s.close()


def test_single_coin_crowd_filtered_by_min_coins():
    """单币人群(只在一个币上同向) 不应被当作庄家集团：min_coins=2 过滤掉。"""
    s = Store(Path(tempfile.mkdtemp()) / "s.db")
    rows = []
    for k in range(4):
        t = k * 120_000
        for addr in ("0xA", "0xB", "0xC"):   # 三地址同窗买同一个币(像拉盘人群)
            rows.append(_trade("kPEPE", "B", addr, "0xM", addr, t + 1000))
    s.insert_hl_meme_trades(rows)
    ac = AddressCorrelation(s, cfg=_NO_FILTER)
    assert ac.clusters(since_ms=0, min_shared=3, min_coins=1)   # 单币默认能聚成群
    assert ac.clusters(since_ms=0, min_shared=3, min_coins=2) == []  # 要求跨2币→过滤
    s.close()


def test_multi_coin_cluster_detected():
    """真协同：两地址在 2 个不同币上反复同向 → 跨币数=2，min_coins=2 仍识别。"""
    s = Store(Path(tempfile.mkdtemp()) / "s.db")
    rows = []
    for k in range(4):
        t = k * 120_000
        for coin in ("kPEPE", "kWIF"):
            rows.append(_trade(coin, "B", "0xA", "0xM", "0xA", t))
            rows.append(_trade(coin, "B", "0xB", "0xM", "0xB", t + 1000))
    s.insert_hl_meme_trades(rows)
    # 全放行配置（小样本），验证跨币聚合逻辑
    cfg_no_filter = CorrelationCfg(min_lift=0.0, max_p=1.0, min_shared=3, min_coins=2)
    det = AddressCorrelation(s, cfg=cfg_no_filter).clusters_detailed(
        since_ms=0, min_shared=3, min_coins=2)
    assert det and set(det[0]["members"]) == {"0xA", "0xB"}
    assert det[0]["coins"] == 2            # 跨 2 币(硬证据)
    assert det[0]["events"] == 8           # 每币 4 次 × 2 币
    s.close()


def test_counterparties():
    s = Store(Path(tempfile.mkdtemp()) / "s.db")
    rows = [_trade("WIF", "B", "0xX", "0xY", "0xX", i * 1000) for i in range(6)]
    s.insert_hl_meme_trades(rows)
    cp = AddressCorrelation(s).counterparties(since_ms=0, min_count=5)
    assert cp and cp[0][:2] == ("0xX", "0xY") and cp[0][2] == 6
    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
