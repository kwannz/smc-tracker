# 谐波主-详情 SVG 前端 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a master-detail SPA at `/harmonic2` (or replace `/harmonic`) with a coin list on the left, an inline SVG candlestick chart with XABCD overlays on the right, multi-timeframe S/R table, setup detail, and history — all self-contained, no CDN.

**Architecture:** The new page is driven by two new API endpoints (`/api/harmonic/list`, `/api/harmonic/coin/<coin>`) and a new HTML render function `render_harmonic_detail_html`. Backend helpers `build_harmonic_list` and `build_coin_detail` read from the already-populated DB through the existing `Store` interface. The old `/harmonic` route and `render_harmonic_html`/`build_harmonic_state` are kept intact so that existing tests don't break.

**Tech Stack:** Python 3.12, aiohttp, SQLite (via `Store`), pure-JS inline SVG (createElementNS strings), pytest/gtest style unit tests, no CDN.

---

## Global Constraints

- Only modify: `src/smc_tracker/dashboard.py`, `tests/test_dashboard.py`
- Do NOT break existing routes `/`, `/api/state`, `/health`, `/harmonic`, `/api/harmonic`
- Do NOT import CDN resources — page must be self-contained
- Dark theme CSS vars already defined: `--bg:#0d1117`, `--green:#3fb950`, `--red:#f85149`, etc.
- Python: `{{`/`}}` double-brace escaping in `_HTML_TEMPLATE` strings is mandatory; inject JSON after `.replace("{{","{").replace("}}","}")`, then `.replace("__INITIAL_STATE__", json_str)` — same pattern as `render_html`
- None values must display as `—` (em dash) in JS, never as `"None"` or `"null"`
- All DB queries wrapped in `try/except` returning `[]`/`{}` on failure (same as `_safe_rows`)
- pytest baseline: **1208 passed** — must not decrease
- `PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py -q` must be all green
- Full `pytest -q` must show ≥1208 passed, 0 failed

---

## File Structure

| File | What Changes |
|---|---|
| `src/smc_tracker/dashboard.py` | Add `build_harmonic_list`, `build_coin_detail`, `render_harmonic_detail_html`, `_HARMONIC_DETAIL_TEMPLATE`, plus two new route handlers and router registrations |
| `tests/test_dashboard.py` | Add test functions for the new backend functions and the new render function; keep all existing tests |

---

### Task 1: Backend — `build_harmonic_list`

**Files:**
- Modify: `src/smc_tracker/dashboard.py` (after line 1067, i.e. after `build_harmonic_state`)
- Modify: `tests/test_dashboard.py` (append new tests)

**Interfaces:**
- Produces: `build_harmonic_list(store: Any) -> list[dict]`
  - Each dict: `{coin: str, asset_class: str, best_conf: float|None, direction: str|None, n_setups: int, has_completed: bool}`
  - Sorted by `best_conf` descending (None last)
- Consumes: `store.recent_harmonic_setups() -> list[tuple]` (29-col tuples per `_HARMONIC_KEYS`)
- Consumes: `asset_class(coin) -> str` (already imported in `build_harmonic_state`)

- [ ] **Step 1: Write the failing tests**

Open `tests/test_dashboard.py`. Append the following after the last existing test (line 1321):

```python
# ---------------------------------------------------------------------------
# 新: build_harmonic_list / build_coin_detail / render_harmonic_detail_html
# ---------------------------------------------------------------------------

from smc_tracker.dashboard import build_harmonic_list, build_coin_detail, render_harmonic_detail_html  # noqa: E402


def _store_with_harmonic_multi() -> tuple:
    """建含多币谐波数据的临时 Store（BTC/ETH/XAU）及对应 bb_levels 和 candles。"""
    import tempfile
    from pathlib import Path
    d = tempfile.mkdtemp()
    s = Store(Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    # 插入 BTC completed(long,0.82) + ETH forming(short,0.65) + XAU completed(long,0.78)
    s.insert_harmonic_setups([
        (now_ms - 120_000, "BTC", "1h", "completed", "Gartley", "long",
         65000.0, 64500.0, 64800.0, 63000.0, 67000.0, 69000.0,
         2.5, 0.82, "✓", "✓ 买压", "XA=0.618", 64000.0, 65200.0,
         1, 60000.0, 10, 70000.0, 15, 55000.0, 20, 65000.0, 25, 64500.0),
        (now_ms - 60_000, "ETH", "4h", "forming", "Bat", "short",
         3500.0, None, None, None, None, None, None,
         0.65, "?", "", "BC=0.886", 3450.0, 3550.0,
         None, None, None, None, None, None, None, None, None, None),
        (now_ms - 60_000, "XAU", "1h", "completed", "Gartley", "long",
         2350.0, 2340.0, 2350.0, 2300.0, 2400.0, 2450.0,
         2.0, 0.78, "✓", "✓ 买压", "XA=0.618", 2330.0, 2360.0,
         1, 2200.0, 10, 2450.0, 15, 2280.0, 20, 2420.0, 25, 2340.0),
    ])

    # 插入 BTC bb_levels（两个周期）
    s.insert_bb_levels([
        ("BTC", "1h", now_ms, 66000.0, 65000.0, 64000.0, 0.6, False),
        ("BTC", "4h", now_ms, 68000.0, 65500.0, 63000.0, 0.4, True),
    ])

    # 插入 BTC candles（5根 1h K线）
    s.upsert_candles([
        ("BTC", "1h", now_ms - 300_000, 64800.0, 65200.0, 64600.0, 65100.0, 1000.0),
        ("BTC", "1h", now_ms - 240_000, 65100.0, 65400.0, 64900.0, 65300.0, 1200.0),
        ("BTC", "1h", now_ms - 180_000, 65300.0, 65500.0, 65000.0, 65200.0, 900.0),
        ("BTC", "1h", now_ms - 120_000, 65200.0, 65600.0, 64800.0, 64900.0, 1100.0),
        ("BTC", "1h", now_ms - 60_000,  64900.0, 65100.0, 64700.0, 65000.0, 800.0),
    ])
    s.conn.commit()
    return s, now_ms


# ---- build_harmonic_list ----

def test_build_harmonic_list_returns_list():
    """build_harmonic_list 返回 list。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    assert isinstance(result, list)


def test_build_harmonic_list_has_all_coins():
    """每个出现在 recent_harmonic_setups 中的 coin 都应有一条汇总行。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    coins = {r["coin"] for r in result}
    assert "BTC" in coins
    assert "ETH" in coins
    assert "XAU" in coins


def test_build_harmonic_list_structure():
    """每项含必要字段，类型正确。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    for r in result:
        assert "coin" in r
        assert "asset_class" in r and r["asset_class"] in ("tradfi", "crypto")
        assert "best_conf" in r
        assert "direction" in r
        assert "n_setups" in r and isinstance(r["n_setups"], int) and r["n_setups"] >= 1
        assert "has_completed" in r and isinstance(r["has_completed"], bool)


def test_build_harmonic_list_sorted_by_conf_desc():
    """按 best_conf 降序排列（BTC 0.82 > XAU 0.78 > ETH 0.65）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    confs = [r["best_conf"] for r in result if r["best_conf"] is not None]
    assert confs == sorted(confs, reverse=True), f"应按置信降序排列，实得 {confs}"


def test_build_harmonic_list_has_completed_flag():
    """BTC/XAU 是 completed → has_completed=True；ETH 是 forming → has_completed=False。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    by_coin = {r["coin"]: r for r in result}
    assert by_coin["BTC"]["has_completed"] is True
    assert by_coin["ETH"]["has_completed"] is False
    assert by_coin["XAU"]["has_completed"] is True


def test_build_harmonic_list_asset_class_xau():
    """XAU → asset_class='tradfi'。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    by_coin = {r["coin"]: r for r in result}
    assert by_coin["XAU"]["asset_class"] == "tradfi"
    assert by_coin["BTC"]["asset_class"] == "crypto"


def test_build_harmonic_list_empty_store():
    """空库时返回 []，不抛异常。"""
    s = _store_empty()
    result = build_harmonic_list(s)
    s.close()
    assert result == []
```

