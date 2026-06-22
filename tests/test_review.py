"""预测正确性回顾层单元测试（合成数据，确定性，不联网）。

覆盖：
  - record()：有效 / 无效价格 / 双源价差计算 / px_emit 选择逻辑
  - evaluate_due()：到期 correct/realized_ret 计算；未到期不评估；部分价格缺失跳过
  - accuracy_report()：hit_rate/分组/gap_warn_count 准确
  - fmt_accuracy()：含关键字的字符串输出；空数据友好提示
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from smc_tracker.review import PredictionReview, fmt_accuracy, market_neutral_stats


# ---- 轻量 Store 存根（仅需 conn） ----
class _FakeStore:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)


@pytest.fixture()
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture()
def rev(store: _FakeStore) -> PredictionReview:
    return PredictionReview(store)


# ---- record() 测试 ----

class TestRecord:
    def test_record_hl_preferred_over_bg(self, rev: PredictionReview) -> None:
        """hl_px > 0 时 px_emit 应等于 hl_px（#98 修复：与 evaluate_due 的 price_of「HL 优先」同源，
        避免 k 计价币 emit/eval 单位错配导致 realized_ret 爆炸）。"""
        now = int(time.time() * 1000)
        rev.record(ts=now, coin="DOGE", kind="跟庄", direction="long",
                   hl_px=0.1, bg_px=0.12)
        row = rev.store.conn.execute(
            "SELECT px_emit, hl_px, bg_px FROM predictions WHERE coin='DOGE'"
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(0.1)   # HL 优先（与评估价源一致）
        assert row[1] == pytest.approx(0.1)
        assert row[2] == pytest.approx(0.12)

    def test_record_eval_consistent_units_for_k_coin(self, rev: PredictionReview) -> None:
        """真实病例(kSHIB)：HL 千倍计价(0.0047) vs Bitget 原始(4.694e-06)。px_emit 取 HL，
        evaluate_due 的 price_of 也取 HL → realized_ret 合理(~ +2%)，**不再爆炸成 +1000(+10万%)**。"""
        now = int(time.time() * 1000)
        hz = 300_000
        rev.record(ts=now, coin="kSHIB", kind="SMC", direction="long",
                   hl_px=0.004694, bg_px=4.694e-06, horizon_ms=hz)
        # 评估：HL 价 price_of 返回 0.004788（涨 ~2%）
        rev.evaluate_due(lambda c: 0.004788 if c == "kSHIB" else None, now + hz + 1)
        row = rev.store.conn.execute(
            "SELECT px_emit, px_eval, realized_ret, correct FROM predictions WHERE coin='kSHIB'"
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(0.004694)            # px_emit 用 HL（非 Bitget 原始）
        assert abs(row[2]) < 0.1, f"realized_ret 应合理(~2%)，实际 {row[2]}（单位错配则会 ~+1000）"
        assert row[2] == pytest.approx((0.004788 - 0.004694) / 0.004694, rel=1e-3)
        assert row[3] == 1                                  # long + 上涨 → 命中

    def test_record_fallback_to_hl(self, rev: PredictionReview) -> None:
        """bg_px <= 0 且 hl_px > 0 时 px_emit 应等于 hl_px。"""
        now = int(time.time() * 1000)
        rev.record(ts=now, coin="BTC", kind="前瞻", direction="short",
                   hl_px=50000.0, bg_px=0.0)
        row = rev.store.conn.execute(
            "SELECT px_emit, bg_px FROM predictions WHERE coin='BTC'"
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(50000.0)
        assert row[1] is None  # bg_px <= 0 存 NULL

    def test_record_skips_when_no_valid_price(self, rev: PredictionReview) -> None:
        """两源都 <= 0 时不落库。"""
        now = int(time.time() * 1000)
        rev.record(ts=now, coin="INVALID", kind="共识", direction="long",
                   hl_px=0.0, bg_px=0.0)
        count = rev.store.conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE coin='INVALID'"
        ).fetchone()[0]
        assert count == 0

    def test_record_px_gap_pct_two_sources(self, rev: PredictionReview) -> None:
        """两源都 > 0 时 px_gap_pct 应正确计算。"""
        now = int(time.time() * 1000)
        hl, bg = 100.0, 102.0
        rev.record(ts=now, coin="ETH", kind="背离", direction="up",
                   hl_px=hl, bg_px=bg)
        row = rev.store.conn.execute(
            "SELECT px_gap_pct FROM predictions WHERE coin='ETH'"
        ).fetchone()
        assert row is not None
        mid = (hl + bg) / 2  # 101.0
        expected = abs(hl - bg) / mid  # 2/101 ≈ 0.0198
        assert row[0] == pytest.approx(expected, rel=1e-6)

    def test_record_px_gap_pct_null_when_one_missing(self, rev: PredictionReview) -> None:
        """仅有一源时 px_gap_pct 应为 NULL。"""
        now = int(time.time() * 1000)
        rev.record(ts=now, coin="SOL", kind="暴涨", direction="up",
                   hl_px=20.0, bg_px=0.0)
        row = rev.store.conn.execute(
            "SELECT px_gap_pct FROM predictions WHERE coin='SOL'"
        ).fetchone()
        assert row is not None
        assert row[0] is None


# ---- evaluate_due() 测试 ----

class TestEvaluateDue:
    def _insert_pred(
        self,
        rev: PredictionReview,
        *,
        coin: str,
        direction: str,
        kind: str,
        px_emit: float,
        ts: int,
        horizon_ms: int = 3_600_000,
    ) -> None:
        rev.record(ts=ts, coin=coin, kind=kind, direction=direction,
                   hl_px=px_emit, bg_px=0.0, horizon_ms=horizon_ms)

    def test_correct_long_when_price_rises(self, rev: PredictionReview) -> None:
        """direction=long，价格上涨 → correct=1，realized_ret>0。"""
        ts_emit = 1_000_000
        horizon = 3_600_000
        now = ts_emit + horizon + 1

        self._insert_pred(rev, coin="DOGE", direction="long", kind="跟庄",
                          px_emit=1.0, ts=ts_emit, horizon_ms=horizon)

        def price_of(coin: str) -> float | None:
            return 1.1  # 价格上涨 10%

        n = rev.evaluate_due(price_of, now)
        assert n == 1

        row = rev.store.conn.execute(
            "SELECT correct, realized_ret, evaluated FROM predictions WHERE coin='DOGE'"
        ).fetchone()
        assert row[2] == 1        # evaluated
        assert row[0] == 1        # correct
        assert row[1] == pytest.approx(0.1, rel=1e-6)

    def test_incorrect_long_when_price_falls(self, rev: PredictionReview) -> None:
        """direction=long，价格下跌 → correct=0，realized_ret<0。"""
        ts_emit = 2_000_000
        horizon = 3_600_000
        now = ts_emit + horizon + 1

        self._insert_pred(rev, coin="BTC", direction="long", kind="前瞻",
                          px_emit=50000.0, ts=ts_emit, horizon_ms=horizon)

        def price_of(coin: str) -> float | None:
            return 48000.0  # 下跌 4%

        n = rev.evaluate_due(price_of, now)
        assert n == 1

        row = rev.store.conn.execute(
            "SELECT correct, realized_ret FROM predictions WHERE coin='BTC'"
        ).fetchone()
        assert row[0] == 0
        assert row[1] == pytest.approx(-0.04, rel=1e-6)

    def test_correct_short_when_price_falls(self, rev: PredictionReview) -> None:
        """direction=short，价格下跌 → correct=1。"""
        ts_emit = 3_000_000
        horizon = 3_600_000
        now = ts_emit + horizon + 1

        self._insert_pred(rev, coin="ETH", direction="short", kind="共识",
                          px_emit=2000.0, ts=ts_emit, horizon_ms=horizon)

        def price_of(coin: str) -> float | None:
            return 1900.0

        n = rev.evaluate_due(price_of, now)
        assert n == 1
        row = rev.store.conn.execute(
            "SELECT correct FROM predictions WHERE coin='ETH'"
        ).fetchone()
        assert row[0] == 1

    def test_correct_down_direction(self, rev: PredictionReview) -> None:
        """direction=down，价格下跌 → correct=1。"""
        ts_emit = 4_000_000
        horizon = 3_600_000
        now = ts_emit + horizon + 1

        self._insert_pred(rev, coin="XRP", direction="down", kind="暴涨",
                          px_emit=0.5, ts=ts_emit, horizon_ms=horizon)

        def price_of(coin: str) -> float | None:
            return 0.4

        n = rev.evaluate_due(price_of, now)
        assert n == 1
        row = rev.store.conn.execute(
            "SELECT correct FROM predictions WHERE coin='XRP'"
        ).fetchone()
        assert row[0] == 1

    def test_not_due_rows_not_evaluated(self, rev: PredictionReview) -> None:
        """未到期的记录不应被评估。"""
        ts_emit = 5_000_000
        horizon = 3_600_000
        now = ts_emit + horizon - 1  # 差 1ms 未到期

        self._insert_pred(rev, coin="SOL", direction="up", kind="背离",
                          px_emit=100.0, ts=ts_emit, horizon_ms=horizon)

        def price_of(coin: str) -> float | None:
            return 110.0

        n = rev.evaluate_due(price_of, now)
        assert n == 0

        row = rev.store.conn.execute(
            "SELECT evaluated FROM predictions WHERE coin='SOL'"
        ).fetchone()
        assert row[0] == 0  # 仍未评估

    def test_skip_when_price_unavailable(self, rev: PredictionReview) -> None:
        """price_of 返回 None 或 0 时该条跳过（不更新 evaluated）。"""
        ts_emit = 6_000_000
        horizon = 3_600_000
        now = ts_emit + horizon + 1

        self._insert_pred(rev, coin="LINK", direction="long", kind="跟庄",
                          px_emit=10.0, ts=ts_emit, horizon_ms=horizon)

        def price_of(coin: str) -> float | None:
            return None  # 价格不可用

        n = rev.evaluate_due(price_of, now)
        assert n == 0

        row = rev.store.conn.execute(
            "SELECT evaluated FROM predictions WHERE coin='LINK'"
        ).fetchone()
        assert row[0] == 0

    def test_partial_evaluation(self, rev: PredictionReview) -> None:
        """混合到期/未到期，只评估到期的。"""
        base_ts = 7_000_000
        horizon = 3_600_000

        self._insert_pred(rev, coin="AAA", direction="up", kind="前瞻",
                          px_emit=10.0, ts=base_ts, horizon_ms=horizon)
        self._insert_pred(rev, coin="BBB", direction="down", kind="前瞻",
                          px_emit=20.0, ts=base_ts + 2 * horizon, horizon_ms=horizon)

        now = base_ts + horizon + 1  # AAA 到期，BBB 未到期

        def price_of(coin: str) -> float | None:
            return {"AAA": 11.0, "BBB": 19.0}.get(coin)

        n = rev.evaluate_due(price_of, now)
        assert n == 1

        r_aaa = rev.store.conn.execute(
            "SELECT evaluated FROM predictions WHERE coin='AAA'"
        ).fetchone()
        r_bbb = rev.store.conn.execute(
            "SELECT evaluated FROM predictions WHERE coin='BBB'"
        ).fetchone()
        assert r_aaa[0] == 1
        assert r_bbb[0] == 0


# ---- accuracy_report() 测试 ----

class TestAccuracyReport:
    def _setup_evaluated(
        self, rev: PredictionReview, store: _FakeStore
    ) -> None:
        """直接向 predictions 插入若干已评估记录，供报告测试用。"""
        rows = [
            # (ts, coin, kind, direction, px_emit, hl_px, bg_px, horizon_ms, evaluated,
            #  eval_ts, px_eval, realized_ret, correct, px_gap_pct)
            # 跟庄 2 条：1 命中 / 1 未命中
            (1000, "DOGE", "跟庄", "long",  1.0, 1.0, 0.0, 3600000, 1, 2000, 1.1,  0.10, 1, None),
            (1001, "ETH",  "跟庄", "short", 2000.0, 2000.0, 0.0, 3600000, 1, 2001, 2100.0, 0.05, 0, None),
            # 前瞻 1 条：命中，且两源价差 > 1%
            (1002, "BTC",  "前瞻", "up",    50000.0, 49000.0, 50600.0, 3600000, 1, 2002,
             51000.0, 0.02, 1, abs(49000.0 - 50600.0) / ((49000.0 + 50600.0) / 2)),
            # 背离 1 条：命中
            (1003, "SOL",  "背离", "down",  100.0, 100.0, 0.0, 3600000, 1, 2003, 90.0, -0.10, 1, None),
        ]
        store.conn.executemany(
            "INSERT INTO predictions"
            "(ts,coin,kind,direction,px_emit,hl_px,bg_px,horizon_ms,evaluated,"
            "eval_ts,px_eval,realized_ret,correct,px_gap_pct,dt,eval_dt)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
              r[9], r[10], r[11], r[12], r[13],
              f"2025-01-01 00:00:0{i} CST", f"2025-01-01 01:00:0{i} CST")
             for i, r in enumerate(rows)],
        )

    def test_hit_rate_and_grouping(self, rev: PredictionReview, store: _FakeStore) -> None:
        self._setup_evaluated(rev, store)
        rep = rev.accuracy_report(0, 99999)

        assert rep["total_n"] == 4
        assert rep["total_hits"] == 3
        assert rep["hit_rate"] == pytest.approx(0.75, rel=1e-6)

        by_kind = rep["by_kind"]
        assert "跟庄" in by_kind
        assert by_kind["跟庄"]["n"] == 2
        assert by_kind["跟庄"]["hits"] == 1
        assert by_kind["跟庄"]["hit_rate"] == pytest.approx(0.5, rel=1e-6)

        assert "前瞻" in by_kind
        assert by_kind["前瞻"]["n"] == 1
        assert by_kind["前瞻"]["hit_rate"] == pytest.approx(1.0, rel=1e-6)

        assert "背离" in by_kind
        assert by_kind["背离"]["hit_rate"] == pytest.approx(1.0, rel=1e-6)

    def test_gap_warn_count(self, rev: PredictionReview, store: _FakeStore) -> None:
        """两源价差 > 1% 的记录应被计入 gap_warn_count。"""
        self._setup_evaluated(rev, store)
        rep = rev.accuracy_report(0, 99999)
        # 只有 BTC 那条的 px_gap_pct ≈ 0.032 > 0.01
        assert rep["gap_warn_count"] >= 1

    def test_since_filter(self, rev: PredictionReview, store: _FakeStore) -> None:
        """since_ms 过滤：早于 since 的记录不应计入。"""
        self._setup_evaluated(rev, store)
        # since_ms=1003 → 只有 SOL 那条（ts=1003）被纳入
        rep = rev.accuracy_report(since_ms=1003, now_ms=99999)
        assert rep["total_n"] == 1
        assert rep["hit_rate"] == pytest.approx(1.0)

    def test_recent_list(self, rev: PredictionReview, store: _FakeStore) -> None:
        self._setup_evaluated(rev, store)
        rep = rev.accuracy_report(0, 99999)
        assert len(rep["recent"]) <= 10
        assert len(rep["recent"]) == 4  # 共 4 条
        # 每条应含必要字段
        for item in rep["recent"]:
            assert "coin" in item
            assert "kind" in item
            assert "direction" in item
            assert "realized_ret" in item
            assert "correct" in item

    def test_empty_report(self, rev: PredictionReview) -> None:
        """无样本时返回正确空报告结构。"""
        rep = rev.accuracy_report(0, 99999)
        assert rep["total_n"] == 0
        assert rep["hit_rate"] == 0.0
        assert rep["by_kind"] == {}
        assert rep["recent"] == []
        assert rep["gap_warn_count"] == 0


# ---- fmt_accuracy() 测试 ----

class TestFmtAccuracy:
    def test_empty_data_friendly_message(self) -> None:
        """空数据时返回含「样本不足」的友好提示。"""
        empty_rep = {
            "total_n": 0, "total_hits": 0, "hit_rate": 0.0, "avg_ret": 0.0,
            "by_kind": {}, "gap_warn_count": 0, "recent": [],
        }
        text = fmt_accuracy(empty_rep)
        assert "样本不足" in text
        assert "预测准确率回顾" in text

    def test_contains_key_sections(self) -> None:
        """有数据时应包含关键字段。"""
        rep = {
            "total_n": 10,
            "total_hits": 7,
            "hit_rate": 0.7,
            "avg_ret": 0.025,
            "by_kind": {
                "跟庄": {"n": 6, "hits": 5, "hit_rate": 5 / 6, "avg_ret": 0.03},
                "前瞻": {"n": 4, "hits": 2, "hit_rate": 0.5, "avg_ret": -0.01},
            },
            "gap_warn_count": 2,
            "recent": [
                {"dt": "2025-01-01 00:00:00 CST", "coin": "DOGE",
                 "kind": "跟庄", "direction": "long", "realized_ret": 0.05, "correct": 1},
            ],
        }
        text = fmt_accuracy(rep)
        assert "预测准确率回顾" in text
        assert "跟庄" in text
        assert "前瞻" in text
        assert "70.0%" in text       # 总体命中率
        assert "价差" in text        # 数据质量告警
        assert "DOGE" in text        # 最近样本
        assert "✅" in text          # correct=1 → 绿勾

    def test_no_gap_warn_when_zero(self) -> None:
        """gap_warn_count=0 时不应出现价差告警行。"""
        rep = {
            "total_n": 5,
            "total_hits": 3,
            "hit_rate": 0.6,
            "avg_ret": 0.01,
            "by_kind": {"跟庄": {"n": 5, "hits": 3, "hit_rate": 0.6, "avg_ret": 0.01}},
            "gap_warn_count": 0,
            "recent": [],
        }
        text = fmt_accuracy(rep)
        assert "价差" not in text


# ---- edge / 样本充分性（诚实评估）测试 ----

def _make_evaluated(rev: PredictionReview, n_hit: int, n_miss: int,
                    kind: str = "跟庄") -> None:
    """走真实 record→evaluate_due 路径造 n_hit 命中 + n_miss 未命中的已评估样本。"""
    ts, horizon = 1_000_000, 3_600_000
    now = ts + horizon + 1
    for i in range(n_hit):
        rev.record(ts=ts, coin=f"H{i}", kind=kind, direction="long",
                   hl_px=10.0, bg_px=0.0, horizon_ms=horizon)
    for i in range(n_miss):
        rev.record(ts=ts, coin=f"M{i}", kind=kind, direction="long",
                   hl_px=10.0, bg_px=0.0, horizon_ms=horizon)
    rev.evaluate_due(lambda c: 11.0 if c.startswith("H") else 9.0, now)


class TestEdgeAndSufficiency:
    def test_edge_and_insufficient_sample(self, rev: PredictionReview) -> None:
        """4 样本 3 命中 → edge=+0.25、sufficient=False，文本含边际 + 样本不足。"""
        _make_evaluated(rev, 3, 1)
        rep = rev.accuracy_report(0, 10_000_000)   # 默认 min_sample=20
        assert rep["total_n"] == 4
        assert rep["edge"] == pytest.approx(0.25, rel=1e-6)
        assert rep["sufficient"] is False
        assert rep["min_sample"] == 20
        assert rep["by_kind"]["跟庄"]["edge"] == pytest.approx(0.25, rel=1e-6)
        text = fmt_accuracy(rep)
        assert "边际" in text
        assert "样本不足" in text

    def test_sufficient_large_sample_no_caveat(self, rev: PredictionReview) -> None:
        """25 样本 → sufficient=True，文本含边际但无「样本不足」告警。"""
        _make_evaluated(rev, 15, 10)
        rep = rev.accuracy_report(0, 10_000_000)
        assert rep["total_n"] == 25
        assert rep["sufficient"] is True
        assert rep["edge"] == pytest.approx(15 / 25 - 0.5, rel=1e-6)
        text = fmt_accuracy(rep)
        assert "边际" in text
        assert "样本不足" not in text

    def test_custom_min_sample(self, rev: PredictionReview) -> None:
        """min_sample 可调：4 样本在阈值=4 时判定 sufficient。"""
        _make_evaluated(rev, 2, 2)
        assert rev.accuracy_report(0, 10_000_000, min_sample=4)["sufficient"] is True
        assert rev.accuracy_report(0, 10_000_000, min_sample=5)["sufficient"] is False


class TestDirectionAdjustedReturn:
    def test_short_correct_yields_positive_avg_ret(self, rev: PredictionReview) -> None:
        """做空预测价格下跌→命中 且 avg_ret(按向收益)为正，不被原始负价变动误导。"""
        ts, horizon = 1_000_000, 3_600_000
        now = ts + horizon + 1
        rev.record(ts=ts, coin="ETH", kind="共识", direction="short",
                   hl_px=2000.0, bg_px=0.0, horizon_ms=horizon)
        rev.evaluate_due(lambda c: 1900.0, now)          # 价格跌 5%
        rep = rev.accuracy_report(0, 10_000_000)
        assert rep["total_hits"] == 1                    # 做空价跌=命中
        assert rep["avg_ret"] == pytest.approx(0.05, rel=1e-6)   # 按向收益 +5%
        assert rep["by_kind"]["共识"]["avg_ret"] == pytest.approx(0.05, rel=1e-6)
        rec = rep["recent"][0]
        assert rec["realized_ret"] == pytest.approx(-0.05, rel=1e-6)  # 原始价变动为负
        assert rec["strategy_ret"] == pytest.approx(0.05, rel=1e-6)   # 按向收益为正
        assert "按向+5.00%" in fmt_accuracy(rep)

    def test_long_keeps_raw_sign(self, rev: PredictionReview) -> None:
        """看多预测：按向收益 == 原始收益（不翻转）。"""
        ts, horizon = 2_000_000, 3_600_000
        now = ts + horizon + 1
        rev.record(ts=ts, coin="BTC", kind="跟庄", direction="long",
                   hl_px=100.0, bg_px=0.0, horizon_ms=horizon)
        rev.evaluate_due(lambda c: 103.0, now)           # 涨 3%
        rep = rev.accuracy_report(0, 10_000_000)
        assert rep["avg_ret"] == pytest.approx(0.03, rel=1e-6)
        assert rep["recent"][0]["strategy_ret"] == pytest.approx(0.03, rel=1e-6)


# ---- market_neutral_stats() 纯函数测试 ----

class TestMarketNeutralStats:
    """横截面去均值市场中性命中率纯函数测试（合成数据，确定性，不联网）。"""

    def test_empty_records_returns_zero_dict(self) -> None:
        """空输入安全返回全零字典，不抛异常。"""
        result = market_neutral_stats([])
        assert result["n"] == 0
        assert result["hits"] == 0
        assert result["hit_rate"] == pytest.approx(0.0)
        assert result["edge"] == pytest.approx(0.0)
        assert result["avg_excess"] == pytest.approx(0.0)

    def test_beta_contamination_stripped(self) -> None:
        """全做空 + 同桶市场普跌：原始命中率 100%，去均值后超额≈0 → 中性命中率≈50%。

        场景：5 只币均做空，市场整体跌 5%（每只都跌 5%）。
        原始命中率：100%（跌了=做空命中）。
        超额收益：每只均为 -5% - (-5%) = 0 → 做空方向超额=0，中性命中率应≈50%（恰好在边界）。
        """
        bucket = 3_600_000  # 1h，全部放同一桶
        ts_base = 1_000_000
        # 5 只币均做空，价格各跌 5%（realized_ret = -0.05）
        records: list[tuple[int, str, float]] = [
            (ts_base, "short", -0.05),
            (ts_base, "short", -0.05),
            (ts_base, "short", -0.05),
            (ts_base, "short", -0.05),
            (ts_base, "short", -0.05),
        ]
        result = market_neutral_stats(records, bucket_ms=bucket)
        assert result["n"] == 5
        # 桶均值 = -0.05，excess = -0.05 - (-0.05) = 0
        # excess=0 不满足 excess < 0，中性命中=0/5 → hit_rate = 0.0（非 50%）
        # 因为边界 excess==0 → 不命中；重要的是：远低于原始 100%
        assert result["hit_rate"] < 0.2  # 绝对低于原始 100%，beta 已剥离
        assert result["avg_excess"] == pytest.approx(0.0, abs=1e-9)  # 纯 beta，无超额

    def test_true_alpha_detected(self) -> None:
        """真实选币 alpha：做空的币跌幅大于同桶均值 → 中性命中率 > 50%。

        场景：同桶内 2 只做空：A跌10%, B跌2%（均值跌6%）。
          A: excess = -10% - (-6%) = -4% → is_up=False, excess<0 → 命中
          B: excess = -2% - (-6%) = +4% → is_up=False, excess>0 → 不命中
        中性命中率 = 1/2 = 50%。

        场景 2：3 只做空全部跌幅 > 均值（真正超额）→ 命中率 > 50%。
        """
        bucket = 3_600_000
        ts_base = 2_000_000
        # 4 只同桶：2 只做空跌幅显著超均值（-15% vs 均值-5%）；2 只做空跌幅低于均值（+5% vs 均值-5%）
        # 均值 = (-15 + -15 + 5 + 5) / 4 = -5%（0.5做多对）
        # 为了干净测试只用做空：3 只跌 -10%，1 只跌 -2%，均值 = -8%
        # A(-10%): excess=-2% → 做空 excess<0 → 命中
        # B(-10%): 命中
        # C(-10%): 命中
        # D(-2%):  excess=+6% → 不命中
        records: list[tuple[int, str, float]] = [
            (ts_base, "short", -0.10),
            (ts_base, "short", -0.10),
            (ts_base, "short", -0.10),
            (ts_base, "short", -0.02),
        ]
        result = market_neutral_stats(records, bucket_ms=bucket)
        assert result["n"] == 4
        assert result["hits"] == 3      # 前3命中，最后1不命中
        assert result["hit_rate"] == pytest.approx(0.75, rel=1e-6)
        assert result["edge"] == pytest.approx(0.25, rel=1e-6)
        # sexc for 命中3条: -(-10%-(-8%)) = -(-2%) = +2% each → avg positive
        assert result["avg_excess"] > 0.0

    def test_long_direction_with_alpha(self) -> None:
        """做多方向：超出同桶均值的涨幅 → 中性命中。

        同桶2只做多：A涨10%，B涨2%，均值涨6%。
          A: excess=+4% → long 命中
          B: excess=-4% → long 不命中
        命中率=50%（边界但数值正确）。
        加一个更强的场景：3只做多均涨超均值。
        """
        bucket = 3_600_000
        ts_base = 3_000_000
        # 4 只做多：3 只涨 10%，1 只涨 2%，均值 8%
        # A(10%): excess=+2% → long 命中
        # B(10%): 命中
        # C(10%): 命中
        # D(2%):  excess=-6% → 不命中
        records: list[tuple[int, str, float]] = [
            (ts_base, "long", 0.10),
            (ts_base, "up",   0.10),
            (ts_base, "long", 0.10),
            (ts_base, "long", 0.02),
        ]
        result = market_neutral_stats(records, bucket_ms=bucket)
        assert result["n"] == 4
        assert result["hits"] == 3
        assert result["hit_rate"] == pytest.approx(0.75, rel=1e-6)
        assert result["avg_excess"] > 0.0

    def test_cross_bucket_isolation(self) -> None:
        """不同时间桶互相独立：不同桶的均值不互相影响。

        桶1(ts=0): 1条做多, 涨5%。桶均值5%，excess=0 → 不命中（边界）。
        桶2(ts=bucket): 1条做多, 涨10%，另1条平(0%)。桶均值5%，
          做多10%: excess=+5% → 命中；另做多0%: excess=-5% → 不命中。
        """
        bucket = 3_600_000
        records: list[tuple[int, str, float]] = [
            (0,          "long", 0.05),   # 桶0，单条，excess=0，不命中
            (bucket,     "long", 0.10),   # 桶1，excess=+5%，命中
            (bucket + 1, "long", 0.00),   # 桶1，excess=-5%，不命中
        ]
        result = market_neutral_stats(records, bucket_ms=bucket)
        assert result["n"] == 3
        assert result["hits"] == 1   # 只有第2条命中

    def test_accuracy_report_includes_market_neutral(self) -> None:
        """accuracy_report 返回 dict 应含 market_neutral 键，结构正确。"""
        store = _FakeStore()
        rev = PredictionReview(store)
        ts, horizon = 1_000_000, 3_600_000
        now = ts + horizon + 1
        # 加几条评估过的预测
        rev.record(ts=ts, coin="X1", kind="跟庄", direction="long",
                   hl_px=100.0, bg_px=0.0, horizon_ms=horizon)
        rev.record(ts=ts, coin="X2", kind="跟庄", direction="short",
                   hl_px=100.0, bg_px=0.0, horizon_ms=horizon)
        rev.evaluate_due(lambda c: 110.0 if c == "X1" else 90.0, now)
        rep = rev.accuracy_report(0, 10_000_000)
        assert "market_neutral" in rep
        mn = rep["market_neutral"]
        assert "n" in mn
        assert "hits" in mn
        assert "hit_rate" in mn
        assert "edge" in mn
        assert "avg_excess" in mn
        assert mn["n"] == 2

    def test_fmt_accuracy_shows_market_neutral_line(self) -> None:
        """fmt_accuracy 应在总体命中率附近输出市场中性命中率行。"""
        rep = {
            "total_n": 25,
            "total_hits": 18,
            "hit_rate": 0.72,
            "edge": 0.22,
            "sufficient": True,
            "min_sample": 20,
            "avg_ret": 0.02,
            "n_long": 13, "n_short": 12,
            "dir_skew": 0.52,
            "avg_market_move": 0.001,
            "beta_suspect": False,
            "by_kind": {},
            "by_horizon": {},
            "gap_warn_count": 0,
            "recent": [],
            "market_neutral": {
                "n": 25, "hits": 14, "hit_rate": 0.56,
                "edge": 0.06, "avg_excess": 0.005,
            },
        }
        text = fmt_accuracy(rep)
        assert "市场中性命中率" in text
        assert "56.0%" in text
        assert "alpha" in text  # 应提到纯 alpha

    def test_fmt_accuracy_market_neutral_insufficient_sample(self) -> None:
        """样本不足时市场中性行应标注「样本不足，仅供参考」。"""
        rep = {
            "total_n": 5,           # < min_sample=20
            "total_hits": 3,
            "hit_rate": 0.6,
            "edge": 0.1,
            "sufficient": False,
            "min_sample": 20,
            "avg_ret": 0.01,
            "n_long": 3, "n_short": 2,
            "dir_skew": 0.6,
            "avg_market_move": 0.0,
            "beta_suspect": False,
            "by_kind": {},
            "by_horizon": {},
            "gap_warn_count": 0,
            "recent": [],
            "market_neutral": {
                "n": 5, "hits": 3, "hit_rate": 0.6,
                "edge": 0.1, "avg_excess": 0.002,
            },
        }
        text = fmt_accuracy(rep)
        assert "市场中性命中率" in text
        assert "样本不足，仅供参考" in text

    def test_fmt_accuracy_graceful_without_market_neutral_key(self) -> None:
        """旧 report 无 market_neutral 键时 fmt_accuracy 不报错（向后兼容）。"""
        rep = {
            "total_n": 10,
            "total_hits": 6,
            "hit_rate": 0.6,
            "edge": 0.1,
            "sufficient": False,
            "min_sample": 20,
            "avg_ret": 0.01,
            "by_kind": {},
            "by_horizon": {},
            "gap_warn_count": 0,
            "recent": [],
            # 无 market_neutral 键
        }
        # 应正常渲染，不抛 KeyError
        text = fmt_accuracy(rep)
        assert "预测准确率回顾" in text
        assert "市场中性命中率" not in text  # 旧 report 无此键时跳过，不输出该行


class TestBaseRateCorrection:
    def test_all_short_down_market_flags_beta(self, rev: PredictionReview) -> None:
        """全做空 + 市场普跌 → 命中率高但 beta_suspect=True(边际或来自趋势,诚实标注)。"""
        ts, horizon = 1_000_000, 3_600_000
        now = ts + horizon + 1
        for i in range(5):
            rev.record(ts=ts, coin=f"C{i}", kind="共识", direction="short",
                       hl_px=100.0, bg_px=0.0, horizon_ms=horizon)
        rev.evaluate_due(lambda c: 98.0, now)            # 全跌 2% → 做空全中
        rep = rev.accuracy_report(0, 10_000_000)
        assert rep["hit_rate"] == pytest.approx(1.0)     # 命中率 100%
        assert rep["n_short"] == 5 and rep["n_long"] == 0
        assert rep["avg_market_move"] == pytest.approx(-0.02, rel=1e-6)  # 市场净跌 2%
        assert rep["beta_suspect"] is True               # 一边倒+市场同向→疑趋势beta
        assert "趋势 beta" in fmt_accuracy(rep)

    def test_mixed_directions_no_beta_flag(self, rev: PredictionReview) -> None:
        """方向均衡(多空各半)→ 不触发 beta 嫌疑(非一边倒)。"""
        ts, horizon = 2_000_000, 3_600_000
        now = ts + horizon + 1
        rev.record(ts=ts, coin="UP1", kind="共识", direction="long",
                   hl_px=100.0, bg_px=0.0, horizon_ms=horizon)
        rev.record(ts=ts, coin="DN1", kind="共识", direction="short",
                   hl_px=100.0, bg_px=0.0, horizon_ms=horizon)
        rev.evaluate_due(lambda c: 105.0 if c == "UP1" else 95.0, now)
        rep = rev.accuracy_report(0, 10_000_000)
        assert rep["n_long"] == 1 and rep["n_short"] == 1
        assert rep["beta_suspect"] is False              # dir_skew=0.5 <0.8
        assert "趋势 beta" not in fmt_accuracy(rep)


# ---- MTF 多时间段批量记录测试 ----

MTF_HORIZONS_MIN = [5, 15, 30, 60, 240, 720, 1440]


class TestRecordMtf:
    """record_mtf 批量 MTF 记录（合成数据，确定性，不联网）。"""

    def test_record_mtf_inserts_7_horizons(self, rev: PredictionReview, store: _FakeStore) -> None:
        """record_mtf 对 7 个 TF 各插入一条，共 7 条记录。"""
        now = 1_000_000
        horizons_ms = [h * 60_000 for h in MTF_HORIZONS_MIN]
        n = rev.record_mtf(
            ts=now, coin="BTC", kind="跟庄", direction="long",
            hl_px=50_000.0, bg_px=0.0, horizons_ms=horizons_ms,
        )
        assert n == 7
        count = store.conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE coin='BTC'"
        ).fetchone()[0]
        assert count == 7

    def test_record_mtf_7_distinct_horizon_values(self, rev: PredictionReview, store: _FakeStore) -> None:
        """record_mtf 记录的 7 条 horizon_ms 各不相同，覆盖所有 TF。"""
        now = 2_000_000
        horizons_ms = [h * 60_000 for h in MTF_HORIZONS_MIN]
        rev.record_mtf(
            ts=now, coin="ETH", kind="共识", direction="short",
            hl_px=3_000.0, bg_px=0.0, horizons_ms=horizons_ms,
        )
        rows = store.conn.execute(
            "SELECT DISTINCT horizon_ms FROM predictions WHERE coin='ETH' ORDER BY horizon_ms"
        ).fetchall()
        actual_horizons = [r[0] for r in rows]
        expected = sorted(horizons_ms)
        assert actual_horizons == expected, f"期望 {expected}，实际 {actual_horizons}"

    def test_record_mtf_skips_when_no_valid_price(self, rev: PredictionReview, store: _FakeStore) -> None:
        """两源都无效时 record_mtf 返回 0，不插入任何记录。"""
        horizons_ms = [h * 60_000 for h in MTF_HORIZONS_MIN]
        n = rev.record_mtf(
            ts=3_000_000, coin="INVALID", kind="前瞻", direction="long",
            hl_px=0.0, bg_px=0.0, horizons_ms=horizons_ms,
        )
        assert n == 0
        count = store.conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE coin='INVALID'"
        ).fetchone()[0]
        assert count == 0

    def test_record_mtf_empty_horizons_returns_0(self, rev: PredictionReview) -> None:
        """空 horizons_ms 时返回 0，不报错。"""
        n = rev.record_mtf(
            ts=4_000_000, coin="SOL", kind="背离", direction="up",
            hl_px=100.0, bg_px=0.0, horizons_ms=[],
        )
        assert n == 0

    def test_record_existing_record_single_still_works(self, rev: PredictionReview) -> None:
        """单条 record() 调用向后兼容，不受 record_mtf 影响。"""
        now = 5_000_000
        rev.record(ts=now, coin="DOGE", kind="暴涨", direction="up",
                   hl_px=0.1, bg_px=0.0, horizon_ms=3_600_000)
        count = rev.store.conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE coin='DOGE'"
        ).fetchone()[0]
        assert count == 1


class TestMtfEvaluateDue:
    """各 TF 独立到期评估（短 TF 先到期，长 TF 后到期）。"""

    def test_short_tf_evaluates_before_long_tf(self, rev: PredictionReview, store: _FakeStore) -> None:
        """5m TF 先到期，1d TF 未到期，仅评估已到期的。"""
        ts_emit = 10_000_000
        hz_5m = 5 * 60_000     # 5 分钟
        hz_1d = 1440 * 60_000  # 1 天

        rev.record(ts=ts_emit, coin="BTC", kind="跟庄", direction="long",
                   hl_px=50_000.0, bg_px=0.0, horizon_ms=hz_5m)
        rev.record(ts=ts_emit, coin="BTC", kind="跟庄", direction="long",
                   hl_px=50_000.0, bg_px=0.0, horizon_ms=hz_1d)

        # 仅 5m 到期
        now_after_5m = ts_emit + hz_5m + 1
        n = rev.evaluate_due(lambda c: 51_000.0, now_after_5m)
        assert n == 1   # 只评估 5m 那条

        rows = store.conn.execute(
            "SELECT horizon_ms, evaluated FROM predictions WHERE coin='BTC' ORDER BY horizon_ms"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == hz_5m and rows[0][1] == 1    # 5m 已评估
        assert rows[1][0] == hz_1d and rows[1][1] == 0    # 1d 未评估

    def test_all_tfs_eventually_evaluate(self, rev: PredictionReview, store: _FakeStore) -> None:
        """记录 7 个 TF，等最长 TF 到期后全部 7 条均被评估。"""
        ts_emit = 20_000_000
        horizons_ms = [h * 60_000 for h in MTF_HORIZONS_MIN]
        rev.record_mtf(
            ts=ts_emit, coin="ETH", kind="SMC", direction="long",
            hl_px=3_000.0, bg_px=0.0, horizons_ms=horizons_ms,
        )
        max_hz = max(horizons_ms)
        now = ts_emit + max_hz + 1
        n = rev.evaluate_due(lambda c: 3_300.0, now)  # 价格涨 10%
        assert n == 7   # 全部 7 条被评估

        count_eval = store.conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE coin='ETH' AND evaluated=1"
        ).fetchone()[0]
        assert count_eval == 7


class TestMtfAccuracyReport:
    """by_horizon 分层统计 + by_horizon_market_neutral 正确。"""

    def test_by_horizon_has_all_tfs(self, rev: PredictionReview, store: _FakeStore) -> None:
        """accuracy_report 的 by_horizon 应包含 7 个 TF 的分层统计。"""
        ts_emit = 30_000_000
        horizons_ms = [h * 60_000 for h in MTF_HORIZONS_MIN]
        rev.record_mtf(
            ts=ts_emit, coin="BTC", kind="跟庄", direction="long",
            hl_px=50_000.0, bg_px=0.0, horizons_ms=horizons_ms,
        )
        # 让所有 TF 到期
        now = ts_emit + max(horizons_ms) + 1
        rev.evaluate_due(lambda c: 55_000.0, now)   # 涨 10% → 所有 TF 命中

        rep = rev.accuracy_report(0, now + 1_000)
        by_horizon = rep["by_horizon"]
        assert len(by_horizon) == 7
        # 所有 TF 均命中（价格上涨，long 方向对）
        for hz, d in by_horizon.items():
            assert d["n"] == 1
            assert d["hits"] == 1
            assert d["hit_rate"] == pytest.approx(1.0)

    def test_by_horizon_market_neutral_present(self, rev: PredictionReview, store: _FakeStore) -> None:
        """accuracy_report 应包含 by_horizon_market_neutral 键，各 TF 均有 mn 统计。"""
        ts_emit = 40_000_000
        horizons_ms = [h * 60_000 for h in MTF_HORIZONS_MIN]
        rev.record_mtf(
            ts=ts_emit, coin="ETH", kind="共识", direction="short",
            hl_px=3_000.0, bg_px=0.0, horizons_ms=horizons_ms,
        )
        now = ts_emit + max(horizons_ms) + 1
        rev.evaluate_due(lambda c: 2_800.0, now)  # 价格跌 → short 命中

        rep = rev.accuracy_report(0, now + 1_000)
        assert "by_horizon_market_neutral" in rep
        bhmn = rep["by_horizon_market_neutral"]
        assert len(bhmn) == 7
        for hz_key, mn in bhmn.items():
            assert "n" in mn
            assert "hit_rate" in mn
            assert "edge" in mn

    def test_fmt_accuracy_shows_mtf_section(self, rev: PredictionReview, store: _FakeStore) -> None:
        """fmt_accuracy 输出应包含 MTF 分水平线段落。"""
        ts_emit = 50_000_000
        horizons_ms = [h * 60_000 for h in MTF_HORIZONS_MIN]
        rev.record_mtf(
            ts=ts_emit, coin="SOL", kind="超级", direction="long",
            hl_px=100.0, bg_px=0.0, horizons_ms=horizons_ms,
        )
        now = ts_emit + max(horizons_ms) + 1
        rev.evaluate_due(lambda c: 110.0, now)

        rep = rev.accuracy_report(0, now + 1_000)
        text = fmt_accuracy(rep)
        assert "分水平线命中率" in text
        assert "MTF" in text
        # 验证各 TF 标签出现（至少短的 5m 和长的 1d）
        assert "5m" in text
        assert "24h" in text   # 1440m = 24h
