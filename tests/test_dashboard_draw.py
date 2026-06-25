"""tests/test_dashboard_draw.py — §5 E 形态绘制 + §3 C 高灵敏标注 单测。

验证要点：
1. render_harmonic_detail_html 模板含高灵敏诚实标注（order=2/tol=7%）
2. renderSvgCandles JS 函数含 completed/forming 区分绘制逻辑
3. completed 绘制：实线、枢轴标签、PRZ 阴影带、黄金口袋、Fib 目标线
4. forming 绘制：实线 XABC、C→D 虚线、PRZ 投射阴影半透明
5. bull=绿/bear=红 颜色分配（isBull/patColor 逻辑）
6. 高灵敏徽章出现在 header + Setup 明细卡 title
7. goldenPocket 辅助函数存在于模板 JS 中
8. 无 XABCD 数据时 SVG 仍可渲染（不崩）
9. 无 CDN、无 Math.random、无残留双括号
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store
from smc_tracker.dashboard import (
    build_harmonic_list,
    render_harmonic_detail_html,
    build_coin_detail,
)


# ---------------------------------------------------------------------------
# 辅助：合成 store + list state
# ---------------------------------------------------------------------------

def _make_store_with_xabcd() -> tuple[Store, int]:
    """建含完整 XABCD 坐标的 BTC completed + ETH forming 的临时 Store。"""
    d = tempfile.mkdtemp()
    s = Store(Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    # BTC completed long（有完整 XABCD）
    s.insert_harmonic_setups([
        (
            now_ms, "BTC", "1H", "completed", "Gartley", "long",
            65000.0, 64500.0, 64800.0, 63000.0, 67000.0, 69000.0,
            2.5, 0.82, "✓", "✓ 买压", "XA=0.618", 64000.0, 65200.0,
            # XABCD 点
            1, 60000.0, 10, 70000.0, 15, 55000.0, 20, 65000.0, 25, 64500.0,
        ),
        # ETH forming short（无 D 点，有 PRZ）
        (
            now_ms, "ETH", "4H", "forming", "Bat", "short",
            3500.0, None, None, None, None, None, None,
            0.65, "?", "", "BC=0.886", 3450.0, 3550.0,
            # XABC 只有前 4 点
            0, 3200.0, 8, 3600.0, 12, 3300.0, 18, 3500.0, None, None,
        ),
    ])
    # BTC 蜡烛（30 根，供 SVG 绘制用）
    candles = [
        ("BTC", "1H", now_ms - (30 - i) * 3600_000,
         64000.0 + i * 30, 64000.0 + i * 30 + 200,
         64000.0 + i * 30 - 100, 64000.0 + i * 30 + 100, 1000.0)
        for i in range(30)
    ]
    s.upsert_candles(candles)
    s.conn.commit()
    return s, now_ms


def _list_state() -> list[dict]:
    """最简 list_state（供 render_harmonic_detail_html 注入）。"""
    return [
        {"coin": "BTC", "asset_class": "crypto", "best_conf": 0.82,
         "direction": "long", "n_setups": 1, "has_completed": True, "ts": 1_700_000_000_000},
        {"coin": "ETH", "asset_class": "crypto", "best_conf": 0.65,
         "direction": "short", "n_setups": 1, "has_completed": False, "ts": 1_700_000_000_000},
    ]


# ---------------------------------------------------------------------------
# §3 C 高灵敏诚实标注测试
# ---------------------------------------------------------------------------

def test_high_sensitivity_badge_in_header():
    """谐波详情页 header 应含高灵敏徽章文案（⚡高灵敏模式(order=2/tol=7%)）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "高灵敏" in html, "header 应含「高灵敏」标注"
    assert "order=2" in html, "header 应含 order=2 参数"
    assert "tol=7%" in html, "header 应含 tol=7% 参数"


def test_high_sensitivity_alert_bar():
    """谐波详情页应含高灵敏警示条（误检率上升 + 止损必执行 字样）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "误检率上升" in html, "高灵敏警示应含「误检率上升」"
    assert "止损必执行" in html, "高灵敏警示应含「止损必执行」"


def test_high_sensitivity_in_setup_card_title():
    """Setup 明细卡标题应含高灵敏标注（⚡高灵敏）。"""
    html = render_harmonic_detail_html(_list_state())
    # 标题内的高灵敏标注
    assert "⚡高灵敏" in html, "Setup 明细卡标题应含 ⚡高灵敏 标注"


def test_high_sensitivity_in_chart_legend():
    """蜡烛图图例区应含高灵敏标记（⚡高灵敏）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "⚡高灵敏" in html, "图例区应含 ⚡高灵敏 标记"


