"""tests/test_dashboard_signals_module.py — dashboard_signals 扁平模块单测（T3 TDD）。

TDD 流程：先写测试 → RED → 实现 → GREEN。

验证：
  build_all_signals_state(store, now_ms) 返回 dict 含 signals_list / meta
  render_all_signals_html(state) 返回含 doctype 的非空 str 且无 CDN
均从 smc_tracker.dashboard_signals 导入（非 dashboard.py）。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store

# ── 直接从新扁平模块导入（T3 目标）──────────────────────────────────────────
from smc_tracker.dashboard_signals import (
    build_all_signals_state,
    render_all_signals_html,
)


# ---------------------------------------------------------------------------
# 辅助：空 Store
# ---------------------------------------------------------------------------

def _make_empty_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


def _make_store_with_signals() -> tuple[Store, int]:
    """建含多类型信号行的临时 Store（与 test_dashboard_signals.py 一致）。"""
    d = tempfile.mkdtemp()
    s = Store(Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    # signals 表（SMC 共振）
    s.insert_signal((
        now_ms - 60_000, "kPEPE", "long", 3.5,
        0.0, 0.8, 500_000.0, 0.05, 0.0,
        0.00280, 0.00260, 0.00320, 2.0, "test signal",
    ))

    # divergence 表（背离）
    s.insert_divergence((
        now_ms - 120_000, "kWIF", "bullish", 2.1,
        -0.0005, 0.03, 300_000.0, "divergence test",
    ))

    s.conn.commit()
    return s, now_ms


# ---------------------------------------------------------------------------
# build_all_signals_state 测试
# ---------------------------------------------------------------------------

def test_build_returns_dict_with_signals_list_and_meta():
    """build_all_signals_state 返回 dict，含 signals_list 与 meta 键。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    assert isinstance(state, dict), "返回值应为 dict"
    assert "signals_list" in state, "state 应含 signals_list"
    assert "meta" in state, "state 应含 meta"


def test_build_signals_list_is_list():
    """signals_list 是 list 类型。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    assert isinstance(state["signals_list"], list)


def test_build_meta_contains_generated_and_window_hours():
    """meta 含 generated / window_hours 字段。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000, hours=3.0)
    s.close()

    meta = state["meta"]
    assert "generated" in meta, "meta 应含 generated"
    assert "window_hours" in meta, "meta 应含 window_hours"
    assert meta["window_hours"] == 3.0


def test_build_empty_store_no_raise():
    """空库时不抛异常，signals_list=[]。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    assert state["signals_list"] == []


def test_build_contains_by_type():
    """state 含 by_type 字段（分组计数 dict）。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    assert "by_type" in state
    assert isinstance(state["by_type"], dict)


def test_build_with_signals_returns_nonempty_list():
    """有信号数据时 signals_list 非空。"""
    s, now_ms = _make_store_with_signals()
    state = build_all_signals_state(s, now_ms)
    s.close()

    assert len(state["signals_list"]) > 0, "有信号时 signals_list 应非空"


def test_build_signal_rows_are_dicts_with_unified_fields():
    """每条信号行是 dict，含统一字段 type / coin / ts / evidence_text。"""
    s, now_ms = _make_store_with_signals()
    state = build_all_signals_state(s, now_ms)
    s.close()

    for row in state["signals_list"]:
        assert isinstance(row, dict)
        for field in ("type", "coin", "ts", "evidence_text"):
            assert field in row, f"信号行缺少字段: {field}"


# ---------------------------------------------------------------------------
# render_all_signals_html 测试
# ---------------------------------------------------------------------------

def test_render_returns_nonempty_str():
    """render_all_signals_html 返回非空字符串。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    html = render_all_signals_html(state)
    assert isinstance(html, str) and len(html) > 0


def test_render_contains_doctype():
    """HTML 含 <!DOCTYPE html>（完整独立页面）。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    html = render_all_signals_html(state)
    assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()


def test_render_no_cdn():
    """render_all_signals_html 无外部 CDN 链接（自包含）。"""
    import re

    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    html = render_all_signals_html(state)
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"不应含外部资源: {kw}"
    bad = [m for m in re.findall(r'https?://[^\s"\'<>]+', html)
           if "w3.org/2000/svg" not in m]
    assert not bad, f"不应含外部链接: {bad[:3]}"


def test_render_contains_signals_title():
    """HTML 含「信号总览」字样。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    html = render_all_signals_html(state)
    assert "信号总览" in html


def test_render_initial_state_injected():
    """__INITIAL_STATE__ 占位符已被替换为实际 JSON。"""
    s, now_ms = _make_store_with_signals()
    state = build_all_signals_state(s, now_ms)
    s.close()

    html = render_all_signals_html(state)
    assert "__INITIAL_STATE__" not in html, "占位符应已替换"


def test_render_state_json_parseable():
    """注入的 const S JSON 可解析，含 signals_list 与 meta。"""
    import re

    s, now_ms = _make_store_with_signals()
    state = build_all_signals_state(s, now_ms)
    s.close()

    html = render_all_signals_html(state)
    m = re.search(r"const S\s*=\s*(\{.*?\});", html, re.S)
    assert m, "未找到注入的 const S"
    parsed = json.loads(m.group(1))
    assert "signals_list" in parsed and "meta" in parsed


if __name__ == "__main__":
    import traceback
    _pass = _fail = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  OK {name}")
                _pass += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL {name}: {exc}")
                traceback.print_exc()
                _fail += 1
    print(f"\n{'PASS' if not _fail else 'FAIL'} {_pass} passed, {_fail} failed")
