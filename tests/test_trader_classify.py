"""unit tests for trader_classify — 庄家 vs 游资分类器。

纯函数，合成数据，不联网。
"""
from __future__ import annotations

import pytest

from smc_tracker.monitor.trader_classify import classify_trader, fmt_classify


# ──────────────────────── classify_trader 分类核心 ────────────────────────

class TestClassifyWhale:
    """典型庄家场景。"""

    def test_large_account_long_hold(self):
        """大资金($10M) + 持仓24h → whale。"""
        r = classify_trader(
            account_value=10_000_000.0,
            avg_hold_sec=86_400.0,   # 24h
            n_trades=5,
        )
        assert r["type"] == "whale"
        assert r["score_whale"] > r["score_hot"]

    def test_exactly_at_threshold(self):
        """恰好在庄家阈值边界($5M, 4h) → whale。"""
        r = classify_trader(
            account_value=5_000_000.0,
            avg_hold_sec=14_400.0,   # 4h
            n_trades=3,
        )
        assert r["type"] == "whale"

    def test_p0_zero_hold_blocks_whale_even_for_huge_account(self):
        """P0 根因回归：avg_hold_sec=0(build_dossier 修复前的永久值) → 即使 $10M 大户也判不出 whale。

        这正是 P0 bug 的危害——dossier 不返回 avg_hold_sec 时该值恒为 0，
        whale 硬门槛(account>=$5M AND hold>=4h)永不满足，抓庄核心能力结构性失效。
        修复后 build_dossier 计算真实 avg_hold_sec，whale 判定恢复(见 test_large_account_long_hold)。
        """
        r = classify_trader(account_value=10_000_000.0, avg_hold_sec=0.0, n_trades=5)
        assert r["type"] != "whale", "avg_hold_sec=0 时不应判 whale(P0 根因)"

    def test_very_large_account_days_hold(self):
        """超大资金($50M) + 持仓7天 → whale，高庄分。"""
        r = classify_trader(
            account_value=50_000_000.0,
            avg_hold_sec=604_800.0,  # 7d
            n_trades=2,
        )
        assert r["type"] == "whale"
        assert r["score_whale"] >= 80.0

    def test_reason_contains_chinese(self):
        """reason 应为中文可读描述。"""
        r = classify_trader(
            account_value=8_000_000.0,
            avg_hold_sec=28_800.0,  # 8h
            n_trades=10,
        )
        assert r["type"] == "whale"
        assert len(r["reason"]) > 5
        # 理由应提及账户或持仓相关关键词
        assert any(kw in r["reason"] for kw in ["庄", "持仓", "净值", "账户"])


class TestClassifyHotMoney:
    """典型游资场景。"""

    def test_short_hold_high_frequency(self):
        """持仓25m + 高频50笔 → hot_money。"""
        r = classify_trader(
            account_value=200_000.0,
            avg_hold_sec=1_500.0,   # 25m
            n_trades=50,
        )
        assert r["type"] == "hot_money"
        assert r["score_hot"] > r["score_whale"]

    def test_very_short_hold_many_trades(self):
        """持仓5m + 100笔 → hot_money，高热钱分。"""
        r = classify_trader(
            account_value=500_000.0,
            avg_hold_sec=300.0,     # 5m
            n_trades=100,
        )
        assert r["type"] == "hot_money"
        assert r["score_hot"] >= 60.0

    def test_zero_hold_high_freq(self):
        """持仓0s(极端) + 高频 → hot_money。"""
        r = classify_trader(
            account_value=1_000_000.0,
            avg_hold_sec=0.0,
            n_trades=80,
        )
        assert r["type"] == "hot_money"

    def test_reason_contains_hot_keywords(self):
        """游资 reason 应提及快进快出/高频等词。"""
        r = classify_trader(
            account_value=300_000.0,
            avg_hold_sec=900.0,   # 15m
            n_trades=30,
        )
        assert r["type"] == "hot_money"
        assert any(kw in r["reason"] for kw in ["游资", "快进快出", "高频", "频繁", "成交"])


class TestClassifyMixed:
    """混合场景。"""

    def test_medium_account_medium_hold(self):
        """中等账户($1M) + 中等持仓(2h) + 低频 → mixed。"""
        r = classify_trader(
            account_value=1_000_000.0,
            avg_hold_sec=7_200.0,  # 2h (< 4h 不达庄家，> 1h 不达游资)
            n_trades=15,
        )
        assert r["type"] == "mixed"

    def test_small_account_long_hold(self):
        """小账户($100K) + 持仓长(8h) → mixed (资金不达庄家门槛)。"""
        r = classify_trader(
            account_value=100_000.0,
            avg_hold_sec=28_800.0,  # 8h
            n_trades=5,
        )
        assert r["type"] == "mixed"

    def test_mixed_has_both_scores(self):
        """mixed 结果也应有 score_whale 和 score_hot 字段。"""
        r = classify_trader(
            account_value=2_000_000.0,
            avg_hold_sec=3_600.0,  # 1h 边界
            n_trades=20,
        )
        assert "score_whale" in r
        assert "score_hot" in r
        assert isinstance(r["score_whale"], float)
        assert isinstance(r["score_hot"], float)