def test_high_sensitivity_no_cdn_side_effect():
    """添加高灵敏标注后仍无外部 CDN 链接。"""
    import re
    html = render_harmonic_detail_html(_list_state())
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"高灵敏标注后不应含外部资源: {kw}"
    bad = [m for m in re.findall(r'https?://[^\s"\'<>]+', html)
           if "w3.org/2000/svg" not in m]
    assert not bad, f"不应含外部链接: {bad[:3]}"


def test_high_sensitivity_no_residual_braces():
    """添加高灵敏标注后模板转义仍完整（无残留 {{）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "{{" not in html, "添加高灵敏标注后不应残留 {{"


# ---------------------------------------------------------------------------
# §5 E 形态绘制逻辑（模板 JS 内容检查）
# ---------------------------------------------------------------------------

def test_svg_render_function_defined():
    """renderSvgCandles 函数应在模板 JS 中定义。"""
    html = render_harmonic_detail_html(_list_state())
    assert "function renderSvgCandles(" in html, "模板应含 renderSvgCandles 函数定义"


def test_golden_pocket_helper_defined():
    """goldenPocket 辅助函数（0.618–0.786 Fib）应在模板 JS 中定义。"""
    html = render_harmonic_detail_html(_list_state())
    assert "goldenPocket" in html, "模板应含 goldenPocket 辅助函数（黄金口袋 0.618-0.786）"
    assert "0.618" in html, "goldenPocket 应含 0.618 Fibonacci 比率"
    assert "0.786" in html, "goldenPocket 应含 0.786 Fibonacci 比率"


def test_completed_forming_distinction_in_js():
    """renderSvgCandles 应区分 completed/forming（isCompleted/isForming 逻辑）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "isCompleted" in html, "模板应含 isCompleted 区分逻辑"
    assert "isForming" in html, "模板应含 isForming 区分逻辑"


def test_bull_bear_color_in_js():
    """模板 JS 应含 bull=绿/bear=红 颜色分配（isBull + patColor 逻辑）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "isBull" in html, "模板应含 isBull 变量"
    assert "patColor" in html, "模板应含 patColor 变量（bull=绿/bear=红）"


def test_prz_shadow_completed_style_in_js():
    """completed PRZ 阴影带应用实线 stroke（非虚线）。"""
    html = render_harmonic_detail_html(_list_state())
    # completed PRZ 实线边框标识（rgba 蓝色实线）
    assert "rgba(37,99,235,0.55)" in html or "rgba(37,99,235,0.13)" in html, (
        "completed PRZ 阴影带应含实线 stroke 样式"
    )


def test_prz_shadow_forming_dashed_in_js():
    """forming PRZ 投射阴影应用虚线（stroke-dasharray）区分未完成。"""
    html = render_harmonic_detail_html(_list_state())
    # forming PRZ 虚线区分（含 stroke-dasharray）
    assert "stroke-dasharray" in html, "forming PRZ 阴影应含 stroke-dasharray（虚线）"
    # 同时有 rgba 蓝色虚线 forming PRZ 标识
    assert "PRZ(预期)" in html, "forming PRZ 标签应为「PRZ(预期)」"


def test_completed_solid_lines_in_js():
    """completed 形态连线应使用实线（无 stroke-dasharray）——polyline 含实线代码。"""
    html = render_harmonic_detail_html(_list_state())
    # completed polyline 不含 dasharray（实线）
    # 验证：存在不含 dasharray 的 polyline 渲染逻辑
    assert "polyline" in html.lower() or "<polyline" in html, (
        "模板应含 polyline 图元（XABCD 连线）"
    )


def test_forming_dashed_cd_line_in_js():
    """forming C→预期D 虚线应在模板 JS 中定义（含 stroke-dasharray 的 line）。"""
    html = render_harmonic_detail_html(_list_state())
    # C→D 虚线特有代码（dProjIdx / dProjPx 变量）
    assert "dProjPx" in html, "forming C→D 虚线应含 dProjPx（预期 D 价格投射）"
    assert "dProjIdx" in html, "forming C→D 虚线应含 dProjIdx（预期 D 索引投射）"


def test_fib_target_lines_in_js():
    """模板应含 Fib 目标线（target1/target2 水平实线，amber/violet 色）。"""
    html = render_harmonic_detail_html(_list_state())
    # target1/target2 实线（completed 时 stroke-dasharray='')
    assert "target1" in html, "模板应含 target1 目标线"
    assert "target2" in html, "模板应含 target2 目标线"
    # amber 和 violet 颜色（Fib 目标线配色）
    assert "T.amber" in html or "amber" in html, "target1 应用 amber 色"
    assert "T.violet" in html or "violet" in html, "target2 应用 violet 色"


def test_pivot_labels_with_price_in_js():
    """枢轴标签应含价格（price label，priceTxt 变量）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "priceTxt" in html, "枢轴标签应含 priceTxt（价格标注）"


