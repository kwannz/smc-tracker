"""B3 TDD 测试 — smart_money_score 魔数外置 + 样本守卫。

覆盖 spec B3.3:
1. test_score_cfg_default_equals_legacy   — 旧权重输出快照(回归 golden)
2. test_winrate_small_sample_guarded      — Wilson 守卫: 小样本降分
3. test_winrate_lower_monotone            — analyze_fills 的 win_rate_lower ≤ win_rate 且 n↑→逼近
4. test_cfg_override_changes_weight       — cfg 注入改权重可见效果
5. test_zero_closed_safe                  — n_closed=0 不崩, 胜率项=0
6. test_survivorship_caveat_present       — 小样本 profile → score_caveats 含诚实文案; fmt 含 ⚠
7. test_wilson_n0_guard                   — wilson_interval(0, 0) 不除零 → (0.0, 1.0)
8. 回归: 原有高分/低分/churn 场景继续通过(golden snapshot)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Fill, Side
from smc_tracker.monitor.address_analyzer import (
    analyze_fills,
    smart_money_score,
    AddressAnalyzer,
)
from smc_tracker.config import SmartScoreCfg
from smc_tracker.signals.efficacy import wilson_interval

NOW_MS = 1_700_000_000_000


def _mk(coin: str, side: Side, px: float, sz: float, *, closed_pnl: float = 0.0,
        crossed: bool = True, time_ms: int = NOW_MS) -> Fill:
    return Fill(
        coin=coin, side=side, px=px, sz=sz, time_ms=time_ms,
        start_position=0.0, dir="Open Long", closed_pnl=closed_pnl,
        hash="0xabc", oid=1, crossed=crossed,
    )


# ─────────────────────────────────────────────
# 1. 回归 golden snapshot — 证明重构不静默改分
# ─────────────────────────────────────────────

def test_score_cfg_default_equals_legacy_high():
    """高分场景 golden snapshot: 默认 cfg 结果 == 旧魔数 100.0。"""
    profile = {
        "alltime_pnl": 60_000_000.0, "month_pnl": 12_000_000.0, "week_pnl": 1_000_000.0,
        "realized_pnl": 100_000.0, "account_value": 20_000_000.0, "win_rate": 0.7,
    }
    # 不传 cfg → 默认值 = 旧魔数等价
    assert smart_money_score(profile) == 100.0


def test_score_cfg_default_equals_legacy_low_winrate():
    """低胜率高盈利 golden: 92.0。"""
    profile = {
        "alltime_pnl": 50_000_000.0, "month_pnl": 10_000_000.0, "week_pnl": 2_000_000.0,
        "realized_pnl": 100_000.0, "account_value": 10_000_000.0, "win_rate": 0.0,
    }
    assert smart_money_score(profile) == 92.0


def test_score_cfg_default_equals_legacy_partial():
    """部分维度 golden: 胜率 0.35 + 已实现盈利 → 12.0。"""
    profile = {
        "win_rate": 0.35, "realized_pnl": 1.0,
        "alltime_pnl": 0.0, "month_pnl": 0.0, "account_value": 0.0,
    }
    assert smart_money_score(profile) == 12.0


def test_score_cfg_default_equals_legacy_low():
    """全负 golden: 0.0。"""
    profile = {
        "win_rate": 0.0, "realized_pnl": -5000.0,
        "alltime_pnl": 0.0, "month_pnl": 0.0, "account_value": 0.0,
    }
    assert smart_money_score(profile) == 0.0


def test_score_cfg_default_equals_legacy_churn():
    """churn 折扣 golden: mm/clean 比值 ≈ 0.85。"""
    mm = {
        "alltime_pnl": 5_000_000.0, "month_pnl": 1_000_000.0, "week_pnl": 1_000_000.0,
        "account_value": 10_000_000.0, "realized_pnl": 500.0, "win_rate": 0.5,
        "volume_usd": 100_000_000.0,
    }
    clean = dict(mm, volume_usd=100_000.0)
    s_mm = smart_money_score(mm)
    s_clean = smart_money_score(clean)
    assert s_mm < s_clean
    assert round(s_mm / s_clean, 2) == 0.85


# ─────────────────────────────────────────────
# 2. Wilson 守卫: 小样本降分
# ─────────────────────────────────────────────

def test_winrate_small_sample_guarded():
    """两个裸胜率同为 67%, 小样本(n=3) vs 大样本(n=300).
    analyze_fills 后的 win_rate_lower 注入 profile → 大样本得分明显高于小样本。
    """
    # 小样本: 3 单 2 胜 = 67%
    fills_small = [
        _mk("BTC", Side.SELL, px=1.0, sz=1.0, closed_pnl=10.0),
        _mk("BTC", Side.SELL, px=1.0, sz=1.0, closed_pnl=10.0),
        _mk("BTC", Side.SELL, px=1.0, sz=1.0, closed_pnl=-10.0),
    ]
    beh_small = analyze_fills(fills_small, NOW_MS)
    # 大样本: 300 单 200 胜 = 67% (用批量构造)
    fills_big = (
        [_mk("BTC", Side.SELL, px=1.0, sz=1.0, closed_pnl=10.0)] * 200 +
        [_mk("BTC", Side.SELL, px=1.0, sz=1.0, closed_pnl=-10.0)] * 100
    )
    beh_big = analyze_fills(fills_big, NOW_MS)

    # 裸胜率相同
    assert abs(beh_small["win_rate"] - 2 / 3) < 1e-9
    assert abs(beh_big["win_rate"] - 2 / 3) < 1e-9

    # win_rate_lower 应存在且小样本更低
    assert "win_rate_lower" in beh_small
    assert "win_rate_lower" in beh_big
    assert beh_small["win_rate_lower"] < beh_big["win_rate_lower"]

    # 注入 profile → 计分后大样本更高
    base_profile = {
        "alltime_pnl": 0.0, "month_pnl": 0.0, "week_pnl": 0.0,
        "account_value": 0.0, "realized_pnl": 0.0, "volume_usd": 0.0,
    }
    profile_small = {**base_profile, **beh_small}
    profile_big = {**base_profile, **beh_big}
    score_small = smart_money_score(profile_small)
    score_big = smart_money_score(profile_big)
    # 大样本得分明显更高
    assert score_big > score_small + 1.0, (
        f"大样本({score_big:.1f}) 应 >> 小样本({score_small:.1f}), Wilson 守卫未生效"
    )


# ─────────────────────────────────────────────
# 3. win_rate_lower 单调性
# ─────────────────────────────────────────────

def test_winrate_lower_monotone():
    """analyze_fills 的 win_rate_lower ≤ win_rate; n 越大越逼近裸胜率。"""
    results = []
    for n in [3, 10, 30, 100, 300]:
        wins = round(n * 2 / 3)
        fills = (
            [_mk("BTC", Side.SELL, px=1.0, sz=1.0, closed_pnl=10.0)] * wins +
            [_mk("BTC", Side.SELL, px=1.0, sz=1.0, closed_pnl=-10.0)] * (n - wins)
        )
        beh = analyze_fills(fills, NOW_MS)
        results.append((n, beh["win_rate"], beh["win_rate_lower"]))

    for n, wr, wrl in results:
        assert wrl <= wr + 1e-9, f"n={n}: lower={wrl:.4f} > raw={wr:.4f}"

    # n 越大, lower 越逼近 raw (差距收窄)
    gaps = [wr - wrl for _, wr, wrl in results]
    for i in range(len(gaps) - 1):
        assert gaps[i] >= gaps[i + 1] - 1e-9, (
            f"n={results[i][0]}→{results[i+1][0]}: gap 应单调缩小 ({gaps[i]:.4f}→{gaps[i+1]:.4f})"
        )


# ─────────────────────────────────────────────
# 4. cfg 注入改权重
# ─────────────────────────────────────────────

def test_cfg_override_changes_weight():
    """传 cfg.w_alltime=0 → 全期 PnL 项归零, 分数可预测下降。"""
    profile = {
        "alltime_pnl": 60_000_000.0, "month_pnl": 12_000_000.0, "week_pnl": 1_000_000.0,
        "realized_pnl": 100_000.0, "account_value": 20_000_000.0, "win_rate": 0.7,
    }
    default_score = smart_money_score(profile)                   # 使用默认 cfg

    # w_alltime=0 → 全期 PnL 不计分 (原本 +28)
    cfg_no_alltime = SmartScoreCfg(w_alltime=0.0)
    score_no_alltime = smart_money_score(profile, cfg=cfg_no_alltime)

    assert score_no_alltime < default_score
    # 差值应接近 28 (全期 PnL 项贡献)
    assert abs(default_score - score_no_alltime - 28.0) < 0.5, (
        f"差值={default_score - score_no_alltime:.1f}, 预期≈28"
    )


def test_cfg_override_winrate_cap():
    """调低 cap_winrate=0.5 → 胜率超 0.5 的部分被截断。"""
    profile = {
        "win_rate": 0.8,  # 超出新 cap=0.5
        "realized_pnl": 0.0, "alltime_pnl": 0.0, "month_pnl": 0.0, "account_value": 0.0,
    }
    default_score = smart_money_score(profile)   # 0.7 封顶 → 8分

    cfg_low_cap = SmartScoreCfg(cap_winrate=0.5)
    score_low_cap = smart_money_score(profile, cfg=cfg_low_cap)

    # 两者都满封顶, score 应相同 (win_rate=0.8 超出两个 cap)
    # 但 0.8/0.7*8=8 vs 0.8/0.5*8 → 都封顶=8, 所以应相等
    assert score_low_cap == default_score


# ─────────────────────────────────────────────
# 5. n_closed=0 不崩
# ─────────────────────────────────────────────

def test_zero_closed_safe():
    """n_closed=0 → win_rate_lower=0, 不崩, 胜率项=0。"""
    beh = analyze_fills([], NOW_MS)
    assert beh["win_rate"] == 0.0
    assert beh.get("win_rate_lower", 0.0) == 0.0

    profile = {"win_rate_lower": 0.0, "win_rate": 0.0, "realized_pnl": 0.0,
               "alltime_pnl": 0.0, "month_pnl": 0.0, "account_value": 0.0}
    score = smart_money_score(profile)
    assert score == 0.0


def test_zero_closed_analyze_fills():
    """只有开仓(closed_pnl=0)的 fills → n_closed=0, win_rate_lower 存在且 ≥0。"""
    fills = [
        _mk("BTC", Side.BUY, px=100.0, sz=1.0, closed_pnl=0.0),
        _mk("ETH", Side.BUY, px=10.0, sz=1.0, closed_pnl=0.0),
    ]
    beh = analyze_fills(fills, NOW_MS)
    assert beh["n_closed"] == 0
    assert beh["win_rate"] == 0.0
    assert "win_rate_lower" in beh
    assert beh["win_rate_lower"] == 0.0


# ─────────────────────────────────────────────
# 6. 幸存者偏差标注
# ─────────────────────────────────────────────

def test_survivorship_caveat_present_small_sample():
    """小样本(n_closed < min_trades_winrate=20) → score_caveats 非空且含诚实文案。"""
    fills = [
        _mk("BTC", Side.SELL, px=1.0, sz=1.0, closed_pnl=10.0),
        _mk("BTC", Side.SELL, px=1.0, sz=1.0, closed_pnl=-5.0),
    ]
    beh = analyze_fills(fills, NOW_MS)
    profile = {
        "alltime_pnl": 0.0, "month_pnl": 0.0, "week_pnl": 0.0,
        "account_value": 0.0, "realized_pnl": 0.0,
        **beh,
    }
    score, caveats = smart_money_score(profile, return_caveats=True)
    assert len(caveats) > 0, "小样本应产生 caveats"
    joined = " ".join(caveats)
    # 应包含样本相关字样
    assert any(kw in joined for kw in ["样本", "n=", "小样本"]), f"caveats={caveats}"


def test_fmt_contains_warning_on_small_sample():
    """AddressAnalyzer.fmt 在小样本时输出含 ⚠ 标注。"""
    profile = {
        "address": "0x1234567890abcdef",
        "score": 10.0, "account_value": 1000.0, "n_positions": 0,
        "net_bias": "多", "net_long_usd": 500.0, "net_short_usd": 0.0,
        "alltime_pnl": 0.0, "month_pnl": 0.0,
        "n_trades": 2, "win_rate": 0.5, "realized_pnl": 0.0,
        "taker_ratio": 0.5, "recent_24h": 0, "fav_coins": [],
        "n_closed": 2, "win_rate_lower": 0.05,  # 小样本
        "score_caveats": ["⚠样本2单(胜率下界估计)"],
    }
    text = AddressAnalyzer.fmt(profile)
    assert "⚠" in text, f"fmt 输出缺 ⚠ 标注: {text!r}"


# ─────────────────────────────────────────────
# 7. Wilson n=0 守卫
# ─────────────────────────────────────────────

def test_wilson_n0_guard():
    """wilson_interval(0, 0) 不除零 → (0.0, 1.0)。"""
    lo, hi = wilson_interval(0, 0)
    assert lo == 0.0
    assert hi == 1.0


def test_wilson_n0_from_analyze_fills():
    """analyze_fills 空列表 → win_rate_lower=0.0, 不崩。"""
    beh = analyze_fills([], NOW_MS)
    # win_rate_lower 应存在(空列表 n_closed=0 → wilson(0,0)→lo=0)
    assert beh.get("win_rate_lower", None) is not None
    assert beh["win_rate_lower"] == 0.0


# ─────────────────────────────────────────────
# 8. SmartScoreCfg 可构造且字段正确
# ─────────────────────────────────────────────

def test_smart_score_cfg_defaults():
    """SmartScoreCfg 默认值 == 旧魔数对应值。"""
    cfg = SmartScoreCfg()
    assert cfg.w_alltime == 28.0
    assert cfg.w_month == 18.0
    assert cfg.w_consistency_all == 16.0
    assert cfg.w_consistency_part == 7.0
    assert cfg.w_roi == 14.0
    assert cfg.w_realized == 8.0
    assert cfg.w_account == 8.0
    assert cfg.w_winrate == 8.0
    assert cfg.cap_alltime == 50_000_000
    assert cfg.cap_month == 10_000_000
    assert cfg.cap_roi_monthly == 0.5
    assert cfg.cap_account == 10_000_000
    assert cfg.cap_winrate == 0.7
    assert cfg.churn_vol_floor == 1_000_000
    assert cfg.churn_eff_max == 0.001
    assert cfg.churn_penalty == 0.85
    assert cfg.min_trades_winrate == 20
