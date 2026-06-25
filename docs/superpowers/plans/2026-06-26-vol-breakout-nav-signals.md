# 全部开发计划：波动突破告警 + dashboard 导航 + /signals 迁出（Opus 规划 / Sonnet 执行）

> 状态：Opus 规划完成 → Sonnet workflow 执行 → Opus 复核。本地 main 开发，**不推送**。
> 规范：CLAUDE.md §三-5（Opus 规划/审计，Sonnet 执行）+ §三(零孤儿/数据质量/极简/≤800行) + §四(TDD+真实数据)。
> 基线：全量 `./.venv/bin/python -m pytest -q` 当前 **2333 passed, 2 skipped**，改后必须 ≥此且全绿。
> 统一周期：`config.CANONICAL_TIMEFRAMES`（15m/1H/4H/6H/12H/1D/1W），禁硬编码周期列表。

## 文件归属（并行零冲突铁律）

| 任务 | 新建文件（独占） | 编辑共享文件 | 阶段 |
|---|---|---|---|
| T1 突破跟踪器 | `src/smc_tracker/monitor/volatility_regime_tracker.py` + `tests/test_volatility_regime_tracker.py` | `monitor/__init__.py`(仅 T1 改) | Build(并行) |
| T2 导航页 | `src/smc_tracker/dashboard_nav.py` + `tests/test_dashboard_nav.py` | 无 | Build(并行) |
| T3 /signals 迁出模块 | `src/smc_tracker/dashboard_signals.py` + `tests/test_dashboard_signals_module.py` | 无（只新建，不删原） | Build(并行) |
| T4 集成 | 无 | `app.py` + `dashboard.py` + `tests/test_dashboard_signals.py`(改1行import) | Integrate(串行,在 Build 后) |

> Build 阶段三任务只写各自独占新文件（T1 额外只改 `__init__.py`），互不冲突可真并行。
> T4 串行在 Build 之后，独占编辑 `app.py`/`dashboard.py`，清理 T3 留在 dashboard.py 的原函数 + 接线。

---

## T1：波动 regime 突破跟踪器（压缩→扩张 跨刷新告警）

**新建** `src/smc_tracker/monitor/volatility_regime_tracker.py`：

```python
"""波动 regime 突破跟踪器：跨刷新检测 (coin,tf) 压缩→扩张 转换（蓄势→放量=突破前瞻信号）。

设计（CLAUDE.md §二 领先信号 + 极简）：压缩(蓄势)切到扩张(放量)常先于价格突破；带 per-(coin,tf)
冷却防刷屏。纯内存状态，无 DB；update 接受 VolatilityMonitor.rank 输出。
"""
from __future__ import annotations


class VolatilityRegimeTracker:
    """记忆每 (coin,tf) 上一次 regime，检测 压缩/常态 → 扩张 的新突破事件。"""

    __slots__ = ("_prev", "_last_emit_ms", "cooldown_ms")

    def __init__(self, cooldown_ms: int = 1_800_000) -> None:
        self._prev: dict[tuple[str, str], str] = {}        # (coin,tf) -> 上次 regime
        self._last_emit_ms: dict[tuple[str, str], int] = {}  # (coin,tf) -> 上次告警 ts
        self.cooldown_ms = cooldown_ms

    def update(self, rows: list[dict], now_ms: int) -> list[dict]:
        """rows=rank() 输出。返回新突破事件 [{coin,tf,vol_ratio,velocity}]（仅 压缩/常态→扩张 且过冷却）。"""
        events: list[dict] = []
        for r in rows:
            coin = r.get("coin", "")
            for tf, m in r.get("by_tf", {}).items():
                key = (coin, tf)
                cur = m.get("regime", "常态")
                prev = self._prev.get(key)
                # 突破：上次非扩张（压缩/常态/首见）→ 本次扩张
                if cur == "扩张" and prev != "扩张":
                    last = self._last_emit_ms.get(key, 0)
                    if now_ms - last >= self.cooldown_ms:
                        events.append({"coin": coin, "tf": tf,
                                       "vol_ratio": m.get("vol_ratio", 0.0),
                                       "velocity": m.get("velocity", 0.0)})
                        self._last_emit_ms[key] = now_ms
                self._prev[key] = cur
        return events

    def render(self, events: list[dict], now_ms: int) -> str:
        """渲染突破告警卡片。空返回 ""。"""
        if not events:
            return ""
        from ..util import fmt_ts  # noqa: PLC0415
        lines = [f"🔶 波动突破告警 [{fmt_ts(now_ms)}] 蓄势→放量（领先突破信号）"]
        for e in events:
            lines.append(f"  {e['coin']}/{e['tf']} 放量 速度{e['velocity']:+.2f}% (σ比 {e['vol_ratio']:.2f})")
        return "\n".join(lines)
```

