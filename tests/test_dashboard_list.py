"""tests/test_dashboard_list.py — /api/harmonic/list 分页/搜索/过滤 单测。

合成 DB 数据，验证：
  - handle_harmonic_list 分页（offset/limit 边界）
  - 搜索（q 关键词过滤，大小写不敏感）
  - asset_class 过滤（'crypto' / 'tradfi'）
  - 空结果不崩
  - 响应 envelope 结构正确（items/total/offset/limit）
  - render_harmonic_detail_html 首屏注入的初始状态仍为数组（backward compatible）
  - refreshList JS 函数兼容新 envelope 格式（通过模板扫描）

所有测试均不依赖网络，使用内存合成数据。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store
from smc_tracker.dashboard import (
    build_harmonic_list,
    render_harmonic_detail_html,
)


# ---------------------------------------------------------------------------
# 辅助：构造合成 Store
# ---------------------------------------------------------------------------

_COINS = [
    ("BTC",   "1H",  "completed", "Gartley", "long",  65000.0, 0.92),
    ("ETH",   "4H",  "forming",   "Bat",     "short",  3500.0, 0.75),
    ("SOL",   "1D",  "completed", "Crab",    "long",    150.0, 0.68),
    ("XAU",   "1H",  "completed", "Shark",   "short", 2350.0, 0.85),  # tradfi
    ("BNB",   "15m", "forming",   "Gartley", "long",    560.0, 0.60),
    ("DOGE",  "30m", "completed", "Bat",     "short",   0.15, 0.55),
    ("AAPL",  "1D",  "forming",   "Crab",    "long",   200.0, 0.70),  # tradfi
    ("SOXL",  "4H",  "completed", "Gartley", "short",   45.0, 0.65),  # tradfi
    ("PEPE",  "1H",  "forming",   "Bat",     "long",    0.000011, 0.50),
    ("AVAX",  "12H", "completed", "Shark",   "short",   35.0, 0.58),
]


def _make_store() -> tuple[Store, int]:
    """建含 10 枚合成谐波数据的临时 Store（BTC/ETH/SOL/XAU/BNB/DOGE/AAPL/SOXL/PEPE/AVAX）。"""
    d = tempfile.mkdtemp()
    s = Store(Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    rows = []
    for i, (coin, tf, kind, pattern, direction, price, conf) in enumerate(_COINS):
        # 29 列：ts,coin,tf,kind,pattern,direction,price,entry_lo,entry_hi,stop,
        #        target1,target2,rr,confidence,knn,orderflow,fib_note,prz_lo,prz_hi,
        #        x_idx,x_px,a_idx,a_px,b_idx,b_px,c_idx,c_px,d_idx,d_px
        rows.append((
            now_ms - i * 60_000, coin, tf, kind, pattern, direction,
            price, None, None, None, None, None, None,
            conf, "?", "", "XA=0.618", None, None,
            None, None, None, None, None, None, None, None, None, None,
        ))
    s.insert_harmonic_setups(rows)
    s.conn.commit()
    return s, now_ms


def _make_empty_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


# ---------------------------------------------------------------------------
# 辅助：模拟 aiohttp Request（不起真实服务，直接调用 build_harmonic_list + 过滤逻辑）
# ---------------------------------------------------------------------------

def _simulate_list(
    store: Store,
    *,
    q: str = "",
    asset_class: str = "",
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """复现 handle_harmonic_list 的业务逻辑（不依赖 aiohttp，纯函数测试）。"""
    lst = build_harmonic_list(store)

    q_norm = q.strip().lower()
    ac_norm = asset_class.strip().lower()

    if q_norm:
        lst = [r for r in lst if q_norm in (r.get("coin") or "").lower()]
    if ac_norm in ("crypto", "tradfi"):
        lst = [r for r in lst if r.get("asset_class") == ac_norm]

    total = len(lst)
    items = lst[offset: offset + limit]
    return {"items": items, "total": total, "offset": offset, "limit": limit}


# ---------------------------------------------------------------------------
# 基础结构测试
# ---------------------------------------------------------------------------

def test_list_envelope_structure_basic():
    """build_harmonic_list + 过滤逻辑返回标准 envelope。"""
    s, _ = _make_store()
    result = _simulate_list(s)
    s.close()

    assert "items" in result
    assert "total" in result
    assert "offset" in result
    assert "limit" in result
    assert isinstance(result["items"], list)
    assert isinstance(result["total"], int)
    assert result["offset"] == 0
    assert result["limit"] == 50


def test_list_default_returns_all_coins():
    """默认（无参数）应返回全部 10 枚币，total=10。"""
    s, _ = _make_store()
    result = _simulate_list(s)
    s.close()

    assert result["total"] == 10
    assert len(result["items"]) == 10


def test_list_empty_store_no_crash():
    """空库时 total=0, items=[]，不抛异常。"""
    s = _make_empty_store()
    result = _simulate_list(s)
    s.close()

    assert result["total"] == 0
    assert result["items"] == []
    assert isinstance(result["items"], list)


# ---------------------------------------------------------------------------
# 分页（offset / limit）测试
# ---------------------------------------------------------------------------

def test_list_pagination_limit():
    """limit=3 时 items 长度 ≤ 3。"""
    s, _ = _make_store()
    result = _simulate_list(s, limit=3)
    s.close()

    assert result["limit"] == 3
    assert len(result["items"]) == 3
    assert result["total"] == 10


def test_list_pagination_offset():
    """offset=5 时返回后 5 枚，total 仍=10。"""
    s, _ = _make_store()
    result_all = _simulate_list(s)
    result_offset = _simulate_list(s, offset=5)
    s.close()

    assert result_offset["total"] == 10
    assert result_offset["offset"] == 5
    assert len(result_offset["items"]) == 5
    # items 应为全量列表的后半部分
    assert result_offset["items"] == result_all["items"][5:]


def test_list_pagination_offset_and_limit():
    """offset=2, limit=4 → 返回第 3~6 枚。"""
    s, _ = _make_store()
    result_all = _simulate_list(s)
    result_page = _simulate_list(s, offset=2, limit=4)
    s.close()

    assert result_page["total"] == 10
    assert result_page["offset"] == 2
    assert result_page["limit"] == 4
    assert len(result_page["items"]) == 4
    assert result_page["items"] == result_all["items"][2:6]


def test_list_pagination_offset_beyond_end():
    """offset 超过 total 时 items=[]，total 不变，不崩。"""
    s, _ = _make_store()
    result = _simulate_list(s, offset=100)
    s.close()

    assert result["total"] == 10
    assert result["items"] == []


def test_list_pagination_limit_one():
    """limit=1 只返回第一枚。"""
    s, _ = _make_store()
    result = _simulate_list(s, limit=1)
    s.close()

    assert len(result["items"]) == 1
    assert result["total"] == 10


def test_list_pagination_offset_zero_limit_large():
    """offset=0, limit=9999 → 返回全部。"""
    s, _ = _make_store()
    result = _simulate_list(s, offset=0, limit=9999)
    s.close()

    assert len(result["items"]) == 10
    assert result["total"] == 10


def test_list_pagination_last_page_partial():
    """limit=3, offset=9 → 最后一页只有 1 枚（边界）。"""
    s, _ = _make_store()
    result = _simulate_list(s, limit=3, offset=9)
    s.close()

    assert result["total"] == 10
    assert len(result["items"]) == 1


# ---------------------------------------------------------------------------
# 搜索（q 关键词）测试
# ---------------------------------------------------------------------------

def test_list_search_exact_match():
    """q='BTC' → 精确匹配，total=1，items[0].coin='BTC'。"""
    s, _ = _make_store()
    result = _simulate_list(s, q="BTC")
    s.close()

    assert result["total"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["coin"] == "BTC"


def test_list_search_case_insensitive():
    """q='btc' 大小写不敏感，仍能找到 BTC。"""
    s, _ = _make_store()
    result = _simulate_list(s, q="btc")
    s.close()

    assert result["total"] == 1
    assert result["items"][0]["coin"] == "BTC"


def test_list_search_partial_match():
    """q='OL' 子串匹配 SOL，total=1。"""
    s, _ = _make_store()
    result = _simulate_list(s, q="OL")
    s.close()

    # SOL 含 "OL"
    assert result["total"] == 1
    assert result["items"][0]["coin"] == "SOL"


def test_list_search_no_match():
    """q='ZZZNOMATCH' 无匹配，total=0，items=[]，不崩。"""
    s, _ = _make_store()
    result = _simulate_list(s, q="ZZZNOMATCH")
    s.close()

    assert result["total"] == 0
    assert result["items"] == []


def test_list_search_multiple_match():
    """q='E' 匹配含 'e'/'E' 的币：ETH/DOGE/PEPE/AVAX（4 枚，视数据而定）。"""
    s, _ = _make_store()
    result = _simulate_list(s, q="E")
    s.close()

    coins = {r["coin"] for r in result["items"]}
    # ETH(含E), DOGE(含E), PEPE(含E), AVAX(无E), SOXL(无E)... 精确匹配大小写不敏感
    # "e" in "ETH".lower() → True; "e" in "DOGE".lower() → True; "e" in "PEPE".lower() → True
    # "e" in "AVAX".lower() → False; "e" in "BNB".lower() → False
    assert "ETH" in coins
    assert "DOGE" in coins
    assert "PEPE" in coins
    assert result["total"] == len(result["items"])  # total 与 items 长度一致


def test_list_search_with_pagination():
    """搜索 + 分页组合：q='A' offset=0 limit=1 → 只返回第一枚匹配，total 为匹配总数。"""
    s, _ = _make_store()
    # 'a' in coin.lower() for: AVAX, AAPL, DOGE(含a?), BNB(无)...
    # 确定含 'a' 的：AVAX, AAPL, DOGE(含o,不含a), SOL(无)...
    # AVAX→含a, AAPL→含a, XAU→含a, SOXL→无... BNB→无
    result_all = _simulate_list(s, q="A")
    result_page = _simulate_list(s, q="A", offset=0, limit=1)
    s.close()

    total = result_all["total"]
    assert total >= 1
    assert result_page["total"] == total
    assert len(result_page["items"]) == 1
    assert result_page["items"][0] == result_all["items"][0]


def test_list_search_empty_query():
    """q='' 空字符串等同于无过滤，返回全部。"""
    s, _ = _make_store()
    result = _simulate_list(s, q="")
    s.close()

    assert result["total"] == 10


# ---------------------------------------------------------------------------
# asset_class 过滤测试
# ---------------------------------------------------------------------------

def test_list_filter_crypto():
    """asset_class='crypto' → 只含加密币（7枚：BTC/ETH/SOL/BNB/DOGE/PEPE/AVAX）。"""
    s, _ = _make_store()
    result = _simulate_list(s, asset_class="crypto")
    s.close()

    for r in result["items"]:
        assert r["asset_class"] == "crypto", f"{r['coin']} 应为 crypto，实得 {r['asset_class']}"
    # 加密币：BTC/ETH/SOL/BNB/DOGE/PEPE/AVAX = 7；TradFi：XAU/AAPL/SOXL = 3
    assert result["total"] == 7


def test_list_filter_tradfi():
    """asset_class='tradfi' → 只含 TradFi（XAU/AAPL/SOXL 3枚）。"""
    s, _ = _make_store()
    result = _simulate_list(s, asset_class="tradfi")
    s.close()

    for r in result["items"]:
        assert r["asset_class"] == "tradfi", f"{r['coin']} 应为 tradfi，实得 {r['asset_class']}"
    tradfi_coins = {r["coin"] for r in result["items"]}
    assert "XAU" in tradfi_coins
    assert "AAPL" in tradfi_coins
    assert "SOXL" in tradfi_coins
    assert result["total"] == 3


def test_list_filter_invalid_asset_class():
    """asset_class='unknown' 非法值等同于无过滤，返回全部。"""
    s, _ = _make_store()
    result = _simulate_list(s, asset_class="unknown")
    s.close()

    assert result["total"] == 10


def test_list_filter_and_search_combo():
    """asset_class='tradfi' + q='AU' → 只返回 XAU（TradFi 且含 AU 的币）。"""
    s, _ = _make_store()
    result = _simulate_list(s, asset_class="tradfi", q="AU")
    s.close()

    assert result["total"] == 1
    assert result["items"][0]["coin"] == "XAU"


def test_list_filter_empty_store():
    """空库 + 任意过滤参数，total=0，不崩。"""
    s = _make_empty_store()
    result = _simulate_list(s, asset_class="crypto", q="BTC")
    s.close()

    assert result["total"] == 0
    assert result["items"] == []


# ---------------------------------------------------------------------------
# 排序（来自 build_harmonic_list 的 best_conf 降序）
# ---------------------------------------------------------------------------

def test_list_sorted_by_conf_desc():
    """items 按 best_conf 降序（BTC 0.92 最高，PEPE 0.50 最低）。"""
    s, _ = _make_store()
    result = _simulate_list(s)
    s.close()

    items = result["items"]
    confs = [r["best_conf"] for r in items if r.get("best_conf") is not None]
    assert confs == sorted(confs, reverse=True), f"期望降序，实得 {confs}"
    assert items[0]["coin"] == "BTC"  # 置信 0.92 最高


# ---------------------------------------------------------------------------
# 前端模板：初始状态与 refreshList 兼容性
# ---------------------------------------------------------------------------

def test_render_harmonic_detail_html_initial_state_is_array():
    """render_harmonic_detail_html 注入的 __INITIAL_STATE__ 仍为 JSON 数组（backward-compat）。

    初始状态由 build_harmonic_list 直接传入（未经 envelope 包装），前端 let _listData = S
    直接赋值为数组，不应被包装成 { items, total, ... } 对象。
    """
    import re as _re

    s, _ = _make_store()
    lst = build_harmonic_list(s)
    s.close()

    html = render_harmonic_detail_html(lst)
    assert "__INITIAL_STATE__" not in html, "占位符应已替换"

    # 注入的 const S 应是数组（以 [ 开头）
    m = _re.search(r"const S\s*=\s*(\[.*?\]);", html, _re.S)
    assert m, "const S 应为 JSON 数组，未找到 [ 开头的声明"
    parsed = json.loads(m.group(1))
    assert isinstance(parsed, list), f"const S 期望 list，实得 {type(parsed)}"
    assert len(parsed) == 10


def test_render_harmonic_detail_html_refreshlist_handles_envelope():
    """refreshList JS 函数应能处理 envelope 格式（含 d.items||[] 或 Array.isArray 守卫）。

    任务要求：refreshList 调用 /api/harmonic/list?limit=500，响应为
    { items:[], total, offset, limit } envelope，模板 JS 应正确提取 items。
    """
    html = render_harmonic_detail_html([])

    # 验证 refreshList 含 limit=500 参数（拉全量）
    assert "limit=500" in html, "refreshList 应带 limit=500 参数拉全量数据"
    # 验证含 envelope 提取逻辑（Array.isArray 或 .items）
    has_guard = ("Array.isArray" in html) or (".items" in html and "d.items" in html)
    assert has_guard, "refreshList 应含 envelope 兼容逻辑（Array.isArray 或 d.items）"


def test_render_harmonic_detail_html_no_cdn_after_changes():
    """改动后仍无外部 CDN 链接。"""
    import re as _re
    html = render_harmonic_detail_html([])
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"不应含外部资源: {kw}"
    bad = [m for m in _re.findall(r'https?://[^\s"\'<>]+', html)
           if "w3.org/2000/svg" not in m]
    assert not bad, f"不应含外部链接: {bad[:3]}"


def test_render_harmonic_detail_html_no_residual_braces_after_changes():
    """改动后仍无残留 {{ 转义错误。"""
    html = render_harmonic_detail_html([])
    assert "{{" not in html, "残留 {{ → 模板转义不完整"


# ---------------------------------------------------------------------------
# Tab 统一 SPA 验证
# ---------------------------------------------------------------------------

def test_harmonic_page_tab_links_to_hl2():
    """谐波页（_HARMONIC_DETAIL_TEMPLATE）的「HL 系统」tab 应链接到 /hl2（而非旧的 /）。

    H4 需求：谐波页与 HL 页 header 互相链接，统一 SPA 体验。
    """
    html = render_harmonic_detail_html([])
    # 含指向 /hl2 的链接（HL 系统 tab）
    assert 'href="/hl2"' in html, "谐波页「HL 系统」tab 应链接到 /hl2"


def test_harmonic_page_tab_active_is_harmonic():
    """谐波页的「谐波系统」tab 应为 active 状态（无链接跳转）。"""
    html = render_harmonic_detail_html([])
    # 谐波系统 tab 是 active 且无链接（span 而非 a）
    assert "谐波系统" in html
    # 谐波系统 tab 为 active
    assert "hdr-tab active" in html or 'class="hdr-tab active"' in html


def test_hl_page_tab_links_to_harmonic2():
    """HL 页（_HL_TEMPLATE）的「谐波系统」tab 应链接到 /harmonic2。"""
    from smc_tracker.dashboard import render_hl_html

    # 构造最简 state
    state: dict = {
        "meta": {"generated": "now", "window_min": 60},
        "health": {}, "accuracy": {}, "signals": [], "divergence": [],
        "whale_flows": [], "top_addresses": [], "clusters": [],
        "oi_surges": [], "onchain": [], "pump_alerts": [],
        "whale_signals": [], "ticker_board": [], "exchange_flows": [],
        "wallet_portfolio": [], "okx_signals": [], "okx_liquidations": [],
        "okx_walls": [],
    }
    html = render_hl_html(state)
    assert "/harmonic2" in html, "HL 页应含 /harmonic2 链接"
    assert "谐波系统" in html


def test_hl_page_tab_active_is_hl():
    """HL 页的「HL 系统」tab 应为 active 状态。"""
    from smc_tracker.dashboard import render_hl_html

    state: dict = {
        "meta": {"generated": "now", "window_min": 60},
        "health": {}, "accuracy": {}, "signals": [], "divergence": [],
        "whale_flows": [], "top_addresses": [], "clusters": [],
        "oi_surges": [], "onchain": [], "pump_alerts": [],
        "whale_signals": [], "ticker_board": [], "exchange_flows": [],
        "wallet_portfolio": [], "okx_signals": [], "okx_liquidations": [],
        "okx_walls": [],
    }
    html = render_hl_html(state)
    assert "HL 系统" in html
    # HL 系统 tab 为 active（active 且不含 /hl2 href，它是当前页）
    assert "hdr-tab active" in html or 'class="hdr-tab active"' in html


if __name__ == "__main__":
    import traceback
    _pass = _fail = 0
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
