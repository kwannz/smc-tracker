"""单测：build_coin_detail 丰富字段（knn_note/honest_label/prz_proximity/confluence）。

验证要点：
1. setup 含新派生字段（knn_note / honest_label / prz_proximity）
2. 多周期共振检测：有 PRZ 重叠 + 方向一致 → confluence 非空
3. 方向不一致的跨 TF PRZ 重叠 → 不产生共振
4. 无 PRZ 数据 → confluence 为空，不崩
5. 空 setups → 不崩，返回正常结构
6. knn_note 诚实降级（'?'→样本不足说明；'✓'→找到相似态说明）
7. prz_proximity 正确描述价格位置
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store
from smc_tracker.dashboard import (
    build_coin_detail,
    _knn_note_from_flag,
    _prz_proximity,
    _compute_confluence,
    _enrich_setup,
)


# ---------------------------------------------------------------------------
# 辅助：合成 store
# ---------------------------------------------------------------------------

def _make_store(setups: list[tuple]) -> Store:
    """创建含指定谐波 setups 的临时 Store。"""
    d = tempfile.mkdtemp()
    s = Store(Path(d) / "t.db")
    if setups:
        s.insert_harmonic_setups(setups)
    s.conn.commit()
    return s


NOW_MS = 1_700_000_000_000


def _btc_setup_row(
    tf: str,
    kind: str,
    direction: str,
    prz_lo: float | None,
    prz_hi: float | None,
    confidence: float = 0.80,
    knn: str = "✓",
) -> tuple:
    """构建 29 列 BTC setup 行（只设 prz/direction/kind，其余合理填充）。"""
    price = 65000.0
    entry_lo = prz_lo
    entry_hi = prz_hi
    if kind == "completed":
        stop = 63000.0
        target1 = 68000.0
        target2 = 70000.0
        rr = 2.5
    else:
        stop = target1 = target2 = rr = None
    return (
        NOW_MS, "BTC", tf, kind, "Gartley", direction,
        price, entry_lo, entry_hi, stop, target1, target2,
        rr, confidence, knn, "✓ 买压", "XA=0.618", prz_lo, prz_hi,
        None, None, None, None, None, None, None, None, None, None,
    )


# ---------------------------------------------------------------------------
# 1. knn_note 派生
# ---------------------------------------------------------------------------

def test_knn_note_found():
    note = _knn_note_from_flag("✓")
    assert "找到历史相似态" in note
    assert "随机基线" in note


def test_knn_note_not_found():
    note = _knn_note_from_flag("✗")
    assert "无相似态" in note
    assert "随机基线" in note


def test_knn_note_unknown():
    note = _knn_note_from_flag("?")
    assert "不足" in note or "未计算" in note


def test_knn_note_none():
    note = _knn_note_from_flag(None)
    assert isinstance(note, str) and len(note) > 0


# ---------------------------------------------------------------------------
# 2. prz_proximity
# ---------------------------------------------------------------------------

def test_prz_proximity_inside():
    result = _prz_proximity(65000.0, 64000.0, 66000.0)
    assert "PRZ 内" in result or "内" in result


def test_prz_proximity_below():
    result = _prz_proximity(63000.0, 64000.0, 66000.0)
    assert "低于" in result


def test_prz_proximity_above():
    result = _prz_proximity(67000.0, 64000.0, 66000.0)
    assert "高于" in result


def test_prz_proximity_missing():
    assert _prz_proximity(None, 64000.0, 66000.0) == "—"
    assert _prz_proximity(65000.0, None, 66000.0) == "—"
    assert _prz_proximity(65000.0, 64000.0, None) == "—"


# ---------------------------------------------------------------------------
# 3. _compute_confluence — 有重叠
# ---------------------------------------------------------------------------

def test_confluence_detected_same_direction():
    """两个 TF 同方向 PRZ 重叠 → 产生共振。"""
    setups = [
        {"tf": "1H", "direction": "long", "prz_lo": 64000.0, "prz_hi": 65500.0, "kind": "forming"},
        {"tf": "4H", "direction": "long", "prz_lo": 64500.0, "prz_hi": 66000.0, "kind": "completed"},
    ]
    result = _compute_confluence(setups)
    assert len(result) == 1
    r = result[0]
    assert set([r["tf_a"], r["tf_b"]]) == {"1H", "4H"}
    assert r["direction"] == "long"
    assert r["overlap_lo"] == 64500.0
    assert r["overlap_hi"] == 65500.0


def test_confluence_fwd_count_forming():
    """含 forming 周期的共振 fwd_count>0。"""
    setups = [
        {"tf": "1H", "direction": "short", "prz_lo": 66000.0, "prz_hi": 67000.0, "kind": "forming"},
        {"tf": "4H", "direction": "short", "prz_lo": 66200.0, "prz_hi": 67500.0, "kind": "forming"},
    ]
    result = _compute_confluence(setups)
    assert len(result) == 1
    assert result[0]["fwd_count"] == 2


def test_confluence_no_overlap():
    """PRZ 不重叠 → 无共振。"""
    setups = [
        {"tf": "1H", "direction": "long", "prz_lo": 60000.0, "prz_hi": 61000.0, "kind": "forming"},
        {"tf": "4H", "direction": "long", "prz_lo": 65000.0, "prz_hi": 66000.0, "kind": "completed"},
    ]
    result = _compute_confluence(setups)
    assert result == []


def test_confluence_opposite_direction():
    """方向不同（一 long 一 short）→ 不产生共振。"""
    setups = [
        {"tf": "1H", "direction": "long",  "prz_lo": 64000.0, "prz_hi": 66000.0, "kind": "forming"},
        {"tf": "4H", "direction": "short", "prz_lo": 64500.0, "prz_hi": 65500.0, "kind": "forming"},
    ]
    result = _compute_confluence(setups)
    assert result == []


def test_confluence_same_tf_excluded():
    """同 TF 的两个 setups 不计入共振（共振只在不同 TF 之间）。"""
    setups = [
        {"tf": "1H", "direction": "long", "prz_lo": 64000.0, "prz_hi": 66000.0, "kind": "forming"},
        {"tf": "1H", "direction": "long", "prz_lo": 64500.0, "prz_hi": 65500.0, "kind": "completed"},
    ]
    result = _compute_confluence(setups)
    assert result == []


def test_confluence_missing_prz():
    """PRZ 缺失 → 不崩，跳过该 setup。"""
    setups = [
        {"tf": "1H", "direction": "long", "prz_lo": None, "prz_hi": 65000.0, "kind": "forming"},
        {"tf": "4H", "direction": "long", "prz_lo": 64000.0, "prz_hi": 65500.0, "kind": "completed"},
    ]
    result = _compute_confluence(setups)
    assert result == []


def test_confluence_empty_setups():
    """空 setups → 空列表，不崩。"""
    assert _compute_confluence([]) == []


# ---------------------------------------------------------------------------
# 4. _enrich_setup 字段派生
# ---------------------------------------------------------------------------

def test_enrich_setup_completed():
    d = {"kind": "completed", "knn": "✓", "prz_lo": 64000.0, "prz_hi": 66000.0, "price": 65000.0}
    r = _enrich_setup(d, 65000.0)
    assert "回顾型" in r["honest_label"]
    assert "找到历史相似态" in r["knn_note"]
    assert "PRZ 内" in r["prz_proximity"] or "内" in r["prz_proximity"]


def test_enrich_setup_forming():
    d = {"kind": "forming", "knn": "?", "prz_lo": 64000.0, "prz_hi": 66000.0, "price": 63000.0}
    r = _enrich_setup(d, 63000.0)
    assert "前瞻" in r["honest_label"]
    assert "低于" in r["prz_proximity"]


def test_enrich_setup_missing_prz():
    """PRZ 缺失时 prz_proximity='—'，不崩。"""
    d = {"kind": "forming", "knn": "✗", "prz_lo": None, "prz_hi": None, "price": 65000.0}
    r = _enrich_setup(d, 65000.0)
    assert r["prz_proximity"] == "—"


def test_enrich_setup_does_not_mutate_original():
    """_enrich_setup 应返回新 dict，不修改原始 dict。"""
    d = {"kind": "completed", "knn": "✓", "prz_lo": 64000.0, "prz_hi": 66000.0}
    original_keys = set(d.keys())
    r = _enrich_setup(d, 65000.0)
    assert set(d.keys()) == original_keys
    assert "knn_note" not in d
    assert "knn_note" in r


# ---------------------------------------------------------------------------
# 5. build_coin_detail 集成测试
# ---------------------------------------------------------------------------

def test_build_coin_detail_has_confluence_key():
    """build_coin_detail 返回 dict 含 confluence 键。"""
    s = _make_store([])
    result = build_coin_detail(s, "BTC")
    s.close()
    assert "confluence" in result, "缺少 confluence 键"
    assert isinstance(result["confluence"], list)


def test_build_coin_detail_setups_have_knn_note():
    """setups 含 knn_note 字段。"""
    s = _make_store([
        _btc_setup_row("1H", "completed", "long", 64000.0, 65500.0, knn="✓"),
    ])
    result = build_coin_detail(s, "BTC", tf="1H")
    s.close()
    setups = result["setups"]
    assert len(setups) >= 1
    assert "knn_note" in setups[0], "setup 缺少 knn_note"
    assert "找到历史相似态" in setups[0]["knn_note"]


def test_build_coin_detail_setups_have_honest_label():
    """setups 含 honest_label 字段，completed 标注为回顾型。"""
    s = _make_store([
        _btc_setup_row("1H", "completed", "long", 64000.0, 65500.0),
    ])
    result = build_coin_detail(s, "BTC", tf="1H")
    s.close()
    setups = result["setups"]
    assert "honest_label" in setups[0]
    assert "回顾型" in setups[0]["honest_label"]


def test_build_coin_detail_setups_have_prz_proximity():
    """setups 含 prz_proximity 字段（字符串，非空）。"""
    s = _make_store([
        _btc_setup_row("1H", "completed", "long", 64000.0, 66000.0),
    ])
    result = build_coin_detail(s, "BTC", tf="1H")
    s.close()
    setups = result["setups"]
    assert "prz_proximity" in setups[0]
    assert isinstance(setups[0]["prz_proximity"], str)
    assert len(setups[0]["prz_proximity"]) > 0


def test_build_coin_detail_confluence_detected():
    """两个 TF 同方向 PRZ 重叠 → confluence 非空。"""
    s = _make_store([
        _btc_setup_row("1H", "forming",   "long", 64000.0, 65500.0),
        _btc_setup_row("4H", "completed", "long", 64500.0, 66000.0),
    ])
    result = build_coin_detail(s, "BTC", tf="1H")
    s.close()
    confluence = result["confluence"]
    assert len(confluence) >= 1, f"应检测到共振，实得 {confluence}"
    assert confluence[0]["direction"] == "long"


def test_build_coin_detail_confluence_empty_no_overlap():
    """PRZ 不重叠 → confluence 为空。"""
    s = _make_store([
        _btc_setup_row("1H", "forming",   "long", 60000.0, 61000.0),
        _btc_setup_row("4H", "completed", "long", 65000.0, 66000.0),
    ])
    result = build_coin_detail(s, "BTC", tf="1H")
    s.close()
    assert result["confluence"] == []


def test_build_coin_detail_empty_store_no_crash():
    """空库时所有字段为空，不抛异常。"""
    s = _make_store([])
    result = build_coin_detail(s, "NONEXISTENT")
    s.close()
    assert result["setups"] == []
    assert result["confluence"] == []
    assert isinstance(result, dict)


def test_build_coin_detail_knn_question_mark():
    """knn='?' 时 knn_note 包含诚实降级说明。"""
    s = _make_store([
        _btc_setup_row("1H", "forming", "long", 64000.0, 65500.0, knn="?"),
    ])
    result = build_coin_detail(s, "BTC", tf="1H")
    s.close()
    setups = result["setups"]
    assert "knn_note" in setups[0]
    note = setups[0]["knn_note"]
    assert "不足" in note or "未计算" in note or "随机基线" in note


def test_build_coin_detail_forming_honest_label():
    """forming setup honest_label 含'前瞻'字样。"""
    s = _make_store([
        _btc_setup_row("1H", "forming", "short", 65000.0, 66000.0),
    ])
    result = build_coin_detail(s, "BTC", tf="1H")
    s.close()
    setups = result["setups"]
    assert "前瞻" in setups[0]["honest_label"]