**编辑** `monitor/__init__.py`：`from .volatility_regime_tracker import VolatilityRegimeTracker` + 加入 `__all__`。

**测试** `tests/test_volatility_regime_tracker.py`（sys.path 头同其它测试）：
- `test_no_event_first_seen_squeeze`：首次见 压缩 → 无事件（prev 记录）。
- `test_squeeze_to_expansion_emits`：先喂 压缩 再喂 扩张 → 1 事件。
- `test_expansion_to_expansion_no_repeat`：连续 扩张 → 仅首次（prev=扩张 后不再报）。
- `test_cooldown_suppresses`：压缩→扩张 报1次；再 压缩→扩张 但 now 未过 cooldown → 0；过 cooldown → 1。
- `test_render_nonempty`：有事件 render 含 coin。
构造 rows：`[{"coin":"BTC","by_tf":{"15m":{"regime":"压缩","vol_ratio":0.3,"velocity":0.1}}}]` 等。

- [ ] 写测试 → 跑 RED → 实现 → 跑 GREEN（`pytest tests/test_volatility_regime_tracker.py -q`）。
- [ ] 不 commit（T4 集成后统一由 Opus 复核提交）。

---

## T2：Dashboard 导航页（可发现性）

**新建** `src/smc_tracker/dashboard_nav.py`（复刻 dashboard_vol.py 范式，自包含 HTML 无 CDN）：

```python
"""Dashboard 导航页（扁平模块）：列出全部面板入口，解决可发现性。register(app) 挂 GET /nav。"""
from __future__ import annotations

import aiohttp.web

_LINKS = [
    ("/", "主页 总览"),
    ("/volatility", "🌀 实时波动追踪（逐周期 速度/PD/regime + 动向摘要）"),
    ("/harmonic", "谐波形态"),
    ("/signals", "全信号总览"),
    ("/monitored", "监控币种清单（增删）"),
    ("/hl2", "HL 抓庄系统"),
]


def render_nav_page() -> str:
    items = "".join(
        f'<li><a href="{href}">{label}</a></li>' for href, label in _LINKS
    )
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>导航</title>'
        "<style>body{font-family:-apple-system,Segoe UI,sans-serif;background:#0d1117;color:#c9d1d9;"
        "margin:0;padding:32px}h1{font-size:18px}li{margin:10px 0;font-size:15px}"
        "a{color:#58a6ff;text-decoration:none}a:hover{text-decoration:underline}</style></head><body>"
        "<h1>SMC 抓庄系统 · 面板导航</h1><ul>" + items + "</ul></body></html>"
    )


def register(app: aiohttp.web.Application) -> None:
    async def handle_nav(_req: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text=render_nav_page(), content_type="text/html")
    app.router.add_get("/nav", handle_nav)
```

**测试** `tests/test_dashboard_nav.py`：
- `test_nav_lists_all_panels`：render_nav_page() 含 `/volatility`、`/monitored`、`/signals`、`/harmonic`。
- `test_nav_self_contained`：无 `http://`/`https://`（无外链）。

- [ ] 写测试 → RED → 实现 → GREEN（`pytest tests/test_dashboard_nav.py -q`）。不 commit。

---

## T3：/signals 面板迁出 → 扁平模块（只新建，不删原）

**新建** `src/smc_tracker/dashboard_signals.py`：把 dashboard.py 的 `build_all_signals_state`(约 3856 行起)
与 `render_all_signals_html`(约 4089 行起) **原样复制**进来（逐字，含 docstring），并加 `register`：

```python
"""全信号总览 dashboard 面板（扁平模块，从 dashboard.py 迁出）。register(app, store) 挂 /signals + /api/signals。"""
from __future__ import annotations

import json
import time
from typing import Any

import aiohttp.web

# ↓↓↓ 从 dashboard.py 原样复制（逐字，勿改逻辑）：
def build_all_signals_state(store: Any, now_ms: int, hours: float = 1.0) -> dict:
    ...  # 复制 dashboard.py 同名函数全文（含其 import 的 collect_all_signals 等，按原文件的 import 来源照搬）

def render_all_signals_html(state: dict) -> str:
    ...  # 复制 dashboard.py 同名函数全文
# ↑↑↑ 复制结束

def register(app: aiohttp.web.Application, store: Any) -> None:
    async def handle_signals(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            hours = float(request.rel_url.query.get("hours") or 1.0)
        except (ValueError, TypeError):
            hours = 1.0
        now_ms = int(time.time() * 1000)
        state = build_all_signals_state(store, now_ms, hours=hours)
        return aiohttp.web.Response(text=render_all_signals_html(state), content_type="text/html")

    async def handle_api_signals(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            hours = float(request.rel_url.query.get("hours") or 1.0)
        except (ValueError, TypeError):
            hours = 1.0
        now_ms = int(time.time() * 1000)
        state = build_all_signals_state(store, now_ms, hours=hours)
        return aiohttp.web.json_response(state, dumps=lambda o: json.dumps(o, default=str))

    app.router.add_get("/signals", handle_signals)
    app.router.add_get("/api/signals", handle_api_signals)
```