- [ ] **Step 2: Run to verify RED**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py::test_build_harmonic_list_returns_list -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'build_harmonic_list'` or `FAILED`.

- [ ] **Step 3: Implement `build_harmonic_list` in dashboard.py**

After the closing brace of `build_harmonic_state` (around line 1067), add:

```python
def build_harmonic_list(store: Any) -> list[dict]:
    """聚合 recent_harmonic_setups → 每币一条汇总行，按 best_conf 降序。

    返回字段：coin, asset_class, best_conf, direction, n_setups, has_completed。
    表缺/空时返回 []，不抛。
    """
    from .asset_class import asset_class as _asset_class

    try:
        rows = store.recent_harmonic_setups()
    except Exception:  # noqa: BLE001
        return []

    # 按 coin 聚合
    agg: dict[str, dict] = {}
    for r in rows:
        d = _row_to_dict(r, _HARMONIC_KEYS)
        coin = d.get("coin") or ""
        if coin not in agg:
            agg[coin] = {
                "coin": coin,
                "asset_class": _asset_class(coin),
                "best_conf": None,
                "direction": None,
                "n_setups": 0,
                "has_completed": False,
            }
        entry = agg[coin]
        entry["n_setups"] += 1
        conf = d.get("confidence")
        if conf is not None:
            if entry["best_conf"] is None or conf > entry["best_conf"]:
                entry["best_conf"] = conf
                entry["direction"] = d.get("direction")
        if d.get("kind") == "completed":
            entry["has_completed"] = True

    # 按 best_conf 降序（None 排最后）
    result = list(agg.values())
    result.sort(key=lambda x: (x["best_conf"] is None, -(x["best_conf"] or 0)))
    return result
```

- [ ] **Step 4: Run tests to verify GREEN**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py -k "harmonic_list" -v 2>&1 | tail -15
```

Expected: all `test_build_harmonic_list_*` pass.

---

### Task 2: Backend — `build_coin_detail`

**Files:**
- Modify: `src/smc_tracker/dashboard.py` (after `build_harmonic_list`)
- Modify: `tests/test_dashboard.py` (append after Task 1 tests)

**Interfaces:**
- Produces: `build_coin_detail(store: Any, coin: str, tf: str | None = None) -> dict`
  - Keys: `coin`, `asset_class`, `tf`, `tfs_available`, `candles`, `setups`, `sr`, `history`
  - `candles`: `list[list]` — each item `[open_time_ms, o, h, l, c, v]` (all floats)
  - `setups`: `list[dict]` — same 29 fields as `_HARMONIC_KEYS` plus `asset_class`
  - `sr`: `list[dict]` — each `{tf, upper, lower, pct_b, squeeze}`
  - `history`: `list[dict]` — 30 rows from `harmonic_history(coin, 30)` using same 29+1 fields
- Consumes: `store.recent_harmonic_setups()`, `store.recent_bb_levels(coin)`, `store.get_candles(coin, tf, 200)`, `store.harmonic_history(coin, 30)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
# ---- build_coin_detail ----

def test_build_coin_detail_returns_dict():
    """build_coin_detail 返回 dict，含必要顶层键。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    assert isinstance(result, dict)
    for key in ("coin", "asset_class", "tf", "tfs_available", "candles", "setups", "sr", "history"):
        assert key in result, f"缺少键: {key}"


def test_build_coin_detail_coin_field():
    """coin 字段与参数一致。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    assert result["coin"] == "BTC"


def test_build_coin_detail_asset_class():
    """BTC → crypto；XAU → tradfi。"""
    s, _ = _store_with_harmonic_multi()
    btc = build_coin_detail(s, "BTC")
    xau = build_coin_detail(s, "XAU")
    s.close()
    assert btc["asset_class"] == "crypto"
    assert xau["asset_class"] == "tradfi"


def test_build_coin_detail_tf_defaults_to_first_setup():
    """tf 未传时，应等于该币在 recent_harmonic_setups 中第一个 setup 的 tf。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    # BTC setup tf='1h'
    assert result["tf"] == "1h"


def test_build_coin_detail_tfs_available():
    """tfs_available 是该币所有 setup 的 tf 列表（不重复）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    assert isinstance(result["tfs_available"], list)
    assert "1h" in result["tfs_available"]


def test_build_coin_detail_candles_structure():
    """candles 是 list of [ts, o, h, l, c, v]（已插入5根）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC", tf="1h")
    s.close()
    candles = result["candles"]
    assert isinstance(candles, list)
    assert len(candles) == 5
    for c in candles:
        assert len(c) == 6, f"蜡烛行应有6列，实得{len(c)}"
        assert all(v is not None for v in c), "蜡烛值不应为 None"


def test_build_coin_detail_setups_structure():
    """setups 含 BTC 1h 的谐波 setup，含 XABCD 点。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC", tf="1h")
    s.close()
    setups = result["setups"]
    assert len(setups) >= 1
    setup = setups[0]
    for field in _HARMONIC_KEYS:
        assert field in setup, f"setup 缺少字段: {field}"
    assert "asset_class" in setup
    # XABCD 点有值（BTC completed 行含完整 XABCD）
    assert setup["x_px"] == 60000.0
    assert setup["d_px"] == 64500.0


def test_build_coin_detail_sr():
    """sr 含 BTC bb_levels（两个周期）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    sr = result["sr"]
    assert isinstance(sr, list)
    assert len(sr) == 2
    for item in sr:
        for field in ("tf", "upper", "lower", "pct_b", "squeeze"):
            assert field in item, f"sr 条目缺少字段: {field}"


def test_build_coin_detail_history():
    """history 含该币历史形态 list[dict]（BTC 有1条）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    history = result["history"]
    assert isinstance(history, list)
    assert len(history) >= 1
    assert history[0]["coin"] == "BTC"


def test_build_coin_detail_empty_store():
    """空库时各字段为空 list，不抛。"""
    s = _store_empty()
    result = build_coin_detail(s, "NONEXISTENT")
    s.close()
    assert result["candles"] == []
    assert result["setups"] == []
    assert result["sr"] == []
    assert result["history"] == []


def test_build_coin_detail_unknown_tf():
    """传入不存在的 tf 时 candles=[]，不抛。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC", tf="NOPE")
    s.close()
    assert result["candles"] == []
```

