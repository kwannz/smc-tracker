"""聪明钱地址分析器测试。

覆盖：
- analyze_fills：胜率 / 已实现 pnl / 吃单比 / 偏好币 / 24h 活跃。
- smart_money_score：高分场景。
- Store.upsert_address_profile + top_profiles：临时库 roundtrip。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Fill, Side  # noqa: E402
from smc_tracker.monitor.address_analyzer import (  # noqa: E402
    analyze_fills,
    is_perp_active,
    smart_money_score,
)
from smc_tracker.storage import Store  # noqa: E402

NOW_MS = 1_700_000_000_000


def _mk(coin: str, side: Side, px: float, sz: float, *, closed_pnl: float = 0.0,
        crossed: bool = True, time_ms: int = NOW_MS) -> Fill:
    """构造合成 Fill。"""
    return Fill(
        coin=coin, side=side, px=px, sz=sz, time_ms=time_ms,
        start_position=0.0, dir="Open Long", closed_pnl=closed_pnl,
        hash="0xabc", oid=1, crossed=crossed,
    )


# ------------------------- is_perp_active -------------------------

def test_is_perp_active():
    """永续可追踪性：纯现货/休眠巨鲸(0持仓0成交)→False；有持仓或成交→True。"""
    assert is_perp_active(0, 0) is False        # 疑纯现货/休眠：无永续可追
    assert is_perp_active(2, 0) is True         # 有持仓
    assert is_perp_active(0, 5) is True         # 有近期成交
    assert is_perp_active(3, 10) is True


# ------------------------- analyze_fills -------------------------

def test_analyze_fills_empty():
    r = analyze_fills([], NOW_MS)
    assert r["n_trades"] == 0
    assert r["win_rate"] == 0.0
    assert r["realized_pnl"] == 0.0
    assert r["fav_coins"] == []


def test_analyze_fills_metrics():
    day = 86_400_000
    fills = [
        # 3 笔平仓：2 胜 1 负 -> 胜率 2/3
        _mk("BTC", Side.SELL, px=100.0, sz=1.0, closed_pnl=50.0, crossed=True),   # 胜
        _mk("BTC", Side.SELL, px=100.0, sz=1.0, closed_pnl=30.0, crossed=False),  # 胜
        _mk("ETH", Side.SELL, px=10.0, sz=1.0, closed_pnl=-20.0, crossed=True),   # 负
        # 1 笔开仓（closed_pnl=0，不计入胜率分母）
        _mk("BTC", Side.BUY, px=100.0, sz=2.0, closed_pnl=0.0, crossed=True),
        # 1 笔很久以前的成交（不计入 24h）
        _mk("SOL", Side.BUY, px=5.0, sz=1.0, closed_pnl=0.0, crossed=False,
            time_ms=NOW_MS - 2 * day),
    ]
    r = analyze_fills(fills, NOW_MS)

    assert r["n_trades"] == 5
    assert r["n_closed"] == 3
    # 胜率 = 2 胜 / 3 平仓
    assert abs(r["win_rate"] - 2 / 3) < 1e-9
    # 已实现 = 50 + 30 - 20 = 60
    assert r["realized_pnl"] == 60.0
    # 总成交额：BTC 100+100+200=400, ETH 10, SOL 5 -> 415
    assert r["volume_usd"] == 415.0
    # 吃单比：3 笔 crossed / 5 笔 = 0.6
    assert abs(r["taker_ratio"] - 0.6) < 1e-9
    # 24h 内：除 SOL(2 天前) 外的 4 笔
    assert r["recent_24h"] == 4
    # 偏好币按成交额：BTC(400) > ETH(10) > SOL(5)
    assert r["fav_coins"][0] == "BTC"
    assert set(r["fav_coins"]) == {"BTC", "ETH", "SOL"}


def test_analyze_fills_all_wins():
    fills = [
        _mk("AAA", Side.SELL, px=1.0, sz=1.0, closed_pnl=10.0),
        _mk("AAA", Side.SELL, px=1.0, sz=1.0, closed_pnl=5.0),
    ]
    r = analyze_fills(fills, NOW_MS)
    assert r["win_rate"] == 1.0
    assert r["realized_pnl"] == 15.0


# ------------------------- smart_money_score -------------------------

def test_smart_money_score_high():
    """全维度拉满应满分（盈利为主 + 跨窗一致 + 高 ROI）。"""
    profile = {
        "alltime_pnl": 60_000_000.0,  # 超封顶 -> 28
        "month_pnl": 12_000_000.0,   # 超封顶 -> 18
        "week_pnl": 1_000_000.0,     # 三窗皆正 -> 一致性 16
        "realized_pnl": 100_000.0,   # 盈利 -> 8
        "account_value": 20_000_000.0,  # ROI=0.6 封顶 14 + 规模 8
        "win_rate": 0.7,             # 胜率封顶 -> 8
    }
    assert smart_money_score(profile) == 100.0


def test_smart_money_score_low_winrate_high_pnl():
    """聪明钱典型画像：胜率为 0 但持续强盈利，仍应高分。"""
    profile = {
        "alltime_pnl": 50_000_000.0,  # 封顶 -> 28
        "month_pnl": 10_000_000.0,   # 封顶 -> 18
        "week_pnl": 2_000_000.0,     # 三窗皆正 -> 16
        "realized_pnl": 100_000.0,   # 盈利 -> 8
        "account_value": 10_000_000.0,  # ROI=1.0 封顶 14 + 规模 8
        "win_rate": 0.0,             # 零胜率 -> 0
    }
    # 28+18+16+14+8+8 = 92，胜率为 0 仍达 92
    assert smart_money_score(profile) == 92.0


def test_smart_money_score_low():
    """亏损 + 低胜率 + 小账户应低分。"""
    profile = {
        "win_rate": 0.0,
        "realized_pnl": -5000.0,
        "alltime_pnl": 0.0,
        "month_pnl": 0.0,
        "account_value": 0.0,
    }
    assert smart_money_score(profile) == 0.0


def test_smart_money_score_partial():
    """部分维度：胜率一半(4) + 已实现盈利(8)。"""
    profile = {
        "win_rate": 0.35,            # 0.35/0.7*8 = 4
        "realized_pnl": 1.0,         # +8
        "alltime_pnl": 0.0,
        "month_pnl": 0.0,
        "account_value": 0.0,
    }
    assert smart_money_score(profile) == 12.0


def test_score_consistency_beats_luck():
    """跨窗一致(三窗皆正) 应高于一次性运气(周亏)。"""
    base = {"alltime_pnl": 5_000_000.0, "month_pnl": 2_000_000.0,
            "account_value": 10_000_000.0, "realized_pnl": 0.0, "win_rate": 0.0}
    consistent = dict(base, week_pnl=1_000_000.0)    # 一致性 16
    lucky = dict(base, week_pnl=-1_000_000.0)        # 仅月+全期正 -> 7
    sc, sl = smart_money_score(consistent), smart_money_score(lucky)
    assert sc > sl
    assert round(sc - sl, 1) == 9.0                  # 16 - 7


def test_score_roi_efficiency():
    """同等近月盈利，小账户(高 ROI/资本效率) 应得分更高。"""
    small = {"alltime_pnl": 0.0, "month_pnl": 2_000_000.0, "week_pnl": 0.0,
             "account_value": 4_000_000.0, "realized_pnl": 0.0, "win_rate": 0.0}   # ROI=0.5
    big = dict(small, account_value=40_000_000.0)                                   # ROI=0.05
    assert smart_money_score(small) > smart_money_score(big)


def test_score_churn_discount():
    """做市商/刷量：高成交额但方向盈亏≈0 → 整体打折(×0.85)。"""
    mm = {"alltime_pnl": 5_000_000.0, "month_pnl": 1_000_000.0, "week_pnl": 1_000_000.0,
          "account_value": 10_000_000.0, "realized_pnl": 500.0, "win_rate": 0.5,
          "volume_usd": 100_000_000.0}               # rp/vol=5e-6 < 0.1% → churn
    clean = dict(mm, volume_usd=100_000.0)           # 成交额小 → 不触发判别
    s_mm, s_clean = smart_money_score(mm), smart_money_score(clean)
    assert s_mm < s_clean
    assert round(s_mm / s_clean, 2) == 0.85


# ------------------------- Store roundtrip -------------------------

def test_upsert_and_top_profiles(tmp_path):
    store = Store(tmp_path / "t.db")
    try:
        p1 = {
            "address": "0xaaa", "score": 88.5, "account_value": 1_000_000.0,
            "alltime_pnl": 5_000_000.0, "month_pnl": 200_000.0, "win_rate": 0.65,
            "realized_pnl": 12_345.0, "n_trades": 40, "net_bias": "多",
            "fav_coins": ["BTC", "ETH"], "ts": NOW_MS,
        }
        p2 = {
            "address": "0xbbb", "score": 42.0, "account_value": 50_000.0,
            "alltime_pnl": 0.0, "month_pnl": 0.0, "win_rate": 0.5,
            "realized_pnl": -100.0, "n_trades": 5, "net_bias": "空",
            "fav_coins": ["SOL"], "ts": NOW_MS,
        }
        store.upsert_address_profile(p1)
        store.upsert_address_profile(p2)

        rows = store.top_profiles(limit=10)
        # 按 score DESC：0xaaa 在前
        assert len(rows) == 2
        assert rows[0][0] == "0xaaa"
        assert rows[0][1] == 88.5
        assert rows[1][0] == "0xbbb"
        # fav_coins 以逗号拼接存储
        assert rows[0][9] == "BTC,ETH"
        assert rows[1][9] == "SOL"

        # 同地址 upsert 应更新而非新增
        p1_upd = dict(p1, score=10.0, n_trades=99, fav_coins=["DOGE"])
        store.upsert_address_profile(p1_upd)
        rows2 = store.top_profiles(limit=10)
        assert len(rows2) == 2          # 未新增行
        by_addr = {r[0]: r for r in rows2}
        assert by_addr["0xaaa"][1] == 10.0   # score 已更新
        assert by_addr["0xaaa"][7] == 99     # n_trades 已更新
        assert by_addr["0xaaa"][9] == "DOGE"
        # limit 生效
        assert len(store.top_profiles(limit=1)) == 1
    finally:
        store.close()