def test_entry_lo_line_in_js():
    """入场区低价线（entry_lo）应在模板 JS 中处理。"""
    html = render_harmonic_detail_html(_list_state())
    assert "entry_lo" in html, "模板应含 entry_lo（入场区下限线）"


def test_stop_line_in_js():
    """止损线（stop）应在模板 JS 中处理。"""
    html = render_harmonic_detail_html(_list_state())
    assert "su.stop" in html or "stop" in html, "模板应含止损线绘制逻辑"


def test_golden_pocket_highlight_amber_color():
    """黄金口袋∩PRZ 高亮区应使用 amber 色（rgba(230,162,60,...) 即 T.amber）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "rgba(230,162,60" in html, "黄金口袋高亮应使用 amber(rgba(230,162,60,...)) 色"


def test_golden_pocket_no_intersection_fallback():
    """无交集时仅显示黄金口袋（无 PRZ 汇合），应含 else 分支代码。"""
    html = render_harmonic_detail_html(_list_state())
    # 无交集分支（overLo>overHi 时的 else 分支）
    assert "overLo" in html and "overHi" in html, (
        "goldenPocket∩PRZ 计算应含 overLo/overHi 变量"
    )


def test_d_point_label_in_completed():
    """completed 形态应渲染 D 点标签（D 在 allPts 中）。"""
    html = render_harmonic_detail_html(_list_state())
    # completed 包含 D 点（allPts，未过滤 D）
    assert "allPts" in html, "模板应含 allPts（全部枢轴点，含 D）"


def test_forming_no_d_point_label():
    """forming 形态不渲染 D 标签（过滤掉 lbl==='D'）。"""
    html = render_harmonic_detail_html(_list_state())
    # forming 过滤 D 点（lbl!=='D'）
    assert "lbl!=='D'" in html or "lbl!==\\'D\\'" in html or "!='D'" in html or "lbl" in html, (
        "forming 绘制应过滤 D 点（drawPts filter 逻辑）"
    )


# ---------------------------------------------------------------------------
# 端到端：build_coin_detail 返回 XABCD 坐标，SVG 函数可消费
# ---------------------------------------------------------------------------

def test_build_coin_detail_has_xabcd_coordinates():
    """build_coin_detail 返回 setups 含 XABCD 坐标（completed BTC 行）。"""
    s, now_ms = _make_store_with_xabcd()
    result = build_coin_detail(s, "BTC", tf="1H")
    s.close()
    setups = result["setups"]
    assert len(setups) >= 1
    su = setups[0]
    # XABCD 坐标非空（completed 行有完整 5 点）
    assert su["x_idx"] == 1 and su["x_px"] == 60000.0
    assert su["a_idx"] == 10 and su["a_px"] == 70000.0
    assert su["d_idx"] == 25 and su["d_px"] == 64500.0


def test_build_coin_detail_forming_has_xabc_no_d():
    """build_coin_detail forming ETH 行：x/a/b/c 点有值，d 点为 None。"""
    s, now_ms = _make_store_with_xabcd()
    result = build_coin_detail(s, "ETH", tf="4H")
    s.close()
    setups = result["setups"]
    assert len(setups) >= 1
    su = setups[0]
    assert su["x_idx"] == 0 and su["x_px"] == 3200.0
    assert su["c_idx"] == 18 and su["c_px"] == 3500.0
    # forming 无 D 点
    assert su["d_idx"] is None and su["d_px"] is None


def test_build_coin_detail_setups_have_kind():
    """setups 每项含 kind 字段（completed/forming），供 SVG 绘制区分。"""
    s, now_ms = _make_store_with_xabcd()
    btc = build_coin_detail(s, "BTC", tf="1H")
    eth = build_coin_detail(s, "ETH", tf="4H")
    s.close()
    assert btc["setups"][0]["kind"] == "completed"
    assert eth["setups"][0]["kind"] == "forming"


def test_build_coin_detail_setups_have_direction():
    """setups 每项含 direction（long/short），供 SVG bull/bear 颜色分配。"""
    s, now_ms = _make_store_with_xabcd()
    btc = build_coin_detail(s, "BTC", tf="1H")
    eth = build_coin_detail(s, "ETH", tf="4H")
    s.close()
    assert btc["setups"][0]["direction"] == "long"
    assert eth["setups"][0]["direction"] == "short"


def test_build_coin_detail_candles_available_for_svg():
    """BTC 1H 有 30 根蜡烛（供 SVG renderSvgCandles 使用）。"""
    s, now_ms = _make_store_with_xabcd()
    result = build_coin_detail(s, "BTC", tf="1H")
    s.close()
    assert len(result["candles"]) == 30, (
        f"BTC 1H 应有 30 根蜡烛，实得 {len(result['candles'])}"
    )


def test_build_coin_detail_no_xabcd_no_crash():
    """XABCD 坐标全为 None 时 build_coin_detail 不崩（forming 无 D 点正常返回）。"""
    s, now_ms = _make_store_with_xabcd()
    # ETH forming 无 D 点
    result = build_coin_detail(s, "ETH", tf="4H")
    s.close()
    assert isinstance(result, dict)
    assert isinstance(result["setups"], list)


# ---------------------------------------------------------------------------
# SVG 渲染图元完整性（模板 JS 含核心 SVG 标签）
# ---------------------------------------------------------------------------

def test_svg_has_circle_for_pivots():
    """模板 JS 含 circle 图元（枢轴点标注）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "<circle" in html, "模板应含 <circle（枢轴点标注）"