> 复制 `build_all_signals_state`/`render_all_signals_html` 时，**照搬它们在 dashboard.py 顶部/函数内的 import 来源**
> （如 `from .notify import collect_all_signals` 或 `from .signals import ...`——以 dashboard.py 实际为准）。
> 此阶段 dashboard.py 仍保留原函数（T4 再删），故会暂时重复定义，属预期。

**测试** `tests/test_dashboard_signals_module.py`：从 `smc_tracker.dashboard_signals` import 两函数，
断言 `build_all_signals_state(empty_store, ts)` 返回 dict 含 `signals_list`/`meta`、`render_all_signals_html(state)`
返回含 doctype 的非空 str 且无 CDN。（构造空 Store：`Store(tmp/'t.db')`。）

- [ ] 写测试 → RED → 复制实现 → GREEN（`pytest tests/test_dashboard_signals_module.py -q`）。不 commit。

---

## T4：集成（串行，Build 全绿后）

**编辑** `src/smc_tracker/app.py`（波动突破接入周期推送）：
在 `_periodic_volatility_board`（约 1700 行）循环外建一个 `VolatilityRegimeTracker` 实例，循环内每轮
`mon.rank(now)` 后调 `tracker.update(rows, now)`，有突破事件则 `self._push_harmonic(tracker.render(events, now))`：

```python
        from .monitor.volatility_regime_tracker import VolatilityRegimeTracker  # noqa: PLC0415
        tracker = VolatilityRegimeTracker()
        ...
        while not self._stopping:
            try:
                coins = self.store.get_monitored_coins()
                if coins:
                    mon = VolatilityMonitor(coins, list(mc.timeframes), self.store)
                    now = int(time.time() * 1000)
                    rows = mon.rank(now)
                    card = mon.render(rows, now)
                    if card:
                        print(card); self._push_harmonic(card)
                    events = tracker.update(rows, now)          # 新增：突破检测
                    bo = tracker.render(events, now)
                    if bo:
                        print(bo); self._push_harmonic(bo)      # 新增：突破告警推送
            except Exception as exc:  # noqa: BLE001
                log.warning("波动追踪板推送失败: %s", exc)
            await asyncio.sleep(mc.vol_board_sec)
```
（按 app.py 实际 `_periodic_volatility_board` 现有结构最小改动接入，保持 opt-in 守卫不变。）

**编辑** `src/smc_tracker/dashboard.py`：
1. 删除模块级 `build_all_signals_state` 与 `render_all_signals_html` 全文（已迁 T3）。
2. 删除 `serve()` 内 `handle_signals`/`handle_api_signals` 两 handler + `add_get("/signals")`/`add_get("/api/signals")` 两路由。
3. 在面板注册区（`_register_monitored(app, store)` 附近）加：
   ```python
   from .dashboard_signals import register as _register_signals  # noqa: PLC0415
   from .dashboard_nav import register as _register_nav          # noqa: PLC0415
   _register_signals(app, store)   # /signals + /api/signals
   _register_nav(app)              # /nav 导航
   ```
4. 若 dashboard.py 其它处仍调用 `build_all_signals_state`/`render_all_signals_html`，改为
   `from .dashboard_signals import build_all_signals_state, render_all_signals_html`（保持可达）。

**编辑** `tests/test_dashboard_signals.py`：第 26-27 行 import 改为
`from smc_tracker.dashboard_signals import build_all_signals_state, render_all_signals_html`（其余 40 处断言不动）。

- [ ] 改完跑全量 `pytest -q`（必须 ≥2333 + 新增，全绿）。不 commit（交 Opus 复核）。

---

## Opus 复核（终）

- 全量 `pytest -q` 亲自复跑全绿；`py_compile` 关键文件。
- 零孤儿 grep：`VolatilityRegimeTracker`/`render_nav_page`/`dashboard_signals`/`dashboard_nav` 均被消费。
- dashboard.py 行数应**下降**（删 /signals 两大函数）；新模块均 ≤800。
- 端到端实跑 dashboard：curl `/nav`、`/signals` 仍 200；`vol --db`（突破 tracker 不影响 CLI）。
- 通过后由 Opus 统一 commit（本地，不 push）+ PLAN 迭代日志。
