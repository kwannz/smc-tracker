"""信号有效性自适应加权（direct-correct 版）单元测试。

覆盖：
  - wilson_interval：数学正确性（已知答案）
  - SignalEfficacy.refresh：用 predictions.correct 字段直接聚合命中率驱动 Wilson 加权
    - 高命中大样本→加权；低命中大样本→反指降权；小样本→中性
  - _RET_OUTLIER 离群值过滤：|realized_ret|>10 行不计入
  - weight_of / label_of / is_contrarian：行为验证
  - fmt：含关键数字的格式输出
  - 核心修复验证：MTF 7-TF 场景不再错贴 contrarian（旧 market_neutral_stats 会在此场景误判）
全部使用合成数据，不联网，不依赖外部服务。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from smc_tracker.signals.efficacy import KindEfficacy, SignalEfficacy, wilson_interval


# ---- 轻量 Store 存根（仅需 conn）----

class _FakeStore:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        # 建 predictions 表（与 review.py 保持同 schema 子集）
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS predictions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           INTEGER NOT NULL,
                dt           TEXT    NOT NULL DEFAULT '',
                coin         TEXT    NOT NULL DEFAULT '',
                kind         TEXT    NOT NULL,
                direction    TEXT    NOT NULL DEFAULT 'long',
                px_emit      REAL    NOT NULL DEFAULT 1.0,
                hl_px        REAL,
                bg_px        REAL,
                px_gap_pct   REAL,
                horizon_ms   INTEGER NOT NULL DEFAULT 3600000,
                evaluated    INTEGER DEFAULT 0,
                eval_ts      INTEGER,
                eval_dt      TEXT,
                px_eval      REAL,
                realized_ret REAL,
                correct      INTEGER,
                note         TEXT
            );
        """)


@pytest.fixture()
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture()
def eff(store: _FakeStore) -> SignalEfficacy:
    return SignalEfficacy(store, min_sample=20)


def _now_ms() -> int:
    """当前 epoch ms（用于 ts，确保在 lookback 窗口内）。"""
    return int(time.time() * 1000)


# ---- 数据构造辅助函数 ----

def _insert_records(
    store: _FakeStore,
    *,
    kind: str,
    records: list[tuple[int, float]],  # [(correct, realized_ret), ...]
    ts_base: int | None = None,
    bucket_step_ms: int = 3_600_000,
) -> None:
    """插入 evaluated=1 的合成预测行。

    records: [(correct, realized_ret), ...]
      correct: 0 或 1
      realized_ret: 实际收益率（用于离群值过滤）
    ts_base: 起始时间戳 ms（默认 now-1000ms，在 lookback 窗口内）
    bucket_step_ms: 每条记录的时间间隔
    """
    if ts_base is None:
        ts_base = _now_ms() - 1000
    rows = [
        (ts_base + i * bucket_step_ms, kind, "long", realized_ret, correct)
        for i, (correct, realized_ret) in enumerate(records)
    ]
    store.conn.executemany(
        "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
        " VALUES(?, ?, ?, ?, ?, 1)",
        rows,
    )


def _insert_n_hits(
    store: _FakeStore,
    *,
    kind: str,
    hits: int,
    n: int,
    ts_base: int | None = None,
) -> None:
    """插入 n 条记录，其中 hits 条 correct=1，其余 correct=0。"""
    records = [(1, 0.05)] * hits + [(0, -0.02)] * (n - hits)
    _insert_records(store, kind=kind, records=records, ts_base=ts_base)


# ================================================================
# wilson_interval：数学正确性
# ================================================================