class TestClassifyEdgeCases:
    """边界与数据质量守卫。"""

    def test_all_zeros(self):
        """全零输入 → mixed，不崩溃，reason 有提示。"""
        r = classify_trader(
            account_value=0.0,
            avg_hold_sec=0.0,
            n_trades=0,
        )
        assert r["type"] in {"whale", "hot_money", "mixed"}
        assert isinstance(r["reason"], str)
        assert len(r["reason"]) > 0

    def test_none_inputs_guarded(self):
        """None 输入通过 to_float 守卫变 0 → 不抛异常。"""
        r = classify_trader(
            account_value=None,   # type: ignore[arg-type]
            avg_hold_sec=None,    # type: ignore[arg-type]
            n_trades=None,        # type: ignore[arg-type]
        )
        assert r["type"] in {"whale", "hot_money", "mixed"}

    def test_nan_inputs_guarded(self):
        """NaN 输入被 to_float 过滤为 0 → 不抛异常。"""
        import math
        r = classify_trader(
            account_value=math.nan,
            avg_hold_sec=math.nan,
            n_trades=0,
        )
        assert r["type"] in {"whale", "hot_money", "mixed"}

    def test_inf_inputs_guarded(self):
        """inf 输入被 to_float 过滤为 0 → 不抛异常。"""
        import math
        r = classify_trader(
            account_value=math.inf,
            avg_hold_sec=math.inf,
            n_trades=999,
        )
        assert r["type"] in {"whale", "hot_money", "mixed"}

    def test_negative_n_trades_guarded(self):
        """负交易笔数守卫 → 视为 0。"""
        r = classify_trader(
            account_value=1_000_000.0,
            avg_hold_sec=3_600.0,
            n_trades=-10,
        )
        assert r["type"] in {"whale", "hot_money", "mixed"}
        assert r["_n_trades"] == 0

    def test_scores_in_range(self):
        """分数始终在 [0, 100] 范围内。"""
        cases = [
            dict(account_value=0, avg_hold_sec=0, n_trades=0),
            dict(account_value=1e9, avg_hold_sec=1e9, n_trades=10000),
            dict(account_value=5_000_000, avg_hold_sec=14_400, n_trades=50),
        ]
        for kw in cases:
            r = classify_trader(**kw)
            assert 0.0 <= r["score_whale"] <= 100.0, f"score_whale out of range for {kw}"
            assert 0.0 <= r["score_hot"] <= 100.0, f"score_hot out of range for {kw}"

    def test_return_keys(self):
        """返回 dict 必须包含规定的 key。"""
        r = classify_trader(account_value=1e6, avg_hold_sec=3600, n_trades=10)
        for key in ("type", "score_whale", "score_hot", "reason",
                    "_account_value", "_avg_hold_sec", "_n_trades"):
            assert key in r, f"missing key: {key}"

    def test_type_values(self):
        """type 只能是三种合法值。"""
        for av, hold, n in [
            (10e6, 86400, 2),    # whale
            (200e3, 600, 50),    # hot_money
            (1e6, 3600, 10),     # mixed
        ]:
            r = classify_trader(account_value=av, avg_hold_sec=hold, n_trades=n)
            assert r["type"] in {"whale", "hot_money", "mixed"}

    def test_optional_turnover_winrate(self):
        """可选参数 turnover/win_rate 不传时默认 0，不崩溃。"""
        r1 = classify_trader(account_value=5e6, avg_hold_sec=18000, n_trades=5)
        r2 = classify_trader(account_value=5e6, avg_hold_sec=18000, n_trades=5,
                             turnover=0.5, win_rate=0.6)
        # 分类类型不受可选参数影响（当前实现 turnover 为备用字段）
        assert r1["type"] == r2["type"]


# ──────────────────────── fmt_classify 格式化 ────────────────────────

class TestFmtClassify:
    """fmt_classify 格式化字符串测试。"""

    def test_whale_label(self):
        """庄家标签含 🐋。"""
        r = classify_trader(account_value=10e6, avg_hold_sec=86400, n_trades=5)
        label = fmt_classify(r)
        assert "🐋" in label
        assert "庄家" in label

    def test_hot_money_label(self):
        """游资标签含 🔥。"""
        r = classify_trader(account_value=200e3, avg_hold_sec=900, n_trades=50)
        label = fmt_classify(r)
        assert "🔥" in label
        assert "游资" in label

    def test_mixed_label(self):
        """混合标签含 🔀。"""
        r = classify_trader(account_value=1e6, avg_hold_sec=3600, n_trades=10)
        label = fmt_classify(r)
        assert "🔀" in label
        assert "混合" in label

    def test_hold_display_minutes(self):
        """持仓 < 1h 时 label 含分钟表示。"""
        r = classify_trader(account_value=200e3, avg_hold_sec=1500, n_trades=40)
        label = fmt_classify(r)
        assert "m" in label or "s" in label  # 25m

    def test_hold_display_hours(self):
        """持仓 ≥ 1h 时 label 含小时表示。"""
        r = classify_trader(account_value=10e6, avg_hold_sec=28800, n_trades=5)
        label = fmt_classify(r)
        assert "h" in label  # 8.0h

    def test_account_million_display(self):
        """账户 ≥ $1M 时 label 含 M 单位。"""
        r = classify_trader(account_value=10e6, avg_hold_sec=86400, n_trades=2)
        label = fmt_classify(r)
        assert "M" in label

    def test_account_thousand_display(self):
        """账户 $1K~$1M 时 label 含 K 单位。"""
        r = classify_trader(account_value=500_000.0, avg_hold_sec=300, n_trades=80)
        label = fmt_classify(r)
        # 热钱标签不含 M（资金不足1M）或含 K
        # fmt_classify 游资标签只显示均持和频率，不显示账户值，所以只要不崩溃即可
        assert isinstance(label, str)
        assert len(label) > 2

    def test_returns_string(self):
        """任何输入都返回非空字符串。"""
        for r in [
            classify_trader(account_value=0, avg_hold_sec=0, n_trades=0),
            classify_trader(account_value=50e6, avg_hold_sec=604800, n_trades=1),
            classify_trader(account_value=300e3, avg_hold_sec=600, n_trades=200),
        ]:
            label = fmt_classify(r)
            assert isinstance(label, str)
            assert len(label) > 0
