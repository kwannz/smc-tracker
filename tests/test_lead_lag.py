"""lead_lag / cluster_leader 单测：确定性，不联网，用合成 hl_meme_trades。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.address_correlation import AddressCorrelation
from smc_tracker.storage import Store


def _trade(coin: str, side: str, buyer: str, seller: str, taker: str, t: int) -> tuple:
    # (coin,px,sz,notional,taker_side,buyer,seller,taker,hash,tid,time_ms)
    return (coin, 1.0, 1.0, 100.0, side, buyer, seller, taker, "h", t, t)


def _make_store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "s.db")


# ---- 场景1：A 始终早于 B（跨多币多次）→ A 是 leader ----

def test_lead_lag_a_leads_b():
    """A 在每次同币同向建仓都早于 B 5 秒 → A.score > 0 > B.score，A 排第一。"""
    s = _make_store()
    rows = []
    # 在 kPEPE 和 kWIF 两币上各做 3 轮，每轮 A 先 5 秒
    for k in range(3):
        base = k * 200_000     # 轮次间隔 200s，超过不应期(window=60s)
        for coin in ("kPEPE", "kWIF"):
            rows.append(_trade(coin, "B", "0xA", "0xM", "0xA", base))
            rows.append(_trade(coin, "B", "0xB", "0xM", "0xB", base + 5_000))  # B 晚 5s
    s.insert_hl_meme_trades(rows)
    ac = AddressCorrelation(s)
    ll = ac.lead_lag(["0xA", "0xB"], since_ms=0, window_sec=60)
    # 应有结果
    assert ll, "lead_lag 不应为空"
    # A 应排第一，score > 0
    assert ll[0][0] == "0xA", f"第一名应是 0xA，实际: {ll[0]}"
    assert ll[0][1] > 0, f"A 的 score 应 > 0，实际: {ll[0][1]}"
    # B 的 score 应 < A
    b_score = next(x[1] for x in ll if x[0] == "0xB")
    assert b_score < ll[0][1], f"B 的 score 应 < A，B={b_score}, A={ll[0][1]}"
    s.close()


def test_cluster_leader_returns_a():
    """cluster_leader([A,B]) → A（A score>0）。"""
    s = _make_store()
    rows = []
    for k in range(3):
        base = k * 200_000
        for coin in ("kPEPE", "kWIF"):
            rows.append(_trade(coin, "B", "0xA", "0xM", "0xA", base))
            rows.append(_trade(coin, "B", "0xB", "0xM", "0xB", base + 5_000))
    s.insert_hl_meme_trades(rows)
    ac = AddressCorrelation(s)
    result = ac.cluster_leader(["0xA", "0xB"], since_ms=0, window_sec=60)
    assert result is not None, "应识别出 leader"
    leader, score = result
    assert leader == "0xA", f"leader 应是 0xA，实际: {leader}"
    assert score > 0, f"score 应 > 0，实际: {score}"
    s.close()


# ---- 场景2：A/B 交替领先（无明显先后）→ cluster_leader 返回 None ----

def test_cluster_leader_no_clear_leader():
    """A/B 交替领先：A 在偶数轮先，B 在奇数轮先 → score 相近 / 领先关系对称 → 返回 None。"""
    s = _make_store()
    rows = []
    # 4 轮，偶数轮 A 先，奇数轮 B 先，各在同一币上
    for k in range(4):
        base = k * 200_000
        if k % 2 == 0:
            rows.append(_trade("kPEPE", "B", "0xA", "0xM", "0xA", base))
            rows.append(_trade("kPEPE", "B", "0xB", "0xM", "0xB", base + 5_000))
        else:
            rows.append(_trade("kPEPE", "B", "0xB", "0xM", "0xB", base))
            rows.append(_trade("kPEPE", "B", "0xA", "0xM", "0xA", base + 5_000))
    s.insert_hl_meme_trades(rows)
    ac = AddressCorrelation(s)
    result = ac.cluster_leader(["0xA", "0xB"], since_ms=0, window_sec=60)
    # 交替领先：净领先 score == 0，应返回 None
    assert result is None, f"交替领先应无明显 leader，实际: {result}"
    s.close()


# ---- 场景3：不应期——同对短时间多次重叠只记一次 ----

def test_refractory_period_prevents_inflation():
    """同对在 < window_sec 内的多次重叠只记一次：leads 不膨胀。"""
    s = _make_store()
    rows = []
    w_ms = 60_000   # window = 60s
    # 在 w 内 A → B 出现 5 次（间隔 5s），不应期应只记 1 次
    for i in range(5):
        rows.append(_trade("kPEPE", "B", "0xA", "0xM", "0xA", i * 5_000))
        rows.append(_trade("kPEPE", "B", "0xB", "0xM", "0xB", i * 5_000 + 1_000))
    s.insert_hl_meme_trades(rows)
    ac = AddressCorrelation(s)
    ll = ac.lead_lag(["0xA", "0xB"], since_ms=0, window_sec=60)
    a_leads = next(x[2] for x in ll if x[0] == "0xA")
    # 不应期：5 次重叠在 60s 内，只应记 1 次领先事件
    assert a_leads == 1, f"不应期保护失败，A.leads={a_leads}，应=1"
    s.close()


def test_refractory_not_block_separate_windows():
    """跨 window 的两次领先（间隔 > window_sec）应各自独立记录（不应期不过度屏蔽）。"""
    s = _make_store()
    rows = []
    # 第一次：t=0 A → t=5s B（同 coin/side）
    rows.append(_trade("kPEPE", "B", "0xA", "0xM", "0xA", 0))
    rows.append(_trade("kPEPE", "B", "0xB", "0xM", "0xB", 5_000))
    # 第二次：间隔 > 60s（t=70s），全新独立事件
    rows.append(_trade("kPEPE", "B", "0xA", "0xM", "0xA", 70_000))
    rows.append(_trade("kPEPE", "B", "0xB", "0xM", "0xB", 75_000))
    s.insert_hl_meme_trades(rows)
    ac = AddressCorrelation(s)
    ll = ac.lead_lag(["0xA", "0xB"], since_ms=0, window_sec=60)
    a_leads = next(x[2] for x in ll if x[0] == "0xA")
    # 两次独立事件应各记一次 → leads = 2
    assert a_leads == 2, f"两次独立事件应记 2 次，实际 A.leads={a_leads}"
    s.close()


# ---- 场景4：边界——空/单地址 ----

def test_lead_lag_empty_addresses():
    """空地址列表 → 返回 []，不抛异常。"""
    s = _make_store()
    ac = AddressCorrelation(s)
    result = ac.lead_lag([], since_ms=0)
    assert result == [], f"空地址应返回 []，实际: {result}"
    s.close()


def test_lead_lag_single_address():
    """单地址列表 → 无配对，返回 [(addr, 0, 0, 0)] 或 []，不抛异常。"""
    s = _make_store()
    rows = [_trade("kPEPE", "B", "0xA", "0xM", "0xA", 0)]
    s.insert_hl_meme_trades(rows)
    ac = AddressCorrelation(s)
    result = ac.lead_lag(["0xA"], since_ms=0)
    # 单地址无法领先任何人，score=leads=lags=0
    assert len(result) <= 1, f"单地址结果应 ≤ 1 条，实际: {result}"
    if result:
        addr, score, leads, lags = result[0]
        assert score == 0 and leads == 0 and lags == 0, (
            f"单地址 score/leads/lags 应全 0，实际: {result[0]}")
    s.close()


def test_cluster_leader_empty_members():
    """cluster_leader([]) → None，不抛异常。"""
    s = _make_store()
    ac = AddressCorrelation(s)
    result = ac.cluster_leader([], since_ms=0)
    assert result is None
    s.close()


def test_cluster_leader_no_trades():
    """无成交数据 → cluster_leader → None。"""
    s = _make_store()
    ac = AddressCorrelation(s)
    result = ac.cluster_leader(["0xA", "0xB"], since_ms=0)
    assert result is None
    s.close()


# ---- 场景5：三地址，明确线性链 A→B→C ----

def test_lead_lag_three_address_chain():
    """A 始终最先，B 其次，C 最后 → score: A>B>C。"""
    s = _make_store()
    rows = []
    for k in range(4):
        base = k * 200_000
        rows.append(_trade("kPEPE", "B", "0xA", "0xM", "0xA", base))
        rows.append(_trade("kPEPE", "B", "0xB", "0xM", "0xB", base + 5_000))
        rows.append(_trade("kPEPE", "B", "0xC", "0xM", "0xC", base + 10_000))
    s.insert_hl_meme_trades(rows)
    ac = AddressCorrelation(s)
    ll = ac.lead_lag(["0xA", "0xB", "0xC"], since_ms=0, window_sec=60)
    scores = {addr: score for addr, score, _, _ in ll}
    assert scores["0xA"] > scores["0xB"] > scores["0xC"], (
        f"A>B>C score 链期望，实际: {scores}")
    # cluster_leader 应返回 A
    result = ac.cluster_leader(["0xA", "0xB", "0xC"], since_ms=0, window_sec=60)
    assert result is not None
    assert result[0] == "0xA", f"leader 应是 0xA，实际: {result}"
    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