class TestWilsonInterval:
    def test_n_zero_returns_widest_interval(self) -> None:
        """n=0 时返回 (0.0, 1.0) 无信息区间。"""
        lo, hi = wilson_interval(0, 0)
        assert lo == pytest.approx(0.0)
        assert hi == pytest.approx(1.0)

    def test_perfect_hits_lower_above_half(self) -> None:
        """hits=72, n=72（100% 命中率）：Wilson 下界应 > 0.5。"""
        lo, hi = wilson_interval(72, 72)
        assert lo > 0.5, f"期望下界 > 0.5，实际 {lo:.4f}"
        assert hi <= 1.0

    def test_low_hit_rate_large_sample_upper_below_half(self) -> None:
        """hits=4, n=20（20% 命中率）：Wilson 上界应 < 0.5（统计显著反指）。

        注：n=14 时 28.6% 命中率的 CI 上界 ≈ 0.55，跨越 0.5，不足以判定反指；
        n=20 时 20% 命中率上界 ≈ 0.42 < 0.5，才统计显著。
        """
        lo, hi = wilson_interval(4, 20)
        assert hi < 0.5, f"期望上界 < 0.5（统计反指），实际 {hi:.4f}"
        assert lo >= 0.0

    def test_50pct_interval_crosses_half(self) -> None:
        """hits=10, n=20（50% 命中率）：区间应跨越 0.5（无统计显著性）。"""
        lo, hi = wilson_interval(10, 20)
        assert lo < 0.5 < hi

    def test_bounds_within_0_1(self) -> None:
        """区间边界必须在 [0, 1] 内。"""
        for hits, n in [(0, 1), (1, 1), (5, 10), (19, 20), (100, 100)]:
            lo, hi = wilson_interval(hits, n)
            assert 0.0 <= lo <= 1.0, f"下界越界: hits={hits},n={n} → {lo}"
            assert 0.0 <= hi <= 1.0, f"上界越界: hits={hits},n={n} → {hi}"
            assert lo <= hi

    def test_known_value_n50_hit35(self) -> None:
        """hits=35, n=50 (70% 命中率)：Wilson 95% CI 数值验证。

        手算结果（z=1.96）：lo≈0.563, hi≈0.809。
        """
        lo, hi = wilson_interval(35, 50)
        assert lo == pytest.approx(0.5625, abs=0.002)
        assert hi == pytest.approx(0.8090, abs=0.002)
        # 下界 > 0.5，应统计显著优于随机
        assert lo > 0.5

    def test_small_sample_hits_zero(self) -> None:
        """hits=0, n=5：下界应为 0，上界应有值。"""
        lo, hi = wilson_interval(0, 5)
        assert lo == pytest.approx(0.0, abs=1e-9)
        assert hi > 0.0


# ================================================================
# SignalEfficacy.refresh：权重/contrarian 分类逻辑（direct correct 语义）
# ================================================================