- [ ] **Step 2: Run to verify RED**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py::test_build_coin_detail_returns_dict -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'build_coin_detail'` or `FAILED`.

- [ ] **Step 3: Implement `build_coin_detail` in dashboard.py**

After `build_harmonic_list`, add:

```python
def build_coin_detail(store: Any, coin: str, tf: str | None = None) -> dict:
    """组装指定 coin（和 tf）的详情数据：蜡烛/setup/S/R/历史。

    tf 缺省时取该币在 recent_harmonic_setups 中首个 setup 的 tf。
    表缺/空时各字段返回 []，不抛。
    """
    from .asset_class import asset_class as _asset_class

    # 1. 读该币全部最新 setup 行（按 tf 过滤）
    all_setups: list[dict] = []
    tfs_available: list[str] = []
    resolved_tf = tf
    try:
        for r in store.recent_harmonic_setups():
            d = _row_to_dict(r, _HARMONIC_KEYS)
            if d.get("coin") != coin:
                continue
            d["asset_class"] = _asset_class(coin)
            all_setups.append(d)
            t = d.get("tf") or ""
            if t and t not in tfs_available:
                tfs_available.append(t)
    except Exception:  # noqa: BLE001
        pass

    # tf 缺省 → 用该币第一个 setup 的 tf
    if not resolved_tf:
        resolved_tf = tfs_available[0] if tfs_available else ""

    # 只保留目标 tf 的 setup
    setups = [d for d in all_setups if d.get("tf") == resolved_tf]

    # 2. 蜡烛（200 根）
    candles: list[list] = []
    if resolved_tf:
        try:
            raw_candles = store.get_candles(coin, resolved_tf, 200)
            candles = [
                [c.open_time_ms, c.o, c.h, c.l, c.c, c.v]
                for c in raw_candles
            ]
        except Exception:  # noqa: BLE001
            pass

    # 3. S/R（该币所有 tf 的最新 bb_levels）
    sr: list[dict] = []
    try:
        for r in store.recent_bb_levels(coin):
            sr.append({
                "tf":      r[1],
                "upper":   r[3],
                "lower":   r[5],
                "pct_b":   r[6],
                "squeeze": r[7],
            })
    except Exception:  # noqa: BLE001
        pass

    # 4. 历史形态
    history: list[dict] = []
    try:
        for r in store.harmonic_history(coin, 30):
            d = _row_to_dict(r, _HARMONIC_KEYS)
            d["asset_class"] = _asset_class(coin)
            history.append(d)
    except Exception:  # noqa: BLE001
        pass

    return {
        "coin": coin,
        "asset_class": _asset_class(coin),
        "tf": resolved_tf,
        "tfs_available": tfs_available,
        "candles": candles,
        "setups": setups,
        "sr": sr,
        "history": history,
    }
```

- [ ] **Step 4: Run tests to verify GREEN**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py -k "coin_detail" -v 2>&1 | tail -20
```

Expected: all `test_build_coin_detail_*` pass.

---

### Task 3: Frontend HTML template `render_harmonic_detail_html`

**Files:**
- Modify: `src/smc_tracker/dashboard.py` (after `build_coin_detail`, add `_HARMONIC_DETAIL_TEMPLATE` and `render_harmonic_detail_html`)
- Modify: `tests/test_dashboard.py` (append render tests)

**Interfaces:**
- Produces: `render_harmonic_detail_html(list_state: list[dict]) -> str`
  - `list_state` is the output of `build_harmonic_list(store)` — the left-panel initial coin list
  - The page fetches `/api/harmonic/list` and `/api/harmonic/coin/<coin>` dynamically
- The template uses `{{`/`}}` escaping (same as `_HTML_TEMPLATE`), `__INITIAL_STATE__` placeholder

**Key front-end behaviors to implement in the template's JS section:**

1. Left panel: renders each entry from `list_state` as a clickable row. Shows `coin`, asset badge (`🏦TradFi` / `₿加密`), confidence as `%`, direction color (green=看多, red=看空). Filter buttons: 全部/加密/TradFi/有完整形态. Sort by confidence.
2. 5s poll of `/api/harmonic/list` to refresh left panel without losing selected coin.
3. Right panel: clicking a coin calls `fetchCoinDetail(coin, tf)` → `GET /api/harmonic/coin/<coin>?tf=<tf>`. Renders:
   - Timeframe tabs (loop `detail.tfs_available`).
   - Inline SVG candlestick chart: OHLC bars (green candle if close≥open, red otherwise), high/low wicks (vertical lines), price Y-axis auto-scaled to min/max with margin, time X-axis implicit. SVG built as a string using `<svg>`, `<rect>`, `<line>`, `<polyline>`, `<text>` tags. Overlaid on the same SVG: XABCD points (circles + labels), XABCD polyline connecting them, PRZ band (semi-transparent rect), entry/stop/target1/target2 horizontal dashed lines, S/R lines for current tf (upper=red pressure, lower=green support).
   - Multi-tf S/R table (7 periods).
   - Setup detail table: entry range / stop / target1 / target2 / RR / confidence / KNN / orderflow.
   - History list.
   - Collapsible explainer (`<details>`) with term definitions and honest disclaimer.
4. None values → `—` (use JS helper `fmtN(v, dec)` returning `'—'` when null).
5. Self-contained: no CDN. SVG namespace `http://www.w3.org/2000/svg` is allowed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
# ---- render_harmonic_detail_html ----

def _detail_list_state() -> list[dict]:
    """合成 list_state（供 render_harmonic_detail_html 首屏注入）。"""
    return [
        {"coin": "BTC", "asset_class": "crypto",  "best_conf": 0.82,
         "direction": "long",  "n_setups": 1, "has_completed": True},
        {"coin": "XAU", "asset_class": "tradfi",  "best_conf": 0.78,
         "direction": "long",  "n_setups": 1, "has_completed": True},
        {"coin": "ETH", "asset_class": "crypto",  "best_conf": 0.65,
         "direction": "short", "n_setups": 1, "has_completed": False},
    ]


def test_render_harmonic_detail_html_returns_str():
    """render_harmonic_detail_html 返回非空字符串。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert isinstance(html, str) and len(html) > 100


def test_render_harmonic_detail_html_doctype():
    """应是完整独立页面（含 <!DOCTYPE html>）。"""
    html = render_harmonic_detail_html([])
    assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()


def test_render_harmonic_detail_html_dark_theme():
    """含深色主题 CSS 变量 --bg。"""
    html = render_harmonic_detail_html([])
    assert "--bg" in html


def test_render_harmonic_detail_html_no_cdn():
    """自包含：不含外部 CDN/http 链接（w3.org SVG 命名空间除外）。"""
    import re
    html = render_harmonic_detail_html(_detail_list_state())
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"不应含外部资源: {kw}"
    bad = [m for m in re.findall(r'https?://[^\s"\'<>]+', html)
           if "w3.org/2000/svg" not in m]
    assert not bad, f"不应含外部链接: {bad[:3]}"


def test_render_harmonic_detail_html_svg_elements():
    """模板 JS 中应含 SVG 核心元素 <svg、<rect、<line、<polyline（蜡烛图定义）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    for tag in ("<svg", "<rect", "<line", "<polyline"):
        assert tag in html, f"HTML 应含 SVG 元素: {tag}"


