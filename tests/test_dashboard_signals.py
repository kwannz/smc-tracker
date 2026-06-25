"""tests/test_dashboard_signals.py — /signals 页 + /api/signals JSON API 单测。

TDD:
  build_all_signals_state(store, now_ms, hours) → dict
  render_all_signals_html(state)              → str
  GET /api/signals                            → JSON（含 signals_list, meta）
  GET /signals                                → HTML（含关键元素，无 CDN）

均不依赖网络；合成 Store + 若干信号表行。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store
from smc_tracker.dashboard import (
    build_all_signals_state,
    render_all_signals_html,
)


# ---------------------------------------------------------------------------
# 辅助：建合成 Store（含多类型信号行）
# ---------------------------------------------------------------------------

def _make_store() -> tuple[Store, int]:
    """建含多类型信号行的临时 Store。"""
    d = tempfile.mkdtemp()
    s = Store(Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    # 1. signals 表（SMC 共振）
    s.insert_signal((
        now_ms - 60_000, "kPEPE", "long", 3.5,
        0.0, 0.8, 500_000.0, 0.05, 0.0,
        0.00280, 0.00260, 0.00320, 2.0, "test signal",
    ))

    # 2. divergence 表（背离）
    s.insert_divergence((
        now_ms - 120_000, "kWIF", "bullish", 2.1,
        -0.0005, 0.03, 300_000.0, "divergence test",
    ))

    # 3. whale_signals 表（跟庄）
    s.insert_whale_signal((
        now_ms - 180_000, "0xA", "whale_A",
        "kPEPE", "OPEN", "long",
        200_000.0, 0.00275, 200_000.0, 1,
    ))

    # 4. consensus 表（多庄共识）
    try:
        s.conn.execute(
            "CREATE TABLE IF NOT EXISTS consensus "
            "(ts INTEGER, coin TEXT, direction TEXT, n_agree INTEGER, "
            "n_oppose INTEGER, net_notional REAL, score REAL, labels TEXT)"
        )
        s.conn.execute(
            "INSERT INTO consensus VALUES (?,?,?,?,?,?,?,?)",
            (now_ms - 240_000, "SOL", "long", 3, 1, 500_000.0, 0.8, "A,B,C"),
        )
        s.conn.commit()
    except Exception:
        pass

    # 5. harmonic_setups 表（谐波形态）
    s.insert_harmonic_setups([
        (
            now_ms - 300_000, "BTC", "1h", "completed", "Gartley", "long",
            65000.0, 64500.0, 64800.0, 63000.0, 67000.0, 69000.0,
            2.5, 0.82, "✓", "✓ 买压确认", "XA=0.618", 64000.0, 65200.0,
            1, 60000.0, 10, 70000.0, 15, 55000.0, 20, 65000.0, 25, 64500.0,
        ),
    ])

    s.conn.commit()
    return s, now_ms


def _make_empty_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


# ---------------------------------------------------------------------------
# build_all_signals_state 测试
# ---------------------------------------------------------------------------

def test_build_all_signals_state_returns_dict():
    """build_all_signals_state 返回 dict，含 signals_list / meta 键。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    assert isinstance(state, dict)
    assert "signals_list" in state, "state 应含 signals_list"
    assert "meta" in state, "state 应含 meta"


def test_build_all_signals_state_meta_fields():
    """meta 含 generated / window_hours 字段。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms, hours=2)
    s.close()

    meta = state["meta"]
    assert "generated" in meta, "meta 应含 generated"
    assert "window_hours" in meta, "meta 应含 window_hours"
    assert meta["window_hours"] == 2


def test_build_all_signals_state_signals_list_is_list():
    """signals_list 是 list，每项是 dict。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    lst = state["signals_list"]
    assert isinstance(lst, list)
    for item in lst:
        assert isinstance(item, dict)


def test_build_all_signals_state_unified_fields():
    """每条信号含统一字段：type / type_label / coin / direction / ts / evidence_text。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    for row in state["signals_list"]:
        for field in ("type", "type_label", "coin", "ts", "evidence_text"):
            assert field in row, f"信号行缺少字段: {field}"


def test_build_all_signals_state_contains_multiple_types():
    """signals_list 含多种 type（至少 signal + divergence + whale_signal）。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    types = {r["type"] for r in state["signals_list"]}
    assert "signal" in types, "应含 signal 类型"
    assert "divergence" in types, "应含 divergence 类型"
    assert "whale_signal" in types, "应含 whale_signal 类型"


