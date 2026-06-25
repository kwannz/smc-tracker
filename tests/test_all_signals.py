"""collect_all_signals 聚合 helper 单元测试。

覆盖：
  - 11 张信号表各插 1-2 行样本 → collect_all_signals 能正确读取并归一化
  - 统一行字段：type, type_label, coin, direction, ts, price, score, evidence, evidence_text
  - evidence_text 非空（包含该类型关键证据）
  - 结果按 ts 倒序排列
  - 表不存在时优雅跳过（不抛异常）

全部使用合成数据，不联网，不依赖外部服务。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from smc_tracker.signals.all_signals import collect_all_signals


# ---- 轻量 Store 存根 ----

class _FakeStore:
    """模拟 Store：仅暴露 conn，不建任何真实 Schema（按需建表）。"""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)


@pytest.fixture()
def store() -> _FakeStore:
    return _FakeStore()


# 固定时间戳基准，方便验证倒序
_T0 = int(time.time() * 1000)
_T1 = _T0 - 60_000   # 1 分钟前
_T2 = _T0 - 120_000  # 2 分钟前
_T3 = _T0 - 180_000  # 3 分钟前
_T4 = _T0 - 240_000  # 4 分钟前
_T5 = _T0 - 300_000  # 5 分钟前

SINCE = _T0 - 600_000  # 10 分钟窗口，所有样本都在内


# ---- 表建立辅助 ----

def _exec(store: _FakeStore, sql: str) -> None:
    store.conn.executescript(sql)


def _setup_signals(store: _FakeStore) -> None:
    """建 signals 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS signals (
            ts INTEGER, coin TEXT, direction TEXT, score REAL,
            structure_bias REAL, flow_bias REAL, flow_net_usd REAL,
            oi_change_pct REAL, onchain_usd REAL,
            entry REAL, stop REAL, target REAL, rr REAL, reason TEXT
        );
    """)
    store.conn.execute(
        "INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_T0, "BTC", "long", 0.8, 0.5, 0.6, 100000.0, 0.02, 50000.0,
         45000.0, 44000.0, 47000.0, 2.0, "SMC共振多头"),
    )


def _setup_divergence(store: _FakeStore) -> None:
    """建 divergence 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS divergence (
            ts INTEGER, coin TEXT, direction TEXT, score REAL,
            funding REAL, oi_change_pct REAL, dex_flow_usd REAL, reason TEXT
        );
    """)
    store.conn.execute(
        "INSERT INTO divergence VALUES(?,?,?,?,?,?,?,?)",
        (_T1, "ETH", "bullish", 0.7, -0.001, 0.03, 80000.0, "CEX资金费极负背离"),
    )


def _setup_whale_signals(store: _FakeStore) -> None:
    """建 whale_signals 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS whale_signals (
            ts INTEGER, address TEXT, label TEXT, coin TEXT,
            action TEXT, direction TEXT, notional REAL, px REAL,
            pos_after REAL, taker INTEGER
        );
    """)
    store.conn.execute(
        "INSERT INTO whale_signals VALUES(?,?,?,?,?,?,?,?,?,?)",
        (_T2, "0xabc123", "鲸鱼A", "SOL", "OPEN", "long",
         30000.0, 180.5, 30000.0, 1),
    )


def _setup_position_changes(store: _FakeStore) -> None:
    """建 position_changes 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS position_changes (
            ts INTEGER, address TEXT, label TEXT, coin TEXT,
            kind TEXT, direction TEXT, prev_notional REAL, new_notional REAL
        );
    """)
    store.conn.execute(
        "INSERT INTO position_changes VALUES(?,?,?,?,?,?,?,?)",
        (_T3, "0xdef456", "鲸鱼B", "BNB", "reversal", "short", 50000.0, 45000.0),
    )


def _setup_consensus(store: _FakeStore) -> None:
    """建 consensus 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS consensus (
            ts INTEGER, coin TEXT, direction TEXT,
            n_agree INTEGER, n_oppose INTEGER,
            net_notional REAL, score REAL, labels TEXT
        );
    """)
    store.conn.execute(
        "INSERT INTO consensus VALUES(?,?,?,?,?,?,?,?)",
        (_T0, "BTC", "long", 6, 1, 130000000.0, 0.9, "庄A,庄B,庄C"),
    )


def _setup_confluence(store: _FakeStore) -> None:
    """建 confluence_signals 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS confluence_signals (
            ts INTEGER, coin TEXT, direction TEXT,
            n_sources INTEGER, sources TEXT, opposing INTEGER, score REAL
        );
    """)
    store.conn.execute(
        "INSERT INTO confluence_signals VALUES(?,?,?,?,?,?,?)",
        (_T4, "ETH", "short", 4, "SMC,OKX,divergence,consensus", 0, 0.85),
    )


def _setup_flagged(store: _FakeStore) -> None:
    """建 flagged_addresses 表并插样本行（无 ts 列，用 last_seen_ms）。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS flagged_addresses (
            address TEXT PRIMARY KEY,
            first_seen_ms INTEGER,
            coin TEXT, reason TEXT,
            net_usd REAL, promoted INTEGER,
            last_seen_ms INTEGER
        );
    """)
    store.conn.execute(
        "INSERT INTO flagged_addresses VALUES(?,?,?,?,?,?,?)",
        ("0xsuspect1", _T5, "DOGE",
         "快速建仓可疑", 20000.0, 0, _T0),
    )


def _setup_flow_predictions(store: _FakeStore) -> None:
    """建 flow_predictions 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS flow_predictions (
            ts INTEGER, coin TEXT, direction TEXT,
            score REAL, vel REAL, accel REAL, book_imb REAL
        );
    """)
    store.conn.execute(
        "INSERT INTO flow_predictions VALUES(?,?,?,?,?,?,?)",
        (_T1, "BTC", "long", 0.75, 12000.0, 800.0, 0.3),
    )


def _setup_okx_signals(store: _FakeStore) -> None:
    """建 okx_signals 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS okx_signals (
            ts INTEGER, coin TEXT, direction TEXT,
            kind TEXT, funding REAL, net_flow REAL
        );
    """)
    store.conn.execute(
        "INSERT INTO okx_signals VALUES(?,?,?,?,?,?)",
        (_T3, "ETH", "long", "accumulation", -0.0005, 250000.0),
    )


def _setup_orderbook_walls(store: _FakeStore) -> None:
    """建 hl_orderbook_walls 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS hl_orderbook_walls (
            ts INTEGER, coin TEXT, side TEXT, kind TEXT,
            px REAL, notional REAL
        );
    """)
    store.conn.execute(
        "INSERT INTO hl_orderbook_walls VALUES(?,?,?,?,?,?)",
        (_T2, "SOL", "bid", "build", 175.0, 500000.0),
    )


def _setup_harmonic(store: _FakeStore) -> None:
    """建 harmonic_setups 表并插样本行。"""
    _exec(store, """
        CREATE TABLE IF NOT EXISTS harmonic_setups (
            ts INTEGER, coin TEXT, tf TEXT, kind TEXT,
            pattern TEXT, direction TEXT, price REAL,
            entry_lo REAL, entry_hi REAL, stop REAL,
            target1 REAL, target2 REAL, rr REAL,
            confidence REAL, knn TEXT, orderflow TEXT, fib_note TEXT,
            prz_lo REAL, prz_hi REAL
        );
    """)
    store.conn.execute(
        "INSERT INTO harmonic_setups(ts,coin,tf,kind,pattern,direction,price,"
        "entry_lo,entry_hi,stop,target1,target2,rr,confidence,knn,orderflow,"
        "fib_note,prz_lo,prz_hi) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_T4, "BTC", "1h", "completed", "Gartley", "long", 45050.0,
         45000.0, 45100.0, 44500.0, 46000.0, 47000.0, 2.0,
         0.87, "✓", "✓bid5k", "XA=0.618", 45000.0, 45100.0),
    )


def _setup_all(store: _FakeStore) -> None:
    """建立所有 11 张表并插入样本行。"""
    _setup_signals(store)
    _setup_divergence(store)
    _setup_whale_signals(store)
    _setup_position_changes(store)
    _setup_consensus(store)
    _setup_confluence(store)
    _setup_flagged(store)
    _setup_flow_predictions(store)
    _setup_okx_signals(store)
    _setup_orderbook_walls(store)
    _setup_harmonic(store)


# ---- 核心测试 ----

def test_collect_all_returns_list(store: _FakeStore) -> None:
    """即使所有表都不存在，collect_all_signals 也应返回空列表而不抛。"""
    result = collect_all_signals(store, SINCE, _T0)
    assert isinstance(result, list)


def test_collect_all_11_types(store: _FakeStore) -> None:
    """11 张表都有数据时，返回结果应覆盖全部 11 种 type。"""
    _setup_all(store)
    result = collect_all_signals(store, SINCE, _T0)
    found_types = {r["type"] for r in result}
    expected_types = {
        "signal", "divergence", "whale_signal", "position_change",
        "consensus", "confluence", "flagged_address",
        "flow_prediction", "okx_signal", "orderbook_wall", "harmonic_setup",
    }
    assert expected_types == found_types, (
        f"缺少类型: {expected_types - found_types}; 多余类型: {found_types - expected_types}"
    )


def test_unified_fields_present(store: _FakeStore) -> None:
    """每行都应有统一字段。"""
    _setup_all(store)
    result = collect_all_signals(store, SINCE, _T0)
    assert result, "结果不应为空"
    for row in result:
        for field in ("type", "type_label", "coin", "direction", "ts",
                      "price", "score", "evidence", "evidence_text"):
            assert field in row, f"行 {row.get('type')} 缺少字段 {field!r}"


def test_evidence_text_nonempty(store: _FakeStore) -> None:
    """evidence_text 对每行都应为非空字符串。"""
    _setup_all(store)
    result = collect_all_signals(store, SINCE, _T0)
    for row in result:
        assert isinstance(row["evidence_text"], str) and row["evidence_text"].strip(), (
            f"type={row['type']} evidence_text 为空"
        )


def test_sorted_by_ts_desc(store: _FakeStore) -> None:
    """结果应按 ts 倒序排列（最新在前）。"""
    _setup_all(store)
    result = collect_all_signals(store, SINCE, _T0)
    ts_list = [r["ts"] for r in result]
    assert ts_list == sorted(ts_list, reverse=True), "结果未按 ts 倒序排列"


def test_evidence_is_dict(store: _FakeStore) -> None:
    """evidence 字段应为 dict（专属证据字典）。"""
    _setup_all(store)
    result = collect_all_signals(store, SINCE, _T0)
    for row in result:
        assert isinstance(row["evidence"], dict), (
            f"type={row['type']} evidence 不是 dict，实际为 {type(row['evidence'])}"
        )


def test_missing_tables_skip_gracefully(store: _FakeStore) -> None:
    """只建部分表，其余不存在，应正常返回（不抛 OperationalError）。"""
    # 只建 signals 和 whale_signals 两张
    _setup_signals(store)
    _setup_whale_signals(store)
    result = collect_all_signals(store, SINCE, _T0)
    found_types = {r["type"] for r in result}
    assert "signal" in found_types
    assert "whale_signal" in found_types
    # 其他类型表不存在，不应在结果中出现，且不抛出异常


def test_ts_window_filter(store: _FakeStore) -> None:
    """since_ms 应起到时间窗口过滤作用。"""
    _setup_signals(store)
    # 查询从 _T0 + 1 开始（比最新样本还新）→ 应无结果
    result = collect_all_signals(store, _T0 + 1, _T0 + 60_000)
    assert result == [], "时间窗口外的行不应被返回"


def test_coin_field_correct(store: _FakeStore) -> None:
    """coin 字段应正确映射。"""
    _setup_signals(store)
    result = collect_all_signals(store, SINCE, _T0)
    sig_rows = [r for r in result if r["type"] == "signal"]
    assert sig_rows, "signals 应有结果"
    assert sig_rows[0]["coin"] == "BTC"


def test_harmonic_evidence_text(store: _FakeStore) -> None:
    """谐波行的 evidence_text 应包含 pattern 和 PRZ 价格信息。"""
    _setup_harmonic(store)
    result = collect_all_signals(store, SINCE, _T0)
    harm = [r for r in result if r["type"] == "harmonic_setup"]
    assert harm, "谐波应有结果"
    text = harm[0]["evidence_text"]
    assert "Gartley" in text or "PRZ" in text or "45000" in text, (
        f"谐波 evidence_text 缺少关键信息: {text!r}"
    )


def test_whale_evidence_text(store: _FakeStore) -> None:
    """跟庄行的 evidence_text 应包含地址和名义金额。"""
    _setup_whale_signals(store)
    result = collect_all_signals(store, SINCE, _T0)
    whale = [r for r in result if r["type"] == "whale_signal"]
    assert whale, "whale_signals 应有结果"
    text = whale[0]["evidence_text"]
    assert "0xabc123" in text or "30000" in text or "鲸鱼A" in text, (
        f"跟庄 evidence_text 缺少关键信息: {text!r}"
    )


def test_consensus_evidence_text(store: _FakeStore) -> None:
    """共识行的 evidence_text 应包含庄家数和净名义金额。"""
    _setup_consensus(store)
    result = collect_all_signals(store, SINCE, _T0)
    cons = [r for r in result if r["type"] == "consensus"]
    assert cons, "consensus 应有结果"
    text = cons[0]["evidence_text"]
    # 应含庄家人数或净名义信息
    assert any(c in text for c in ("6", "1.3", "130", "庄", "agree")), (
        f"共识 evidence_text 缺少关键信息: {text!r}"
    )


def test_type_label_chinese(store: _FakeStore) -> None:
    """type_label 应为中文描述。"""
    _setup_all(store)
    result = collect_all_signals(store, SINCE, _T0)
    for row in result:
        label = row["type_label"]
        assert isinstance(label, str) and label, f"type={row['type']} type_label 为空"
        # 中文标签应包含至少一个中文字符
        has_chinese = any("一" <= c <= "鿿" for c in label)
        assert has_chinese, f"type={row['type']} type_label={label!r} 不含中文"