def test_render_harmonic_detail_html_candle_word():
    """HTML 含「蜡烛」字样（表明渲染函数注释/label 存在）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "蜡烛" in html


def test_render_harmonic_detail_html_tf_tab():
    """HTML 含周期 tab 相关 JS（tfs_available）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "tfs_available" in html or "tab" in html.lower()


def test_render_harmonic_detail_html_bullbear_labels():
    """HTML 含「看多」和「看空」方向标签（JS 定义中）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "看多" in html
    assert "看空" in html


def test_render_harmonic_detail_html_tradfi_badge():
    """HTML 含「TradFi」和「加密」徽章定义（JS 中 badgeHtml 或等效函数）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "TradFi" in html
    assert "加密" in html


def test_render_harmonic_detail_html_orderflow():
    """HTML 含「订单流」字样（setup detail 或 explainer）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "订单流" in html


def test_render_harmonic_detail_html_disclaimer():
    """HTML 含「确认层非投资建议」诚实声明。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "确认层非投资建议" in html or "非投资建议" in html


def test_render_harmonic_detail_html_set_interval():
    """HTML 含 setInterval（5s 轮询）。"""
    html = render_harmonic_detail_html([])
    assert "setInterval" in html


def test_render_harmonic_detail_html_api_list_fetch():
    """HTML 拉取 /api/harmonic/list（左面板刷新）。"""
    html = render_harmonic_detail_html([])
    assert "/api/harmonic/list" in html


def test_render_harmonic_detail_html_api_coin_fetch():
    """HTML 拉取 /api/harmonic/coin/（右面板详情）。"""
    html = render_harmonic_detail_html([])
    assert "/api/harmonic/coin/" in html


def test_render_harmonic_detail_html_no_residual_double_braces():
    """模板解转义完整：不含残留 {{ 。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "{{" not in html, "残留 {{ → 模板转义不完整"


def test_render_harmonic_detail_html_initial_state_injected():
    """__INITIAL_STATE__ 占位符已被替换为可解析 JSON。"""
    import json as _json, re as _re
    html = render_harmonic_detail_html(_detail_list_state())
    assert "__INITIAL_STATE__" not in html
    m = _re.search(r"const S\s*=\s*(\[.*?\]);", html, _re.S)
    assert m, "未找到注入的 const S（应为 JSON array）"
    parsed = _json.loads(m.group(1))
    assert isinstance(parsed, list)
    assert parsed[0]["coin"] == "BTC"


def test_render_harmonic_detail_html_filter_buttons():
    """HTML 含过滤按钮：全部/加密/TradFi/有完整形态。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "全部" in html
    assert "有完整形态" in html


def test_render_harmonic_detail_html_none_as_dash():
    """JS 工具函数中 null → '—'（fmtN 或等效守卫）。"""
    html = render_harmonic_detail_html([])
    # 验证 JS 中有对 null/undefined 的守卫，返回 em dash
    assert "—" in html or "&#x2014;" in html