def test_build_all_signals_state_sorted_by_ts_desc():
    """signals_list 按 ts 降序排列（最新在前）。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    lst = state["signals_list"]
    if len(lst) >= 2:
        tss = [r["ts"] for r in lst]
        assert tss == sorted(tss, reverse=True), "signals_list 应按 ts 降序"


def test_build_all_signals_state_window_filter():
    """窗口过滤：hours=0.0001（极小窗口）时，近期数据全过滤，返回空列表。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms, hours=0.0001)
    s.close()

    assert state["signals_list"] == [], "极小窗口时 signals_list 应为 []"


def test_build_all_signals_state_empty_store_no_raise():
    """空库时 build_all_signals_state 不抛异常，signals_list=[]。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    assert isinstance(state, dict)
    assert state["signals_list"] == []


def test_build_all_signals_state_groups_by_type():
    """state 含 by_type 字段，按 type 分组计数。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    assert "by_type" in state, "state 应含 by_type 分组"
    by_type = state["by_type"]
    assert isinstance(by_type, dict)
    # 有信号行的类型在分组中出现
    types = {r["type"] for r in state["signals_list"]}
    for t in types:
        assert t in by_type, f"by_type 应含 {t}"


def test_build_all_signals_state_harmonic_type():
    """harmonic_setups 信号归入 harmonic_setup 类型。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    types = {r["type"] for r in state["signals_list"]}
    assert "harmonic_setup" in types, "应含 harmonic_setup 类型"


# ---------------------------------------------------------------------------
# render_all_signals_html 测试
# ---------------------------------------------------------------------------

def test_render_all_signals_html_returns_str():
    """render_all_signals_html 返回非空字符串。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    html = render_all_signals_html(state)
    assert isinstance(html, str) and len(html) > 0


def test_render_all_signals_html_doctype():
    """HTML 是完整独立页面（含 <!DOCTYPE html>）。"""
    state = build_all_signals_state(_make_empty_store(), 1_700_000_000_000)
    html = render_all_signals_html(state)
    assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()


def test_render_all_signals_html_title():
    """HTML 含「信号总览」字样（页面标题）。"""
    state = build_all_signals_state(_make_empty_store(), 1_700_000_000_000)
    html = render_all_signals_html(state)
    assert "信号总览" in html, "HTML 应含「信号总览」字样"


def test_render_all_signals_html_disclaimer():
    """HTML 含诚实免责声明字样（1h≈随机 或 非投资建议）。"""
    state = build_all_signals_state(_make_empty_store(), 1_700_000_000_000)
    html = render_all_signals_html(state)
    # 至少含其中之一
    has_disc = "随机" in html or "非投资建议" in html or "1h≈随机" in html
    assert has_disc, "HTML 应含诚实免责声明"


def test_render_all_signals_html_type_labels():
    """HTML 含各信号类型的中文标签（SMC共振/背离/跟庄等）。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    html = render_all_signals_html(state)
    # 中文 type_label 应出现在 HTML 中
    assert "SMC共振" in html or "背离" in html or "跟庄" in html


def test_render_all_signals_html_coin_names():
    """HTML 含注入的币名（kPEPE / kWIF / BTC）。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    html = render_all_signals_html(state)
    assert "kPEPE" in html or "kWIF" in html or "BTC" in html


def test_render_all_signals_html_no_cdn():
    """render_all_signals_html 无外部 CDN/资源链接（自包含）。"""
    import re
    state = build_all_signals_state(_make_empty_store(), 1_700_000_000_000)
    html = render_all_signals_html(state)

    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"不应含外部资源: {kw}"
    bad = [m for m in re.findall(r'https?://[^\s"\'<>]+', html)
           if "w3.org/2000/svg" not in m]
    assert not bad, f"不应含外部链接: {bad[:3]}"


def test_render_all_signals_html_no_residual_braces():
    """转义正确性：输出不含残留 {{ (模板解转义完整)。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    html = render_all_signals_html(state)
    assert "{{" not in html, "残留 {{ → 模板转义不完整"


def test_render_all_signals_html_api_signals_fetch():
    """HTML 含 /api/signals fetch 逻辑（5s 自刷新）。"""
    state = build_all_signals_state(_make_empty_store(), 1_700_000_000_000)
    html = render_all_signals_html(state)
    assert "/api/signals" in html, "HTML 应含 /api/signals"
    assert "setInterval" in html, "HTML 应含 setInterval（5s 自刷新）"


def test_render_all_signals_html_initial_state_injected():
    """__INITIAL_STATE__ 被替换为实际可解析 JSON。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    html = render_all_signals_html(state)
    assert "__INITIAL_STATE__" not in html, "占位符应已替换"

    import re
    m = re.search(r"const S\s*=\s*(\{.*?\});", html, re.S)
    assert m, "未找到注入的 const S"
    parsed = json.loads(m.group(1))
    assert "signals_list" in parsed and "meta" in parsed


