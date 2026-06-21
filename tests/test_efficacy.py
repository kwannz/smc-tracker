"""信号有效性自适应加权（market-neutral 版）单元测试。

覆盖：
  - wilson_interval：数学正确性（已知答案）
  - SignalEfficacy.refresh：用市场中性命中率（横截面去均值）驱动 Wilson 加权
    - 高市场中性命中大样本→加权；低市场中性大样本→反指降权；小样本→中性
  - weight_of / label_of / is_contrarian：行为验证
  - fmt：含关键数字的格式输出
  - Beta 污染核心测试：raw correct 高但中性低→应降权；raw correct 低但中性高→应加权
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

def _insert_mn_records(
    store: _FakeStore,
    *,
    kind: str,
    records: list[tuple[str, float, int]],
    ts_base: int | None = None,
    bucket_size_ms: int = 3_600_000,
) -> None:
    """插入 evaluated=1 的合成预测行，用于市场中性命中率测试。

    records: [(direction, realized_ret, correct), ...]
      direction: 'long' 或 'short'
      realized_ret: 实际收益率（已有值，IS NOT NULL，驱动市场中性计算）
      correct: 0 或 1（原始方向命中，仅供旧逻辑参考，新逻辑不用）
    ts_base: 起始时间戳 ms（默认 now-1000ms，在 lookback 窗口内）
    bucket_size_ms: 每条记录的桶偏移量（不同值放入不同时间桶）

    注意：若要多条记录在同一时间桶内做横截面，传相同 ts 即可。
    本函数按 index 递增 bucket_size_ms，若需同桶，调用方分开传 ts_base。
    """
    if ts_base is None:
        ts_base = _now_ms() - 1000  # 1 秒前，确保在 lookback 窗口内
    rows = [
        (ts_base + i * bucket_size_ms, kind, direction, realized_ret, correct)
        for i, (direction, realized_ret, correct) in enumerate(records)
    ]
    store.conn.executemany(
        "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
        " VALUES(?, ?, ?, ?, ?, 1)",
        rows,
    )


def _insert_paired_buckets(
    store: _FakeStore,
    *,
    kind: str,
    n_good_pairs: int,
    n_bad_pairs: int,
    ts_base: int | None = None,
) -> None:
    """插入配对时间桶数据，每个桶有 2 条记录：good pair 全命中，bad pair 零命中。

    good pair（同一桶）：long(+0.10) + short(-0.05)
      bucket_mean = 0.025; long excess=0.075>0→命中; short excess=-0.075<0→命中
    bad pair（同一桶）：long(-0.02) + short(+0.06)
      bucket_mean = 0.02; long excess=-0.04<0→不命中; short excess=0.04>0→不命中(short需<0)

    参数
    ----
    n_good_pairs : "加权"桶数，每桶 2 条全命中
    n_bad_pairs  : "反指"桶数，每桶 2 条零命中
    ts_base      : 起始时间戳；每桶间隔 7_200_000ms（2小时，确保桶分离）
    """
    if ts_base is None:
        ts_base = _now_ms() - 1000
    BUCKET_STEP = 7_200_000  # 2小时间隔，确保每对都在独立时间桶
    rows: list[tuple] = []
    idx = 0
    for _ in range(n_good_pairs):
        ts = ts_base + idx * BUCKET_STEP
        rows.append((ts, kind, "long", 0.10, 1))   # long 命中
        rows.append((ts, kind, "short", -0.05, 1)) # short 命中
        idx += 1
    for _ in range(n_bad_pairs):
        ts = ts_base + idx * BUCKET_STEP
        rows.append((ts, kind, "long", -0.02, 0))  # long 不命中
        rows.append((ts, kind, "short", 0.06, 0))  # short 不命中
        idx += 1
    store.conn.executemany(
        "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
        " VALUES(?, ?, ?, ?, ?, 1)",
        rows,
    )


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
# SignalEfficacy.refresh：权重/contrarian 分类逻辑（市场中性语义）
# ================================================================

class TestRefreshHighHitRate:
    def test_high_mn_hit_large_sample_gets_weight_above_1(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """高市场中性命中率 + 大样本（36 good pairs / 72 条）→ weight > 1, contrarian=False。

        每对在同一桶：long(+0.10)+short(-0.05)，桶均值=0.025。
        long excess=0.075>0（命中）；short excess=-0.075<0（命中）。
        72/72 = 100% 市场中性命中 → Wilson 下界 > 0.5 → 加权。
        """
        _insert_paired_buckets(store, kind="共识", n_good_pairs=36, n_bad_pairs=0)
        table = eff.refresh(_now_ms())
        assert "共识" in table, "共识 kind 应出现在结果中"
        e = table["共识"]
        assert e.n == 72
        assert e.hits == 72
        assert e.weight > 1.0, f"期望加权>1，实际 {e.weight:.3f}"
        assert not e.contrarian
        assert e.weight <= 1.5  # 上限 cap


class TestRefreshLowHitRate:
    def test_low_mn_hit_large_sample_is_contrarian(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """低市场中性命中率 + 大样本（10 bad pairs / 20 条）→ contrarian=True, weight < 1。

        每对在同一桶：long(-0.02)+short(+0.06)，桶均值=0.02。
        long excess=-0.04<0（不命中）；short excess=0.04>0（不命中，short 需负超额）。
        0/20 = 0% 市场中性命中 → Wilson 上界 < 0.5 → 反指降权。
        """
        _insert_paired_buckets(store, kind="跟庄", n_good_pairs=0, n_bad_pairs=10)
        table = eff.refresh(_now_ms())
        assert "跟庄" in table
        e = table["跟庄"]
        assert e.n == 20
        assert e.contrarian, (
            f"市场中性命中0%，Wilson上界<0.5，应标为反指。"
            f"lower={e.lower:.3f}, upper={e.upper:.3f}"
        )
        assert e.weight < 1.0
        assert e.weight >= 0.3  # 下限 cap


class TestRefreshSmallSample:
    def test_small_sample_is_neutral(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """小样本（n=6 < min_sample=20）→ weight=1.0, contrarian=False，note 含'样本不足'。"""
        _insert_paired_buckets(store, kind="背离", n_good_pairs=3, n_bad_pairs=0)  # n=6
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
        # 5 good pairs = 10 records，全部市场中性命中
        _insert_paired_buckets(store, kind="超级", n_good_pairs=5, n_bad_pairs=0)
        table = eff.refresh(_now_ms())
        assert "超级" in table
        e = table["超级"]
        assert e.n == 10
        assert "样本不足" not in e.note, "n==min_sample 时不应标样本不足"


class TestRefreshNeutralZone:
    def test_ambiguous_mn_hit_rate_neutral(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """市场中性命中率~50%（CI 跨越 0.5）→ weight=1.0, contrarian=False。

        10 good pairs (20 hit) + 10 bad pairs (0 hit) = 40 条, 20/40 = 50%。
        Wilson CI for 20/40 跨越 0.5 → 无统计显著性 → 中性权重。
        """
        _insert_paired_buckets(store, kind="前瞻", n_good_pairs=10, n_bad_pairs=10)
        table = eff.refresh(_now_ms())
        assert "前瞻" in table
        e = table["前瞻"]
        assert e.n == 40
        # 50% 命中率 CI 跨越 0.5，无统计显著性
        assert e.weight == pytest.approx(1.0), f"50%市场中性命中率应中性权重，实际 {e.weight:.3f}"
        assert not e.contrarian


class TestRefreshLookback:
    def test_old_predictions_excluded_by_lookback(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """lookback 窗口外的已评估预测不应计入（即使有 realized_ret）。"""
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
        """窗口内的已评估预测应计入（需 realized_ret IS NOT NULL）。"""
        eff = SignalEfficacy(store, min_sample=1)
        now = _now_ms()
        store.conn.execute(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?,?,?,?,?,1)",
            (now - 100, "共识", "long", 0.05, 1),
        )
        table = eff.refresh(now, lookback_ms=10_000)
        assert "共识" in table

    def test_null_realized_ret_excluded(
        self, store: _FakeStore
    ) -> None:
        """realized_ret IS NULL 的记录不应计入（评估未完成）。"""
        eff = SignalEfficacy(store, min_sample=1)
        now = _now_ms()
        store.conn.execute(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?,?,?,?,?,1)",
            (now - 100, "共识", "long", None, 1),  # realized_ret = NULL
        )
        table = eff.refresh(now, lookback_ms=10_000)
        assert "共识" not in table, "realized_ret IS NULL 的记录不应计入"


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

    def test_after_refresh_high_mn_returns_above_1(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """刷新后高市场中性命中 kind 的 weight_of 应 > 1.0。"""
        _insert_paired_buckets(store, kind="共识", n_good_pairs=36, n_bad_pairs=0)
        eff.refresh(_now_ms())
        w = eff.weight_of("共识")
        assert w > 1.0, f"期望 >1，实际 {w}"

    def test_after_refresh_contrarian_returns_below_1(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """刷新后反指 kind 的 weight_of 应 < 1.0。"""
        _insert_paired_buckets(store, kind="跟庄", n_good_pairs=0, n_bad_pairs=10)
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
        """高市场中性命中率 kind → label 含 kind 名和命中率，以及'中性'字样。"""
        _insert_paired_buckets(store, kind="共识", n_good_pairs=36, n_bad_pairs=0)
        eff.refresh(_now_ms())
        label = eff.label_of("共识")
        assert "共识" in label
        assert "100%" in label  # 72/72 → 100%
        assert "中性" in label  # 新语义：明确标注市场中性

    def test_contrarian_label_has_warning(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """反指 kind → label 含 '⚠️' 和 kind 名。"""
        _insert_paired_buckets(store, kind="跟庄", n_good_pairs=0, n_bad_pairs=10)
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

    def test_low_mn_hit_large_sample_kind_is_contrarian(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """市场中性低命中率（0%, n=20）→ is_contrarian=True。"""
        _insert_paired_buckets(store, kind="跟庄", n_good_pairs=0, n_bad_pairs=10)
        eff.refresh(_now_ms())
        assert eff.is_contrarian("跟庄") is True

    def test_high_mn_hit_kind_not_contrarian(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """市场中性高命中率 → is_contrarian=False。"""
        _insert_paired_buckets(store, kind="共识", n_good_pairs=36, n_bad_pairs=0)
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
        """有数据时 fmt 应含 kind 名、命中数/总数、权重标识、'中性命中'字样。"""
        _insert_paired_buckets(store, kind="共识", n_good_pairs=36, n_bad_pairs=0)
        eff.refresh(_now_ms())
        result = eff.fmt()
        assert "共识" in result
        assert "72" in result
        # 权重 > 1.0，应显示加权标识
        assert "加权" in result or "↑" in result
        # 新语义：体现市场中性
        assert "中性命中" in result

    def test_fmt_contrarian_shows_warning(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """反指 kind 在 fmt 中应有警告标识。"""
        _insert_paired_buckets(store, kind="跟庄", n_good_pairs=0, n_bad_pairs=10)
        eff.refresh(_now_ms())
        result = eff.fmt()
        assert "跟庄" in result
        assert "⚠️" in result or "反指" in result

    def test_fmt_shows_multiple_kinds(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """多 kind 时 fmt 应包含各 kind 信息。"""
        _insert_paired_buckets(store, kind="共识", n_good_pairs=36, n_bad_pairs=0)
        _insert_paired_buckets(
            store, kind="跟庄", n_good_pairs=0, n_bad_pairs=10,
            ts_base=_now_ms() - 100_000,  # 不同 ts_base 避免桶冲突
        )
        eff.refresh(_now_ms())
        result = eff.fmt()
        assert "共识" in result
        assert "跟庄" in result


# ================================================================
# 多 kind 并存 + 无 realized_ret 记录
# ================================================================

class TestMultiKind:
    def test_multiple_kinds_independent(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """多 kind 各自独立评估，互不影响。"""
        _insert_paired_buckets(store, kind="共识", n_good_pairs=15, n_bad_pairs=0)  # n=30，高命中
        _insert_paired_buckets(store, kind="背离", n_good_pairs=1, n_bad_pairs=1,   # n=4 < min_sample
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
        """无 evaluated=1 且 realized_ret IS NOT NULL 的行时 refresh 返回空 dict。"""
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
        """即使市场中性命中率极高（100%，n=100），weight 不超过 1.5。"""
        _insert_paired_buckets(store, kind="共识", n_good_pairs=50, n_bad_pairs=0)
        table = eff.refresh(_now_ms())
        assert "共识" in table
        assert table["共识"].weight <= 1.5

    def test_weight_floored_at_0_3(
        self, eff: SignalEfficacy, store: _FakeStore
    ) -> None:
        """即使市场中性命中率极低（0%，n=100），weight 不低于 0.3。"""
        _insert_paired_buckets(store, kind="跟庄", n_good_pairs=0, n_bad_pairs=50)
        table = eff.refresh(_now_ms())
        assert "跟庄" in table
        assert table["跟庄"].weight >= 0.3


# ================================================================
# 【核心修复验证】Beta 污染测试：证明改用市场中性命中率能纠正旧算法的错误决策
# ================================================================

class TestBetaContaminationFix:
    """验证修复核心：raw correct 高但市场中性低→应降权；raw correct 低但市场中性高→应加权。

    设计场景：
    - "跟庄": raw correct ~80%（大部分预测方向正确），
              但市场中性命中率~25%（在同期币种中表现垫底）
              → 旧算法用 correct 会加权（错误），新算法用市场中性会降权（正确）
    - "共识": raw correct ~25%（大部分预测方向错误，如在跌市做多），
              但市场中性命中率~100%（在同期币种中超额显著）
              → 旧算法用 correct 会降权（错误），新算法用市场中性会加权（正确）
    """

    def test_high_raw_correct_but_low_mn_is_contrarian(
        self, store: _FakeStore
    ) -> None:
        """跟庄：raw correct 高（多数记录价格朝预测方向走）但市场中性命中率低。

        构造方式：同一桶内 4 条 long 记录，all positive（raw correct=100%），
        但只有最高收益那条超过桶均值（市场中性命中=1/4=25%）。
        跨 5 个桶 → n=20, market_neutral_hits=5 → Wilson upper < 0.5 → contrarian。

        旧算法（用 correct）：correct=1 for 20/20 → 100% → 应会加权 → 错误决策。
        新算法（用市场中性）：25% → contrarian → 降权 → 正确决策。
        """
        eff = SignalEfficacy(store, min_sample=20)
        ts_base = _now_ms() - 1000
        STEP = 7_200_000  # 2小时，确保每组在独立桶

        # 每桶4条 long，returns: [+0.12, +0.03, +0.03, +0.03]
        # mean = 0.0525; 只有 0.12 > 0.0525 → 1/4 市场中性命中
        # raw correct: 4/4 都是正收益 = 100% raw correct
        rows: list[tuple] = []
        for bucket_idx in range(5):  # 5桶 × 4条 = 20条
            ts = ts_base + bucket_idx * STEP
            for ret in [0.12, 0.03, 0.03, 0.03]:
                rows.append((ts, "跟庄", "long", ret, 1))  # correct=1（正收益看多=raw命中）

        store.conn.executemany(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?, ?, ?, ?, ?, 1)",
            rows,
        )

        table = eff.refresh(_now_ms())
        assert "跟庄" in table
        e = table["跟庄"]

        # 验证样本数
        assert e.n == 20, f"期望 n=20，实际 {e.n}"

        # 关键断言：市场中性命中率 = 5/20 = 25%，远低于 50%
        # Wilson 上界 < 0.5 → contrarian
        assert e.contrarian, (
            f"跟庄 raw correct=100% 但市场中性命中率 25%，"
            f"应标为反指(降权)，实际 weight={e.weight:.3f}, "
            f"contrarian={e.contrarian}, CI=[{e.lower:.3f},{e.upper:.3f}]"
        )
        assert e.weight < 1.0, (
            f"beta 污染跟庄应降权(weight<1)，实际 {e.weight:.3f}"
        )

        # 确认市场中性命中率约 25%（5/20）
        assert e.hits == 5, f"期望5个市场中性命中，实际 {e.hits}"
        assert e.hit_rate == pytest.approx(0.25, abs=0.01)

    def test_low_raw_correct_but_high_mn_gets_weight(
        self, store: _FakeStore
    ) -> None:
        """共识：raw correct 低（做多但价格下跌）但市场中性命中率高（跌幅最小）。

        构造方式：同一桶内 4 条 long 记录，均为负收益（raw correct=0%），
        但其中最高收益（跌幅最小）那条超过桶均值（市场中性命中=1/4 per bucket）。
        为达到高市场中性命中，用 good_pairs 模式（long/short paired）产生高命中。

        改用全命中 good_pairs：36桶 × 2条 = 72条，72/72=100%市场中性命中。
        raw correct 分析：long(+0.10)命中，short(-0.05)也命中（raw correct 混合）。
        """
        eff = SignalEfficacy(store, min_sample=20)
        # 用同一桶内 long(+0.10)+short(-0.05) 的配对，100% 市场中性命中
        # 而这些 long 的 correct 字段标为 0（表示旧逻辑认为是"错误"），
        # 验证新逻辑依然能正确判定为加权
        ts_base = _now_ms() - 1000
        STEP = 7_200_000
        rows: list[tuple] = []
        for bucket_idx in range(36):  # 36桶 × 2条 = 72条
            ts = ts_base + bucket_idx * STEP
            # long(+0.10)：旧 correct=0（故意设为"旧算法认为错误"），realized_ret 为正
            rows.append((ts, "共识", "long", 0.10, 0))   # correct=0（旧算法会降权）
            # short(-0.05)：旧 correct=0，realized_ret 为负（价格跌，空头成功）
            rows.append((ts, "共识", "short", -0.05, 0))  # correct=0

        store.conn.executemany(
            "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
            " VALUES(?, ?, ?, ?, ?, 1)",
            rows,
        )

        table = eff.refresh(_now_ms())
        assert "共识" in table
        e = table["共识"]

        # 验证样本数
        assert e.n == 72, f"期望 n=72，实际 {e.n}"

        # 关键断言：市场中性命中率 100%，Wilson 下界 > 0.5 → 加权
        assert not e.contrarian, (
            f"共识 raw correct=0% 但市场中性命中率 100%，"
            f"不应被标为反指，实际 contrarian={e.contrarian}"
        )
        assert e.weight > 1.0, (
            f"市场中性高命中共识应加权(weight>1)，实际 {e.weight:.3f}"
        )
        assert e.hits == 72, f"期望72个市场中性命中，实际 {e.hits}"

    def test_beta_contamination_decision_reversal(
        self, store: _FakeStore
    ) -> None:
        """综合：同一 DB 中两 kind 并存，新算法做出与旧算法完全相反的正确决策。

        "跟庄": raw correct=100%（旧算法→加权），market-neutral=25%（新算法→降权反指）
        "共识": raw correct=0%（旧算法→降权），market-neutral=100%（新算法→加权）

        此测试是对整个修复的端到端证明：
        旧用 correct 的决策与新用市场中性的决策方向完全相反，证明修复纠正了决策。
        """
        eff = SignalEfficacy(store, min_sample=20)
        now = _now_ms()
        STEP = 7_200_000

        # "跟庄"：5桶 × 4条 long，全正收益（raw correct=100%），但25%市场中性命中
        for bi in range(5):
            ts = now - 1000 + bi * STEP
            for ret in [0.12, 0.03, 0.03, 0.03]:
                store.conn.execute(
                    "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
                    " VALUES(?,?,?,?,?,1)",
                    (ts, "跟庄", "long", ret, 1),
                )

        # "共识"：36桶 × 2条 paired，raw correct=0%，但100%市场中性命中
        for bi in range(36):
            ts = now - 2000 + bi * STEP
            store.conn.execute(
                "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
                " VALUES(?,?,?,?,?,1)",
                (ts, "共识", "long", 0.10, 0),
            )
            store.conn.execute(
                "INSERT INTO predictions(ts, kind, direction, realized_ret, correct, evaluated)"
                " VALUES(?,?,?,?,?,1)",
                (ts, "共识", "short", -0.05, 0),
            )

        table = eff.refresh(now)

        # 断言"跟庄"：新算法→反指降权（修复）
        assert "跟庄" in table
        e_gen = table["跟庄"]
        assert e_gen.contrarian, (
            f"跟庄应为反指（市场中性25%），contrarian={e_gen.contrarian}, "
            f"weight={e_gen.weight:.3f}"
        )
        assert e_gen.weight < 1.0, f"跟庄应降权，weight={e_gen.weight:.3f}"

        # 断言"共识"：新算法→加权（修复）
        assert "共识" in table
        e_con = table["共识"]
        assert not e_con.contrarian, (
            f"共识不应为反指（市场中性100%），contrarian={e_con.contrarian}"
        )
        assert e_con.weight > 1.0, f"共识应加权，weight={e_con.weight:.3f}"

        # 打印对比（供调试确认）
        fmt = eff.fmt()
        assert "跟庄" in fmt
        assert "共识" in fmt