def test_render_harmonic_detail_html_prz_band():
    """HTML JS 中含 PRZ（潜在反转区带）相关代码。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "prz" in html.lower() or "PRZ" in html
```

- [ ] **Step 2: Run to verify RED**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py::test_render_harmonic_detail_html_returns_str -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'render_harmonic_detail_html'` or `FAILED`.

- [ ] **Step 3: Implement `_HARMONIC_DETAIL_TEMPLATE` and `render_harmonic_detail_html` in dashboard.py**

After `build_coin_detail`, add the template and function. The template is a large string constant. Here is the complete implementation to add:

```python
# ---------------------------------------------------------------------------
# 谐波主-详情 SPA 模板（左列表 + 右 SVG 蜡烛详情，无 CDN）
# ---------------------------------------------------------------------------
_HARMONIC_DETAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>谐波主-详情</title>
<style>
:root{{
  --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;
  --muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;
  --yellow:#e3b341;--purple:#bc8cff;--orange:#ffa657;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);
  font-family:"SF Mono",ui-monospace,monospace;font-size:13px;line-height:1.5;
  display:flex;flex-direction:column;height:100vh;overflow:hidden}}
header{{padding:10px 16px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:12px;flex-shrink:0}}
h1{{font-size:17px;color:var(--blue)}}
#meta{{color:var(--muted);font-size:11px;margin-left:auto}}
.layout{{display:flex;flex:1;overflow:hidden}}
/* 左面板 */
#left{{width:260px;flex-shrink:0;border-right:1px solid var(--border);
  display:flex;flex-direction:column;overflow:hidden}}
#filters{{padding:6px 8px;border-bottom:1px solid var(--border);
  display:flex;flex-wrap:wrap;gap:4px}}
.filter-btn{{font-size:11px;padding:2px 7px;border-radius:3px;border:1px solid var(--border);
  background:transparent;color:var(--muted);cursor:pointer}}
.filter-btn.active{{border-color:var(--blue);color:var(--blue);background:#0e1f3a}}
#coin-list{{overflow-y:auto;flex:1}}
.coin-row{{padding:7px 10px;cursor:pointer;border-bottom:1px solid rgba(48,54,61,.6);
  display:flex;align-items:center;gap:6px}}
.coin-row:hover{{background:rgba(255,255,255,.04)}}
.coin-row.selected{{background:#0e1f3a;border-left:3px solid var(--blue)}}
.coin-name{{font-weight:700;color:var(--orange)}}
.conf-txt{{font-size:11px;color:var(--muted)}}
/* 右面板 */
#right{{flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:12px}}
#right-empty{{color:var(--muted);padding:40px;text-align:center;font-size:13px}}
/* 周期 tabs */
.tf-tabs{{display:flex;gap:4px;flex-wrap:wrap}}
.tf-tab{{font-size:11px;padding:3px 9px;border-radius:3px;border:1px solid var(--border);
  background:transparent;color:var(--muted);cursor:pointer}}
.tf-tab.active{{border-color:var(--blue);color:var(--blue);background:#0e1f3a}}
/* SVG 蜡烛图容器 */
#chart-wrap{{background:var(--card);border:1px solid var(--border);border-radius:6px;
  overflow:hidden;padding:6px}}
/* 表格卡片 */
.card{{background:var(--card);border:1px solid var(--border);border-radius:6px;overflow:hidden}}
.card-title{{padding:8px 12px;border-bottom:1px solid var(--border);
  font-weight:700;color:var(--blue);font-size:12px}}
.card-body{{padding:10px 12px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{color:var(--muted);font-weight:600;text-align:left;padding:3px 5px;
  border-bottom:1px solid var(--border)}}
td{{padding:3px 5px;vertical-align:top;white-space:nowrap}}
tr:hover td{{background:rgba(255,255,255,.03)}}
.long{{color:var(--green)}} .short{{color:var(--red)}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}}
.none{{color:var(--muted);font-style:italic}}
.tag{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;font-weight:600}}
.tag-long{{background:#1a3a2a;color:var(--green)}}
.tag-short{{background:#3a1a1a;color:var(--red)}}
.badge-tradfi{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;
  font-weight:700;background:#3a2400;color:var(--orange);margin-right:3px}}
.badge-crypto{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;
  font-weight:700;background:#0e1f3a;color:var(--blue);margin-right:3px}}
details.explainer{{background:var(--card);border:1px solid #2a3f55;border-radius:6px;overflow:hidden}}
details.explainer summary{{padding:8px 12px;cursor:pointer;font-weight:700;
  color:var(--blue);font-size:12px;list-style:none;user-select:none}}
details.explainer summary::-webkit-details-marker{{display:none}}
details.explainer summary::before{{content:"▶ ";font-size:10px;color:var(--muted)}}
details[open].explainer summary::before{{content:"▼ ";font-size:10px;color:var(--muted)}}
.explainer-body{{padding:10px 14px;font-size:11px;line-height:1.7;border-top:1px solid #2a3f55}}
.explainer-body dt{{font-weight:700;color:var(--yellow);margin-top:6px}}
.explainer-body dd{{margin-left:10px}}
.honest-note{{margin-top:8px;padding:6px 10px;background:#1c1a10;
  border-left:3px solid var(--yellow);color:var(--muted);font-size:11px}}
.disclaimer{{padding:7px 12px;background:#1c1a10;border:1px solid #5a4a00;
  border-radius:5px;color:var(--yellow);font-size:11px}}
#refresh-bar{{font-size:11px;color:var(--muted);padding:3px 16px;
  border-top:1px solid var(--border);flex-shrink:0}}
</style>
</head>
<body>
<header>
  <h1>🔷 谐波主-详情</h1>
  <a href="/harmonic" style="font-size:11px;color:var(--muted);text-decoration:none;
     border:1px solid var(--border);border-radius:3px;padding:2px 7px">旧版</a>
  <a href="/" style="font-size:11px;color:var(--muted);text-decoration:none;
     border:1px solid var(--border);border-radius:3px;padding:2px 7px">← HL 主页</a>
  <span id="meta">加载中…</span>
</header>
<div class="layout">
  <!-- 左面板 -->
  <div id="left">
    <div id="filters">
      <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">全部</button>
      <button class="filter-btn" data-filter="crypto" onclick="setFilter('crypto')">加密</button>
      <button class="filter-btn" data-filter="tradfi" onclick="setFilter('tradfi')">TradFi</button>
      <button class="filter-btn" data-filter="completed" onclick="setFilter('completed')">有完整形态</button>
    </div>
    <div id="coin-list"><!-- JS 渲染 --></div>
  </div>
  <!-- 右面板 -->
  <div id="right">
    <div id="right-empty">← 点击左侧币种查看详情</div>
  </div>
</div>
<div id="refresh-bar">左列表 5s 自动刷新 · 点击蜡烛图周期 tab 切换</div>
<script>
// 首屏注入左列表（array，非 object）
const S = __INITIAL_STATE__;

let _listData = S;       // 当前左列表数据
let _selectedCoin = '';  // 当前选中币
let _selectedTf  = '';   // 当前选中周期
let _curFilter   = 'all';// 当前过滤

// ---- 工具函数 ----
function fmtN(v, dec){{
  if(v==null||v===undefined)return'—';
  const n=parseFloat(v);
  return isNaN(n)?'—':n.toFixed(dec!=null?dec:4);
}}
function fmtPct(v){{
  if(v==null)return'—';
  return(parseFloat(v)*100).toFixed(1)+'%';
}}
function fmtTime(ms){{
  if(ms==null)return'—';
  const d=new Date(ms);
  return d.toLocaleTimeString('zh-CN',{{hour12:false}});
}}
function dirTag(d){{
  if(d==='long')return'<span class="tag tag-long">看多</span>';
  if(d==='short')return'<span class="tag tag-short">看空</span>';
  return d||'—';
}}
function badgeHtml(ac){{
  if(ac==='tradfi')return'<span class="badge-tradfi">🏦TradFi</span>';
  return'<span class="badge-crypto">₿加密</span>';
}}
function esc(s){{
  return String(s==null?'':s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

// ---- 左面板渲染 ----
function setFilter(f){{
  _curFilter=f;
  document.querySelectorAll('.filter-btn').forEach(b=>{{
    b.classList.toggle('active', b.dataset.filter===f);
  }});
  renderList(_listData);
}}
function renderList(rows){{
  let filtered=rows;
  if(_curFilter==='crypto')filtered=rows.filter(r=>r.asset_class==='crypto');
  else if(_curFilter==='tradfi')filtered=rows.filter(r=>r.asset_class==='tradfi');
  else if(_curFilter==='completed')filtered=rows.filter(r=>r.has_completed);
  // 按 best_conf 降序（null 排最后）
  filtered=[...filtered].sort((a,b)=>{{
    if(a.best_conf==null&&b.best_conf==null)return 0;
    if(a.best_conf==null)return 1;
    if(b.best_conf==null)return -1;
    return b.best_conf-a.best_conf;
  }});
  document.getElementById('coin-list').innerHTML=filtered.map(r=>{{
    const conf=r.best_conf!=null?Math.round(r.best_conf*100)+'%':'—';
    const dirCls=r.direction==='long'?'long':(r.direction==='short'?'short':'');
    const sel=r.coin===_selectedCoin?' selected':'';
    return`<div class="coin-row${{sel}}" onclick="selectCoin('${{esc(r.coin)}}')">
      ${{badgeHtml(r.asset_class)}}
      <span class="coin-name">${{esc(r.coin)}}</span>
      <span class="conf-txt ${{dirCls}}">${{conf}}</span>
      ${{r.direction==='long'?'<span class="long">▲</span>':r.direction==='short'?'<span class="short">▼</span>':''}}
    </div>`;
  }}).join('');
}}

// ---- 右面板：fetch 详情 ----
function selectCoin(coin, tf){{
  _selectedCoin=coin;
  if(tf)_selectedTf=tf;
  renderList(_listData);
  const url='/api/harmonic/coin/'+encodeURIComponent(coin)+(tf?'?tf='+encodeURIComponent(tf):'');
  fetch(url).then(r=>r.json()).then(d=>{{
    if(!tf)_selectedTf=d.tf||'';
    renderDetail(d);
  }}).catch(e=>{{
    document.getElementById('right').innerHTML='<div style="color:var(--red);padding:20px">加载失败: '+e+'</div>';
  }});
}}

// ---- SVG 蜡烛图 ----
function renderSvgCandles(candles, setups, sr, tf){{
  if(!candles||!candles.length)return'<div style="color:var(--muted);padding:20px">暂无K线数据（该周期未采集）</div>';
  const W=800, H=280, padL=60, padR=12, padT=12, padB=20;
  const n=candles.length;
  const cw=Math.max(2,Math.floor((W-padL-padR)/n)-1);
  const gap=Math.max(1,(W-padL-padR-n*cw)/(n-1||1));

  // 价格范围（含 setup 点，PRZ，S/R线）
  let lo=Infinity, hi=-Infinity;
  candles.forEach(c=>{{const h=parseFloat(c[2]),l=parseFloat(c[3]);if(h>hi)hi=h;if(l<lo)lo=l;}});
  setups.forEach(s=>{{
    [s.x_px,s.a_px,s.b_px,s.c_px,s.d_px,s.prz_lo,s.prz_hi,s.entry_lo,s.entry_hi,s.stop,s.target1,s.target2]
      .forEach(v=>{{if(v!=null){{const f=parseFloat(v);if(f>hi)hi=f;if(f<lo)lo=f;}}}});
  }});
  sr.filter(s=>s.tf===tf).forEach(s=>{{
    [s.upper,s.lower].forEach(v=>{{if(v!=null){{const f=parseFloat(v);if(f>hi)hi=f;if(f<lo)lo=f;}}}});
  }});
  const margin=(hi-lo)*0.05||1;
  hi+=margin; lo-=margin;
  const span=hi-lo||1;

  function py(price){{return padT+(hi-price)/span*(H-padT-padB);}}
  function px(i){{return padL+i*(cw+gap)+cw/2;}}

  let s=`<svg viewBox="0 0 ${{W}} ${{H}}" width="100%" height="${{H}}" xmlns="http://www.w3.org/2000/svg" style="display:block">`;

  // S/R 线（该 tf）
  sr.filter(r=>r.tf===tf).forEach(r=>{{
    if(r.upper!=null){{const y=py(r.upper);s+=`<line x1="${{padL}}" y1="${{y.toFixed(1)}}" x2="${{W-padR}}" y2="${{y.toFixed(1)}}" stroke="#f85149" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>`;}}
    if(r.lower!=null){{const y=py(r.lower);s+=`<line x1="${{padL}}" y1="${{y.toFixed(1)}}" x2="${{W-padR}}" y2="${{y.toFixed(1)}}" stroke="#3fb950" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>`;}}
  }});

  // Setup 水平虚线（进场/止损/目标）
  setups.forEach(su=>{{
    const lines=[
      [su.entry_lo,'#58a6ff',1.5,'6,3'],[su.stop,su.direction==='long'?'#f85149':'#3fb950',1,'4,3'],
      [su.target1,'#e3b341',1,'4,3'],[su.target2,'#bc8cff',1,'4,3'],
    ];
    lines.forEach(([v,c,w,da])=>{{
      if(v==null)return;
      const y=py(parseFloat(v));
      s+=`<line x1="${{padL}}" y1="${{y.toFixed(1)}}" x2="${{W-padR}}" y2="${{y.toFixed(1)}}" stroke="${{c}}" stroke-width="${{w}}" stroke-dasharray="${{da}}" opacity="0.85"/>`;
    }});
    // PRZ 区带
    if(su.prz_lo!=null&&su.prz_hi!=null){{
      const y1=py(Math.max(su.prz_lo,su.prz_hi)), y2=py(Math.min(su.prz_lo,su.prz_hi));
      s+=`<rect x="${{padL}}" y="${{y1.toFixed(1)}}" width="${{W-padL-padR}}" height="${{Math.max(1,(y2-y1)).toFixed(1)}}" fill="#58a6ff" opacity="0.08"/>`;
    }}
  }});

  // 蜡烛（OHLC）
  candles.forEach((c,i)=>{{
    const [ts,o,h,l,cl,v]=c.map(Number);
    const x=padL+i*(cw+gap);
    const cy=py(cl), oy=py(o);
    const top=Math.min(cy,oy), bh=Math.max(1,Math.abs(cy-oy));
    const color=cl>=o?'#3fb950':'#f85149';
    const mx=x+cw/2;
    // 影线
    s+=`<line x1="${{mx.toFixed(1)}}" y1="${{py(h).toFixed(1)}}" x2="${{mx.toFixed(1)}}" y2="${{py(l).toFixed(1)}}" stroke="${{color}}" stroke-width="1"/>`;
    // 实体
    s+=`<rect x="${{x.toFixed(1)}}" y="${{top.toFixed(1)}}" width="${{cw}}" height="${{bh.toFixed(1)}}" fill="${{color}}" rx="1"/>`;
  }});

  // XABCD polyline + 点 + 标注
  setups.forEach(su=>{{
    const pts=[
      [su.x_idx,su.x_px,'X'],[su.a_idx,su.a_px,'A'],
      [su.b_idx,su.b_px,'B'],[su.c_idx,su.c_px,'C'],[su.d_idx,su.d_px,'D'],
    ].filter(([idx,v])=>idx!=null&&v!=null);
    if(pts.length>1){{
      const poly=pts.map(([idx,v])=>`${{px(idx).toFixed(1)}},${{py(v).toFixed(1)}}`).join(' ');
      s+=`<polyline points="${{poly}}" fill="none" stroke="#ffa657" stroke-width="1.5" stroke-dasharray="3,2" opacity="0.9"/>`;
    }}
    pts.forEach(([idx,v,lbl])=>{{
      const cx=px(idx), cy2=py(v);
      s+=`<circle cx="${{cx.toFixed(1)}}" cy="${{cy2.toFixed(1)}}" r="4" fill="#ffa657" opacity="0.9"/>`;
      s+=`<text x="${{(cx+5).toFixed(1)}}" y="${{(cy2-5).toFixed(1)}}" fill="#ffa657" font-size="10" font-weight="bold">${{lbl}}</text>`;
    }});
  }});

  // Y 轴价格刻度（4~5 个）
  const nTicks=5;
  for(let i=0;i<=nTicks;i++){{
    const price=lo+(span*i/nTicks);
    const y=py(price);
    s+=`<text x="${{(padL-4).toFixed(1)}}" y="${{(y+4).toFixed(1)}}" fill="#8b949e" font-size="9" text-anchor="end">${{price>100?price.toFixed(0):price.toFixed(4)}}</text>`;
    s+=`<line x1="${{padL}}" y1="${{y.toFixed(1)}}" x2="${{W-padR}}" y2="${{y.toFixed(1)}}" stroke="#30363d" stroke-width="0.5"/>`;
  }}

  s+='</svg>';
  return s;
}}

// ---- 多周期 S/R 表 ----
function renderSrTable(sr){{
  if(!sr||!sr.length)return'<span class="none">（无 S/R 数据）</span>';
  let h='<table><tr><th>周期</th><th>压力(上轨)</th><th>支撑(下轨)</th><th>%B</th><th>挤压</th></tr>';
  sr.forEach(r=>{{
    h+=`<tr>
      <td style="color:var(--blue)">${{r.tf||'—'}}</td>
      <td class="neg">${{fmtN(r.upper,4)}}</td>
      <td class="pos">${{fmtN(r.lower,4)}}</td>
      <td>${{fmtN(r.pct_b,3)}}</td>
      <td>${{r.squeeze?'<span style="color:var(--yellow)">挤压</span>':'—'}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

// ---- Setup 明细 ----
function renderSetupDetail(setups){{
  if(!setups||!setups.length)return'<span class="none">（该周期无 setup）</span>';
  const su=setups[0];
  const entry=su.entry_lo!=null||su.entry_hi!=null
    ?(fmtN(su.entry_lo,4)+' ~ '+fmtN(su.entry_hi,4)):'—';
  const prz=su.prz_lo!=null||su.prz_hi!=null
    ?(fmtN(su.prz_lo,4)+' ~ '+fmtN(su.prz_hi,4)):'—';
  return`<table>
    <tr><th>形态</th><td>${{su.pattern||'—'}}</td><th>方向</th><td>${{dirTag(su.direction)}}</td></tr>
    <tr><th>进场区</th><td>${{entry}}</td><th>PRZ区</th><td>${{prz}}</td></tr>
    <tr><th>止损</th><td class="${{su.direction==='long'?'neg':'pos'}}">${{fmtN(su.stop,4)}}</td>
        <th>目标1</th><td class="pos">${{fmtN(su.target1,4)}}</td></tr>
    <tr><th>目标2</th><td class="pos">${{fmtN(su.target2,4)}}</td>
        <th>盈亏比</th><td class="pos">${{fmtN(su.rr,2)}}</td></tr>
    <tr><th>置信</th><td>${{su.confidence!=null?Math.round(su.confidence*100)+'%':'—'}}</td>
        <th>KNN</th><td>${{su.knn||'—'}}</td></tr>
    <tr><th>订单流</th><td colspan="3">${{su.orderflow||'—'}}</td></tr>
    <tr><th>Fib注记</th><td colspan="3">${{su.fib_note||'—'}}</td></tr>
  </table>`;
}}

// ---- 历史形态 ----
function renderHistory(history){{
  if(!history||!history.length)return'<span class="none">（暂无历史记录）</span>';
  let h='<table><tr><th>时间</th><th>周期</th><th>形态</th><th>方向</th><th>置信</th><th>状态</th></tr>';
  history.slice(0,15).forEach(r=>{{
    h+=`<tr>
      <td style="color:var(--muted)">${{fmtTime(r.ts)}}</td>
      <td style="color:var(--muted)">${{r.tf||'—'}}</td>
      <td>${{r.pattern||'—'}}</td>
      <td>${{dirTag(r.direction)}}</td>
      <td>${{r.confidence!=null?Math.round(r.confidence*100)+'%':'—'}}</td>
      <td style="color:var(--muted)">${{r.kind||'—'}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

// ---- 右面板主渲染 ----
function renderDetail(d){{
  if(!d){{document.getElementById('right').innerHTML='<div id="right-empty">← 点击左侧币种查看详情</div>';return;}}
  const coin=d.coin||'';
  const ac=d.asset_class||'crypto';
  const tf=d.tf||'';
  const tfs=d.tfs_available||[];

  // 周期 tabs
  const tabsHtml=tfs.length
    ?'<div class="tf-tabs">'+tfs.map(t=>
        `<button class="tf-tab${{t===tf?' active':''}}" onclick="selectCoin('${{esc(coin)}}','${{esc(t)}}')">${{t}}</button>`
      ).join('')+'</div>'
    :'';

  // SVG 蜡烛图
  const svgHtml=renderSvgCandles(d.candles,d.setups||[],d.sr||[],tf);

  const html=
    // 头部
    `<div style="display:flex;align-items:center;gap:8px">
       ${{badgeHtml(ac)}}<span style="font-size:16px;font-weight:700;color:var(--orange)">${{esc(coin)}}</span>
       <span style="color:var(--muted);font-size:12px">周期: ${{tf||'—'}}</span>
     </div>`+
    tabsHtml+
    // 蜡烛图
    `<div class="card"><div class="card-title">📈 蜡烛图（含 XABCD / PRZ / S&R 叠加）</div>
       <div class="card-body" id="chart-wrap">${{svgHtml}}</div></div>`+
    // 多周期 S/R
    `<div class="card"><div class="card-title">📐 多周期 S/R（布林带压力/支撑）</div>
       <div class="card-body">${{renderSrTable(d.sr||[])}}</div></div>`+
    // Setup 明细
    `<div class="card"><div class="card-title">⚡ Setup 明细</div>
       <div class="card-body">${{renderSetupDetail(d.setups||[])}}</div></div>`+
    // 历史
    `<div class="card"><div class="card-title">🕐 历史形态</div>
       <div class="card-body">${{renderHistory(d.history||[])}}</div></div>`+
    // 傻瓜解释
    `<details class="explainer">
       <summary>📖 名词傻瓜解释（点击展开）</summary>
       <div class="explainer-body">
         <dl>
           <dt>看多 / 看空</dt><dd>看多=预期涨，参考做多方向；看空=预期跌，参考做空方向。</dd>
           <dt>蜡烛图（OHLC）</dt><dd>每根蜡烛显示一个周期内的开盘/收盘/最高/最低价。绿柱=上涨；红柱=下跌。</dd>
           <dt>XABCD 形态连线</dt><dd>谐波形态的五个关键价格点，用橙色线连接，D 点是潜在反转位。</dd>
           <dt>PRZ（潜在反转区）</dt><dd>蓝色半透明区带，是形态预测的价格反转区间。</dd>
           <dt>进场区（蓝色虚线）</dt><dd>建议参考入场的价格区间（PRZ 范围内）。需等订单流/成交量确认。</dd>
           <dt>止损（红/绿虚线）</dt><dd>价格突破此位形态失效，应立即止损。</dd>
           <dt>目标（黄/紫虚线）</dt><dd>参考止盈位：target1=保守，target2=扩展目标。</dd>
           <dt>S/R 线（布林带上下轨）</dt><dd>红色虚线=压力位（上轨）；绿色虚线=支撑位（下轨）。</dd>
           <dt>斐波那契 / fib_note</dt><dd>谐波形态基于斐波那契比率（0.618/0.786/0.886等）定义 PRZ。</dd>
           <dt>订单流（前瞻领先信号）</dt><dd>PRZ 附近挂单/成交量异常确认。注意挂单墙可能 spoof（虚假）。</dd>
           <dt>KNN（仅辅助，≈随机基线）</dt><dd>历史相似形态参考，诚实标注：命中率接近随机，不可单独依赖。</dd>
         </dl>
         <div class="honest-note">⚠️ <strong>诚实声明：确认层非投资建议。</strong>
           谐波 PRZ + 订单流提高概率，不保证盈利；KNN ≈ 随机；挂单墙可能 spoof；
           止损必须执行。前瞻预测为参考，不是入场信号。</div>
       </div>
     </details>`+
    `<div class="disclaimer">⚠️ 确认层非投资建议：PRZ 前瞻 × 订单流确认；KNN ≈ 随机基线；墙可能 spoof。</div>`;

  document.getElementById('right').innerHTML=html;
}}

// ---- 初始渲染左列表 ----
renderList(_listData);
document.getElementById('meta').textContent='左列表 5s 刷新中';

// ---- 5s 轮询左列表 ----
async function refreshList(){{
  try{{
    const r=await fetch('/api/harmonic/list');
    if(r.ok){{
      _listData=await r.json();
      renderList(_listData);
      document.getElementById('meta').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{{hour12:false}});
    }}
  }}catch(e){{console.warn('harmonic list refresh err',e)}}
}}
setInterval(refreshList,5000);
</script>
</body>
</html>"""