def test_render_all_signals_html_nav_links():
    """HTML 含导航链接（/ 或 /hl2 或 /harmonic2），与现有 dashboard 导航一致。"""
    state = build_all_signals_state(_make_empty_store(), 1_700_000_000_000)
    html = render_all_signals_html(state)
    # 至少含一个站内导航
    has_nav = 'href="/"' in html or 'href="/hl2"' in html or 'href="/harmonic2"' in html
    assert has_nav, "HTML 应含站内导航链接"


def test_render_all_signals_html_empty_state():
    """空信号时 HTML 仍正常渲染，不抛异常。"""
    s = _make_empty_store()
    state = build_all_signals_state(s, 1_700_000_000_000)
    s.close()

    html = render_all_signals_html(state)
    assert isinstance(html, str) and "信号总览" in html


def test_render_all_signals_html_direction_classes():
    """HTML 含方向色彩 CSS 类或样式（long=绿/short=红），用于信号行显示。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    html = render_all_signals_html(state)
    # 含方向相关 CSS class 或 颜色变量
    has_dir = "long" in html and ("short" in html or "green" in html)
    assert has_dir, "HTML 应含方向色彩信息"


def test_render_all_signals_html_evidence_text():
    """HTML 含 evidence_text（信号证据摘要）字样（通过初始 state JSON 注入）。"""
    s, now_ms = _make_store()
    state = build_all_signals_state(s, now_ms)
    s.close()

    html = render_all_signals_html(state)
    # evidence_text 通过 const S 注入，JS 渲染时展示
    assert "evidence_text" in html, "HTML 应含 evidence_text 键（JSON 注入后可见）"


# ---------------------------------------------------------------------------
# /api/signals 路由集成测试（模拟 aiohttp handler）
# ---------------------------------------------------------------------------

def _simulate_api_signals(store: Store, now_ms: int, hours: float = 1.0) -> dict:
    """复现 handle_api_signals 业务逻辑（纯函数，不起真实服务）。"""
    state = build_all_signals_state(store, now_ms, hours=hours)
    return state


def test_api_signals_envelope_structure():
    """/api/signals 响应含 signals_list / by_type / meta。"""
    s, now_ms = _make_store()
    result = _simulate_api_signals(s, now_ms)
    s.close()

    assert "signals_list" in result
    assert "by_type" in result
    assert "meta" in result


def test_api_signals_json_serializable():
    """/api/signals 响应可序列化为 JSON（不含不可序列化对象）。"""
    s, now_ms = _make_store()
    result = _simulate_api_signals(s, now_ms)
    s.close()

    try:
        json.dumps(result, default=str)
    except (TypeError, ValueError) as exc:
        pytest.fail(f"JSON 序列化失败: {exc}")


def test_api_signals_contains_kpepe():
    """kPEPE 信号应出现在 signals_list 中。"""
    s, now_ms = _make_store()
    result = _simulate_api_signals(s, now_ms)
    s.close()

    coins = {r["coin"] for r in result["signals_list"]}
    assert "kPEPE" in coins, "signals_list 应含 kPEPE"


def test_api_signals_empty_store():
    """空库时 signals_list=[]，不抛异常。"""
    s = _make_empty_store()
    result = _simulate_api_signals(s, 1_700_000_000_000)
    s.close()

    assert result["signals_list"] == []
    assert isinstance(result["by_type"], dict)


def test_api_signals_hours_parameter():
    """hours 参数控制时间窗口大小。"""
    s, now_ms = _make_store()
    # 宽窗口有数据
    wide = _simulate_api_signals(s, now_ms, hours=24)
    # 极窄窗口无数据
    narrow = _simulate_api_signals(s, now_ms, hours=0.0001)
    s.close()

    assert len(wide["signals_list"]) > 0, "24h 宽窗口应有数据"
    assert len(narrow["signals_list"]) == 0, "极窄窗口应无数据"


# ---------------------------------------------------------------------------
# 导航：/signals 页应在主 dashboard nav 中有链接（回归测试）
# ---------------------------------------------------------------------------

def test_main_html_has_signals_nav_link():
    """主 dashboard render_html 应含 /signals 导航链接。"""
    from smc_tracker.dashboard import build_dashboard_state, render_html

    s = _make_empty_store()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    html = render_html(state)
    assert "/signals" in html, "主 dashboard 导航应含 /signals 链接"


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