class TestRefreshHighHitRate:
    def test_high_hit_large_sample_gets_weight_above_1(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """高命中率 + 大样本（72/72 correct=1）→ weight > 1, contrarian=False。"""
        _insert_n_hits(store, kind="共识", hits=72, n=72)
        table = eff.refresh(_now_ms())
        assert "共识" in table, "共识 kind 应出现在结果中"
        e = table["共识"]
        assert e.n == 72
        assert e.hits == 72
        assert e.weight > 1.0, f"期望加权>1，实际 {e.weight:.3f}"
        assert not e.contrarian
        assert e.weight <= 1.5  # 上限 cap


class TestRefreshLowHitRate:
    def test_low_hit_large_sample_is_contrarian(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """低命中率 + 大样本（0/20 correct=1）→ contrarian=True, weight < 1。"""
        _insert_n_hits(store, kind="跟庄", hits=0, n=20)
        table = eff.refresh(_now_ms())
        assert "跟庄" in table
        e = table["跟庄"]
        assert e.n == 20
        assert e.contrarian, (
            f"命中0/20，Wilson上界<0.5，应标为反指。"
            f"lower={e.lower:.3f}, upper={e.upper:.3f}"
        )
        assert e.weight < 1.0
        assert e.weight >= 0.3  # 下限 cap


class TestRefreshSmallSample:
    def test_small_sample_is_neutral(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """小样本（n=6 < min_sample=20）→ weight=1.0, contrarian=False，note 含'样本不足'。"""
        _insert_n_hits(store, kind="背离", hits=6, n=6)  # n=6 < min_sample=20
        table = eff.refresh(_now_ms())
        assert "背离" in table
        e = table["背离"]
        assert e.n == 6
        assert e.weight == pytest.approx(1.0)
        assert not e.contrarian
        assert "样本不足" in e.note

    def test_exactly_min_sample_triggers_evaluation(
        self, store: _FakeStore
    ) -> None:
        """n == min_sample 时应进入统计评估（不再标为样本不足）。"""
        eff = SignalEfficacy(store, min_sample=10)
        # 10 records，全部命中
        _insert_n_hits(store, kind="超级", hits=10, n=10)
        table = eff.refresh(_now_ms())
        assert "超级" in table
        e = table["超级"]
        assert e.n == 10
        assert "样本不足" not in e.note, "n==min_sample 时不应标样本不足"


class TestRefreshNeutralZone:
    def test_ambiguous_hit_rate_neutral(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """命中率~50%（CI 跨越 0.5）→ weight=1.0, contrarian=False。

        20/40 = 50%。Wilson CI for 20/40 跨越 0.5 → 无统计显著性 → 中性权重。
        """
        _insert_n_hits(store, kind="前瞻", hits=20, n=40)
        table = eff.refresh(_now_ms())
        assert "前瞻" in table
        e = table["前瞻"]
        assert e.n == 40
        # 50% 命中率 CI 跨越 0.5，无统计显著性
        assert e.weight == pytest.approx(1.0), f"50%命中率应中性权重，实际 {e.weight:.3f}"
        assert not e.contrarian


class TestRefreshLookback:
    def test_old_predictions_excluded_by_lookback(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """lookback 窗口外的已评估预测不应计入。"""
        old_ts = 1_000  # 极早时间戳（1970年初）
        store.conn.execute(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?,?,?,?,?,1)",
            (old_ts, "共识", "long", 0.05, 1),
        )
        now = _now_ms()
        # lookback_ms=1000ms → old_ts 在窗口外（now - 1000 > 1000）
        table = eff.refresh(now, lookback_ms=1000)
        assert "共识" not in table, "超出 lookback 的记录不应计入"

    def test_recent_predictions_included(
        self, store: _FakeStore
    ) -> None:
        """窗口内的已评估预测应计入。"""
        eff = SignalEfficacy(store, min_sample=1)
        now = _now_ms()
        store.conn.execute(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?,?,?,?,?,1)",
            (now - 100, "共识", "long", 0.05, 1),
        )
        table = eff.refresh(now, lookback_ms=10_000)
        assert "共识" in table

    def test_null_correct_excluded(
        self, store: _FakeStore
    ) -> None:
        """correct IS NULL 的记录不应计入（评估未完成，correct 未写入）。"""
        eff = SignalEfficacy(store, min_sample=1)
        now = _now_ms()
        store.conn.execute(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?,?,?,?,?,1)",
            (now - 100, "共识", "long", 0.05, None),  # correct = NULL
        )
        table = eff.refresh(now, lookback_ms=10_000)
        assert "共识" not in table, "correct IS NULL 的记录不应计入"


# ================================================================
# _RET_OUTLIER 离群值过滤
# ================================================================

class TestOutlierFilter:
    def test_outlier_rows_excluded(self, store: _FakeStore) -> None:
        """|realized_ret| > 10.0 (=1000%) 的行应被过滤，不计入统计。"""
        eff = SignalEfficacy(store, min_sample=1)
        now = _now_ms()
        # 插入 1 条正常行 + 1 条离群行
        store.conn.executemany(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?,?,?,?,?,1)",
            [
                (now - 200, "共识", "long", 0.05, 1),    # 正常：realized_ret=5%
                (now - 100, "共识", "long", 15.0, 1),    # 离群：|ret|=15>10 应过滤
            ],
        )
        table = eff.refresh(now, lookback_ms=10_000)
        assert "共识" in table
        e = table["共识"]
        # 只有 1 条正常行计入
        assert e.n == 1, f"离群行应被过滤，期望 n=1，实际 {e.n}"
        assert e.hits == 1

    def test_negative_outlier_excluded(self, store: _FakeStore) -> None:
        """realized_ret = -15.0 同样被过滤（负向离群）。"""
        eff = SignalEfficacy(store, min_sample=1)
        now = _now_ms()
        store.conn.executemany(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?,?,?,?,?,1)",
            [
                (now - 200, "跟庄", "short", -0.02, 0),  # 正常
                (now - 100, "跟庄", "short", -15.0, 0),  # 离群负向
            ],
        )
        table = eff.refresh(now, lookback_ms=10_000)
        assert "跟庄" in table
        e = table["跟庄"]
        assert e.n == 1, f"负向离群行应被过滤，期望 n=1，实际 {e.n}"