def render_harmonic_detail_html(list_state: list[dict]) -> str:
    """将 build_harmonic_list 的结果渲染成谐波主-详情自包含 HTML 页。

    list_state 注入为 JS 数组（首屏左面板），右面板详情按需 fetch。
    双括号转义模式与 render_html / render_harmonic_html 完全一致。
    """
    state_json = json.dumps(list_state, ensure_ascii=False, default=str)
    html = _HARMONIC_DETAIL_TEMPLATE.replace("{{", "{").replace("}}", "}")
    return html.replace("__INITIAL_STATE__", state_json)
```

- [ ] **Step 4: Run tests to verify GREEN**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py -k "render_harmonic_detail" -v 2>&1 | tail -30
```

Expected: all `test_render_harmonic_detail_html_*` pass.

---

### Task 4: Routes + serve wiring

**Files:**
- Modify: `src/smc_tracker/dashboard.py` — add three new route handlers inside `serve()`, register them on `app.router`

**Interfaces:**
- New routes added inside `serve()`:
  - `GET /api/harmonic/list` → JSON from `build_harmonic_list(store)`
  - `GET /api/harmonic/coin/{coin}` → JSON from `build_coin_detail(store, coin, tf=request.rel_url.query.get("tf"))`
  - `GET /harmonic2` → HTML from `render_harmonic_detail_html(build_harmonic_list(store))`