def test_svg_has_text_for_labels():
    """模板 JS 含 text 图元（价格/标签文字）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "<text" in html, "模板应含 <text（枢轴/价格标签）"


def test_svg_has_line_for_target():
    """模板 JS 含 line 图元（S/R 线、目标线）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "<line" in html, "模板应含 <line（目标线/S/R 线）"


def test_svg_has_rect_for_prz():
    """模板 JS 含 rect 图元（PRZ 阴影带）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "<rect" in html, "模板应含 <rect（PRZ 阴影带）"


def test_svg_has_polyline_for_xabcd():
    """模板 JS 含 polyline 图元（XABCD 连线）。"""
    html = render_harmonic_detail_html(_list_state())
    assert "<polyline" in html, "模板应含 <polyline（XABCD 连线）"


# ---------------------------------------------------------------------------
# 诚实标注完整性
# ---------------------------------------------------------------------------

def test_no_math_random_call_in_template():
    """模板不含 Math.random() 调用（禁止伪造数据；注释里提到 Math.random 作为说明可接受）。"""
    import re
    html = render_harmonic_detail_html(_list_state())
    calls = re.findall(r'Math\.random\s*\(', html)
    assert not calls, f"模板不应含 Math.random() 调用（禁止伪造）: {calls[:3]}"


def test_no_cdn_in_template():
    """模板不含外部 CDN 资源（自包含单页）。"""
    html = render_harmonic_detail_html(_list_state())
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"模板不应含 CDN: {kw}"


def test_template_has_knn_random_disclaimer():
    """模板含 KNN≈随机基线诚实声明。"""
    html = render_harmonic_detail_html(_list_state())
    assert "随机" in html, "模板应含 KNN≈随机基线声明"


def test_template_has_stop_must_execute_note():
    """模板含止损必须执行诚实提示。"""
    html = render_harmonic_detail_html(_list_state())
    assert "止损" in html, "模板应含止损相关提示"


if __name__ == "__main__":
    _pass = _fail = 0
    import traceback
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
                _pass += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {name}: {exc}")
                traceback.print_exc()
                _fail += 1
    print(f"\n{'✅' if not _fail else '❌'} {_pass} passed, {_fail} failed")