# ================================================================
# weight_of / label_of / is_contrarian
# ================================================================

class TestWeightOf:
    def test_unknown_kind_returns_one(self, eff: SignalEfficacy) -> None:
        """未刷新/无记录 kind → weight_of 返回 1.0（安全默认）。"""
        assert eff.weight_of("不存在的kind") == pytest.approx(1.0)

    def test_before_refresh_returns_one(self, eff: SignalEfficacy) -> None:
        """刷新前任何 kind 均返回 1.0。"""
        assert eff.weight_of("共识") == pytest.approx(1.0)

    def test_after_refresh_high_hit_returns_above_1(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """刷新后高命中 kind 的 weight_of 应 > 1.0。"""
        _insert_n_hits(store, kind="共识", hits=72, n=72)
        eff.refresh(_now_ms())
        w = eff.weight_of("共识")
        assert w > 1.0, f"期望 >1，实际 {w}"

    def test_after_refresh_contrarian_returns_below_1(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """刷新后反指 kind 的 weight_of 应 < 1.0。"""
        _insert_n_hits(store, kind="跟庄", hits=0, n=20)
        eff.refresh(_now_ms())
        w = eff.weight_of("跟庄")
        assert w < 1.0, f"期望 <1，实际 {w}"


class TestLabelOf:
    def test_no_record_returns_empty(self, eff: SignalEfficacy) -> None:
        """无记录时 label_of 返回空串，不影响原消息。"""
        assert eff.label_of("跟庄") == ""

    def test_normal_label_contains_kind_and_hit_rate(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """高命中率 kind → label 含 kind 名和命中率。"""
        _insert_n_hits(store, kind="共识", hits=72, n=72)
        eff.refresh(_now_ms())
        label = eff.label_of("共识")
        assert "共识" in label
        assert "100%" in label  # 72/72 → 100%
        assert "命中" in label

    def test_contrarian_label_has_warning(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """反指 kind → label 含 '⚠️' 和 kind 名。"""
        _insert_n_hits(store, kind="跟庄", hits=0, n=20)
        eff.refresh(_now_ms())
        label = eff.label_of("跟庄")
        assert "⚠️" in label
        assert "跟庄" in label

    def test_empty_label_for_n0_kind(self, eff: SignalEfficacy) -> None:
        """n=0 时 label_of 返回空串（兜底）。"""
        eff._table["测试"] = KindEfficacy(
            kind="测试", n=0, hits=0, hit_rate=0.0,
            lower=0.0, upper=1.0, weight=1.0, contrarian=False, note="")
        assert eff.label_of("测试") == ""

    def test_label_empty_string_doesnt_break_concat(self, eff: SignalEfficacy) -> None:
        """label_of 返回空串时，消息拼接不变（f'{msg}' + '' == msg）。"""
        msg = "测试消息"
        result = msg + eff.label_of("不存在")
        assert result == msg


class TestIsContrarian:
    def test_unknown_kind_not_contrarian(self, eff: SignalEfficacy) -> None:
        """未知 kind → is_contrarian 返回 False（保守）。"""
        assert eff.is_contrarian("不存在") is False

    def test_low_hit_large_sample_kind_is_contrarian(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """低命中率（0%, n=20）→ is_contrarian=True。"""
        _insert_n_hits(store, kind="跟庄", hits=0, n=20)
        eff.refresh(_now_ms())
        assert eff.is_contrarian("跟庄") is True

    def test_high_hit_kind_not_contrarian(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """高命中率 → is_contrarian=False。"""
        _insert_n_hits(store, kind="共识", hits=72, n=72)
        eff.refresh(_now_ms())
        assert eff.is_contrarian("共识") is False


# ================================================================
# fmt：多行摘要输出
# ================================================================

class TestFmt:
    def test_empty_table_message(self, eff: SignalEfficacy) -> None:
        """_table 为空时 fmt 返回友好提示。"""
        result = eff.fmt()
        assert "无已评估预测数据" in result

    def test_fmt_contains_kind_and_numbers(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """有数据时 fmt 应含 kind 名、命中数/总数、权重标识。"""
        _insert_n_hits(store, kind="共识", hits=72, n=72)
        eff.refresh(_now_ms())
        result = eff.fmt()
        assert "共识" in result
        assert "72" in result
        # 权重 > 1.0，应显示加权标识
        assert "加权" in result or "↑" in result
        # 命中字样（不含"中性"前缀，改为直接 correct 语义）
        assert "命中" in result

    def test_fmt_contrarian_shows_warning(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """反指 kind 在 fmt 中应有警告标识。"""
        _insert_n_hits(store, kind="跟庄", hits=0, n=20)
        eff.refresh(_now_ms())
        result = eff.fmt()
        assert "跟庄" in result
        assert "⚠️" in result or "反指" in result

    def test_fmt_shows_multiple_kinds(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """多 kind 时 fmt 应包含各 kind 信息。"""
        _insert_n_hits(store, kind="共识", hits=72, n=72)
        _insert_n_hits(store, kind="跟庄", hits=0, n=20,
                       ts_base=_now_ms() - 100_000)
        eff.refresh(_now_ms())
        result = eff.fmt()
        assert "共识" in result
        assert "跟庄" in result


# ================================================================
# 多 kind 并存 + 无 correct 记录
# ================================================================

class TestMultiKind:
    def test_multiple_kinds_independent(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """多 kind 各自独立评估，互不影响。"""
        _insert_n_hits(store, kind="共识", hits=30, n=30)  # n=30，高命中
        _insert_n_hits(store, kind="背离", hits=2, n=4,    # n=4 < min_sample
                       ts_base=_now_ms() - 50_000)
        table = eff.refresh(_now_ms())
        assert "共识" in table
        assert "背离" in table
        # 小样本背离 → 中性
        assert table["背离"].weight == pytest.approx(1.0)
        assert not table["背离"].contrarian

    def test_no_evaluated_rows_returns_empty(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """无 evaluated=1 且 correct IS NOT NULL 的行时 refresh 返回空 dict。"""
        store.conn.execute(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(1000,'共识','long',0.05,1,0)"  # evaluated=0，不计入
        )
        table = eff.refresh(_now_ms())
        assert table == {}
        assert eff.fmt() == "(无已评估预测数据)"


# ================================================================
# weight 上下限 cap 验证
# ================================================================

class TestWeightCap:
    def test_weight_capped_at_1_5(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """即使命中率极高（100%，n=100），weight 不超过 1.5。"""
        _insert_n_hits(store, kind="共识", hits=100, n=100)
        table = eff.refresh(_now_ms())
        assert "共识" in table
        assert table["共识"].weight <= 1.5

    def test_weight_floored_at_0_3(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """即使命中率极低（0%，n=100），weight 不低于 0.3。"""
        _insert_n_hits(store, kind="跟庄", hits=0, n=100)
        table = eff.refresh(_now_ms())
        assert "跟庄" in table
        assert table["跟庄"].weight >= 0.3


# ================================================================
# 【核心修复验证】MTF 7-TF 场景不再错贴 contrarian
#
# 旧 market_neutral_stats 在 MTF 场景下的 bug：
#   - 同一 kind 有 7 条 MTF 记录（5m/15m/30m/1h/4h/12h/1d），共享同一 ts → 同一 1h bucket
#   - 在牛市收益单调递增时，bucket_mean = 7 条的均值
#   - 仅 3 条超过均值 → hit_rate≈3/7=42.9% → contrarian=True（错误！信号实际全对）
#
# 新实现直接读 correct 字段，此场景 7/7 correct=1 → hit_rate=100% → 加权（正确）
# ================================================================

class TestMTFScenarioFix:
    """验证 MTF 7-TF 场景修复：同 ts 7 条 MTF 记录，correct=1，不再误判 contrarian。"""

    def test_mtf_7tf_same_ts_all_correct_not_contrarian(
        self, store: _FakeStore
    ) -> None:
        """50 次信号 × 7 TF = 350 条，全部 correct=1，应加权不应反指。

        旧 market_neutral_stats：7 TF 共享同一 ts → bucket_mean = 7 TF 收益均值
        → 在牛市单调递增场景只有 3/7 超过均值 → hit_rate=42.9% → contrarian=True（错误）

        新实现用 correct 字段：350/350 correct=1 → hit_rate=100% → 加权（正确）
        """
        eff = SignalEfficacy(store, min_sample=20)
        now = _now_ms()
        # 7 个 TF 的典型牛市收益（单调递增，模拟真实 MTF 预测）
        tf_rets = [0.01, 0.015, 0.02, 0.025, 0.04, 0.06, 0.08]
        rows = []
        for signal_idx in range(50):  # 50 次信号
            ts = now - 1000 + signal_idx * 3_600_000  # 每信号 1h 间隔
            for ret in tf_rets:
                rows.append((ts, "前瞻", "long", ret, 1))  # correct=1，全对

        store.conn.executemany(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?, ?, ?, ?, ?, 1)",
            rows,
        )

        table = eff.refresh(now, lookback_ms=999_999_999)  # 宽 lookback 覆盖所有行
        assert "前瞻" in table
        e = table["前瞻"]

        assert e.n == 350, f"期望 n=350，实际 {e.n}"
        assert e.hits == 350, f"期望 hits=350，实际 {e.hits}"
        assert e.hit_rate == pytest.approx(1.0, abs=0.001)

        # 核心：不应被错标为 contrarian
        assert not e.contrarian, (
            f"MTF 7-TF 全 correct 场景不应被标为 contrarian！"
            f"lower={e.lower:.3f}, upper={e.upper:.3f}, weight={e.weight:.3f}"
        )
        assert e.weight > 1.0, f"全命中应加权，weight={e.weight:.3f}"

    def test_mtf_7tf_same_ts_monotone_all_correct_old_would_misfire(
        self, store: _FakeStore
    ) -> None:
        """证明旧算法 market_neutral_stats 在此场景会误判（用纯函数验证 bug 存在）。

        用 review.market_neutral_stats 直接测试：7 TF 同 ts，收益单调递增
        → market_neutral 命中率 ≈ 42.9%（3/7）→ Wilson upper < 0.5 → 旧算法 contrarian。
        新实现不用此函数，所以不会误判。此测试记录旧 bug 的复现证据。
        """
        from smc_tracker.review import market_neutral_stats

        tf_rets = [0.01, 0.015, 0.02, 0.025, 0.04, 0.06, 0.08]
        # 单次信号，7 TF 共享同一 ts
        records = [(1_000 * 3_600_000 + i * 3_600_000, "long", r)
                   for i in range(50)
                   for r in tf_rets]
        mn = market_neutral_stats(records)

        # 旧算法在此场景的输出：hit_rate ≈ 3/7 ≈ 42.9%
        assert abs(mn["hit_rate"] - 3 / 7) < 0.01, (
            f"旧 market_neutral_stats 在 MTF 场景应输出 hit_rate≈{3/7:.4f}，"
            f"实际 {mn['hit_rate']:.4f}"
        )
        # 确认旧算法 Wilson upper < 0.5（会触发 contrarian）
        from smc_tracker.signals.efficacy import wilson_interval
        lo, hi = wilson_interval(mn["hits"], mn["n"])
        assert hi < 0.5, (
            f"旧算法 Wilson 上界 {hi:.4f} 应 < 0.5，触发 contrarian —— 旧 bug 确认"
        )