- Old routes `/harmonic`, `/api/harmonic` are **kept unchanged**

- [ ] **Step 1: Write route registration tests**

Append to `tests/test_dashboard.py`:

```python
# ---- 路由注册（smoke: serve 中路由已声明，不起真实服务）----

def test_serve_registers_harmonic_list_route():
    """serve 函数源码中应含 /api/harmonic/list 路由声明。"""
    import inspect
    from smc_tracker import dashboard as _dash
    src = inspect.getsource(_dash.serve)
    assert "/api/harmonic/list" in src, "serve() 应含 /api/harmonic/list 路由"


def test_serve_registers_harmonic_coin_route():
    """serve 函数源码中应含 /api/harmonic/coin 路由声明。"""
    import inspect
    from smc_tracker import dashboard as _dash
    src = inspect.getsource(_dash.serve)
    assert "/api/harmonic/coin" in src, "serve() 应含 /api/harmonic/coin 路由"


def test_serve_registers_harmonic2_route():
    """serve 函数源码中应含 /harmonic2 路由声明。"""
    import inspect
    from smc_tracker import dashboard as _dash
    src = inspect.getsource(_dash.serve)
    assert "/harmonic2" in src, "serve() 应含 /harmonic2 路由"


def test_serve_still_has_old_harmonic_route():
    """旧 /harmonic 路由在 serve() 中必须保留（不破坏现有用户入口）。"""
    import inspect
    from smc_tracker import dashboard as _dash
    src = inspect.getsource(_dash.serve)
    assert '"/harmonic"' in src or "'/harmonic'" in src, "旧 /harmonic 路由应保留"
```

- [ ] **Step 2: Run to verify RED (or PASS if routes already added)**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py -k "serve_registers" -v 2>&1 | tail -15
```

- [ ] **Step 3: Add route handlers to `serve()` in dashboard.py**

Inside the `serve` function, after `handle_api_harmonic` (around line 995), add three new handlers and register them:

```python
    async def handle_harmonic_list(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /api/harmonic/list — 谐波币列表 JSON（左面板，5s 轮询）。"""
        lst = build_harmonic_list(store)
        return aiohttp.web.json_response(lst, dumps=lambda o: json.dumps(o, default=str))

    async def handle_harmonic_coin(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /api/harmonic/coin/{coin}?tf=<tf> — 指定币详情 JSON（右面板按需拉取）。"""
        coin = request.match_info.get("coin", "")
        tf = request.rel_url.query.get("tf") or None
        detail = build_coin_detail(store, coin, tf)
        return aiohttp.web.json_response(detail, dumps=lambda o: json.dumps(o, default=str))

    async def handle_harmonic2(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /harmonic2 — 谐波主-详情 SPA HTML。"""
        lst = build_harmonic_list(store)
        html = render_harmonic_detail_html(lst)
        return aiohttp.web.Response(text=html, content_type="text/html", charset="utf-8")
```

And in the `app.router.add_get` block:

```python
    app.router.add_get("/api/harmonic/list", handle_harmonic_list)
    app.router.add_get("/api/harmonic/coin/{coin}", handle_harmonic_coin)
    app.router.add_get("/harmonic2", handle_harmonic2)
```

- [ ] **Step 4: Run route tests to verify GREEN**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py -k "serve_registers" -v 2>&1 | tail -10
```

---

### Task 5: Full verification

**Files:** None modified — this is a verification task only.

- [ ] **Step 1: py_compile check**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m py_compile src/smc_tracker/dashboard.py && echo "py_compile OK"
```

Expected: `py_compile OK` with no errors.

- [ ] **Step 2: Dashboard test suite**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_dashboard.py -q 2>&1 | tail -10
```

Expected: all tests pass, 0 failed.

- [ ] **Step 3: Full test suite**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest -q 2>&1 | tail -10
```

Expected: ≥1208 passed (1208 original + new tests), 0 failed.

- [ ] **Step 4: Verify render fragment shows SVG elements**

```bash
cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -c "
from smc_tracker.dashboard import render_harmonic_detail_html
html = render_harmonic_detail_html([{'coin':'BTC','asset_class':'crypto','best_conf':0.82,'direction':'long','n_setups':1,'has_completed':True}])
for tag in ['<svg','<rect','<line','<polyline','<text']:
    print(tag, 'FOUND' if tag in html else 'MISSING')
print('{{' in html and 'LEAKED BRACES' or 'braces OK')
print('/api/harmonic/list', 'FOUND' if '/api/harmonic/list' in html else 'MISSING')
print('setInterval', 'FOUND' if 'setInterval' in html else 'MISSING')
" 2>&1
```

Expected output: all tags `FOUND`, `braces OK`.

---

## Self-Review Against Spec

**Spec §E (Backend API) coverage:**
- `build_harmonic_list`: coin/asset_class/best_conf/direction/n_setups/has_completed — covered in Task 1
- `build_coin_detail`: coin/asset_class/tf/tfs_available/candles/setups/sr/history — covered in Task 2
- Routes `/api/harmonic/list`, `/api/harmonic/coin/<coin>`, `/harmonic2` — covered in Task 4
- Defensive queries (table missing/empty → `[]`) — covered in both implementations

**Spec §F (Frontend) coverage:**
- Left panel: coin list, asset badge, confidence, direction color — template Task 3
- Filter buttons: 全部/加密/TradFi/有完整形态 — template Task 3
- 5s poll `/api/harmonic/list` — template Task 3
- Right panel SVG: OHLC bars + wicks, XABCD polyline + points + labels, PRZ band, entry/stop/target lines, S/R lines — `renderSvgCandles` in template Task 3
- Timeframe tab switching — template Task 3
- Multi-tf S/R table — `renderSrTable` in template Task 3
- Setup detail — `renderSetupDetail` in template Task 3
- History list — `renderHistory` in template Task 3
- Collapsible explainer + honest disclaimer — template Task 3
- None → `—` — `fmtN` in template Task 3
- No CDN — verified in tests Task 3

**Constraints verified:**
- Old routes preserved — Task 4 checks `"/harmonic"` stays
- Double-brace escaping — test `test_render_harmonic_detail_html_no_residual_double_braces`
- Initial state injected as JSON array — test `test_render_harmonic_detail_html_initial_state_injected`
- py_compile clean — Task 5 step 1

**Placeholder scan:** None — all test assertions use literal strings, all code shows concrete implementations.

**Type consistency:** `build_harmonic_list` returns `list[dict]`, consumed by `render_harmonic_detail_html(list_state: list[dict])`. `build_coin_detail` returns `dict` with `candles: list[list]` where each inner list is `[ts, o, h, l, c, v]` — matches `renderSvgCandles` which does `c.map(Number)` on each `c`. `_HARMONIC_KEYS` used consistently in both `build_coin_detail` (setups) and history dict construction.
