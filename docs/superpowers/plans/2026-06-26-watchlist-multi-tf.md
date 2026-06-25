# 监控清单驱动多周期采集（watchlist-multi-tf）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增一份 DB 驱动的「监控币种清单」，开关打开后只为清单内币种采集 7 周期 K 线（15m/1H/4H/6H/12H/1D/1W），CLI/dashboard 增删支持运行中热载入，关闭则完全保持现状 all_perp 行为。

**Architecture:** 新表 `monitored_coins` 作唯一真相源；`config.monitored_coins.enabled` 主开关在 `app.py` 选币处分支（替换 `resolve_universe`/all_perp）；纯函数 `resolve_monitored_universe` + `reconcile_universe` 承载可测逻辑；周期任务每轮对账 DB 清单实现热载入；CLI `watch` 子命令与 dashboard `/api/monitored` 写 DB（SQLite WAL 跨进程可见）。

**Tech Stack:** Python 3 / asyncio / sqlite3(WAL) / aiohttp(dashboard) / argparse(CLI) / pytest（合成数据单测）。venv：`./.venv/bin/python`。

## Global Constraints

- 测试基线：全量 `./.venv/bin/python -m pytest -q` 当前 **357 passed**，改动后必须全绿。
- 周期集（用户决策）：**`["15m", "1H", "4H", "6H", "12H", "1D", "1W"]`**（6H 替代 Bitget 不支持的 8h）。
- 命名：配置段/dataclass/DB 表/方法用 `monitored_coins`（**禁用 `watchlist`**，已与 `Config.watchlist` 地址列表冲突）。CLI 子命令用 `watch`。
- 零回归：`monitored_coins.enabled=False`（默认）时，新路径完全旁路，现有行为一字不变。
- 风格：中文注释 + 英文标识符 + 类型注解；slots dataclass；DB 方法空安全/幂等，异常向上抛由调用方 `log.warning`。
- DB 路径默认 `data/smc.db`（`cli.py::_DEFAULT_DB`）。所有 CLI handler 用 `Store(Path(args.db))`。
- 每个测试文件顶部需 `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))` 或依赖已装包（仓库测试两种都有，沿用 `from smc_tracker...` 直接 import 即可，pytest 在仓库根跑）。

---

### Task 1: DB 层 — `monitored_coins` 表 + CRUD + 迁移

**Files:**
- Modify: `src/smc_tracker/storage/db.py`（SCHEMA 加表；新增 4 方法；`__init__` 加一次性迁移）
- Test: `tests/test_monitored_coins.py`（新建）

**Interfaces:**
- Produces:
  - `Store.add_monitored_coins(items: Iterable[tuple]) -> None` — 每行 `(coin: str, symbol: str, added_ts: int, note: str)`，幂等 upsert，空安全。
  - `Store.remove_monitored_coins(coins: Iterable[str]) -> int` — 删除并返回删除行数，空安全返回 0。
  - `Store.get_monitored_coins() -> dict[str, str]` — `{coin: symbol}`。
  - `Store.list_monitored_coins() -> list[tuple]` — `(coin, symbol, added_ts, note)`，按 added_ts 升序。
  - 迁移：`Store.__init__` 后，若 `monitored_coins` 空且 `harmonic_collected` 非空，拷入（note=`'migrated:harmonic_collected'`）。

- [ ] **Step 1: 写失败测试** — `tests/test_monitored_coins.py`

```python
"""monitored_coins 表（监控币种清单）读写 + 迁移单测。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from smc_tracker.storage import Store


def _store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


def test_add_and_get():
    s = _store()
    s.add_monitored_coins([("BTC", "BTCUSDT", 1000, "core"),
                           ("ETH", "ETHUSDT", 1000, "")])
    assert s.get_monitored_coins() == {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


def test_add_idempotent_upsert():
    s = _store()
    s.add_monitored_coins([("BTC", "BTCUSDT", 1000, "a")])
    s.add_monitored_coins([("BTC", "BTCUSDT", 2000, "b")])  # 同 coin 覆盖
    assert s.get_monitored_coins() == {"BTC": "BTCUSDT"}
    rows = s.list_monitored_coins()
    assert len(rows) == 1
    assert rows[0][3] == "b"  # note 被更新


def test_remove_returns_count():
    s = _store()
    s.add_monitored_coins([("BTC", "BTCUSDT", 1, ""), ("ETH", "ETHUSDT", 1, "")])
    assert s.remove_monitored_coins(["BTC", "NOPE"]) == 1  # 只 BTC 命中
    assert s.get_monitored_coins() == {"ETH": "ETHUSDT"}


def test_list_sorted_by_added_ts():
    s = _store()
    s.add_monitored_coins([("ETH", "ETHUSDT", 200, ""), ("BTC", "BTCUSDT", 100, "")])
    coins = [r[0] for r in s.list_monitored_coins()]
    assert coins == ["BTC", "ETH"]  # added_ts 升序


def test_empty_ops_safe():
    s = _store()
    s.add_monitored_coins([])
    assert s.remove_monitored_coins([]) == 0
    assert s.get_monitored_coins() == {}
    assert s.list_monitored_coins() == []


def test_migration_from_harmonic_collected():
    """旧库有 harmonic_collected、monitored_coins 空 → 迁移拷入。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "t.db"
    s = Store(p)
    s.add_harmonic_collected([("DOGE", "DOGEUSDT", 5)])
    s.close()
    # 重开库触发迁移（monitored_coins 仍空）
    s2 = Store(p)
    assert s2.get_monitored_coins() == {"DOGE": "DOGEUSDT"}
    rows = s2.list_monitored_coins()
    assert rows[0][3] == "migrated:harmonic_collected"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_monitored_coins.py -q`
Expected: FAIL（`AttributeError: 'Store' object has no attribute 'add_monitored_coins'`）

- [ ] **Step 3: 加表到 SCHEMA** — 在 `db.py` 的 SCHEMA 字符串里 `harmonic_collected` 表定义之后插入：

```sql
-- 监控币种清单（watchlist-multi-tf）：主开关 monitored_coins.enabled 打开时驱动采集/谐波/BB 选币
CREATE TABLE IF NOT EXISTS monitored_coins (
    coin     TEXT    NOT NULL PRIMARY KEY,
    symbol   TEXT    NOT NULL,
    added_ts INTEGER NOT NULL,
    note     TEXT    NOT NULL DEFAULT ''
);
```

- [ ] **Step 4: 加 4 个方法** — 在 `db.py` 中 `get_harmonic_collected` 方法之后插入：

```python
    # ---- 监控币种清单 monitored_coins（主开关驱动采集/选币；CLI/dashboard 增删，运行时热载入）----
    def add_monitored_coins(self, items: Iterable[tuple]) -> None:
        """加入监控币（幂等 upsert）。items: [(coin, symbol, added_ts, note), ...]。空安全。"""
        rows = list(items)
        if not rows:
            return
        try:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT INTO monitored_coins(coin,symbol,added_ts,note) VALUES(?,?,?,?) "
                "ON CONFLICT(coin) DO UPDATE SET symbol=excluded.symbol, note=excluded.note",
                rows,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def remove_monitored_coins(self, coins: Iterable[str]) -> int:
        """从清单删除指定 coin，返回删除行数。空安全返回 0。"""
        cs = [c for c in coins if c]
        if not cs:
            return 0
        try:
            self.conn.execute("BEGIN")
            cur = self.conn.executemany(
                "DELETE FROM monitored_coins WHERE coin=?", [(c,) for c in cs]
            )
            n = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            self.conn.execute("COMMIT")
            return n
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def get_monitored_coins(self) -> dict[str, str]:
        """返回监控清单 {coin: symbol}。"""
        return {
            coin: sym
            for coin, sym in self.conn.execute(
                "SELECT coin, symbol FROM monitored_coins"
            ).fetchall()
        }

    def list_monitored_coins(self) -> list[tuple]:
        """返回 (coin, symbol, added_ts, note) 行，按 added_ts 升序（CLI/dashboard 展示用）。"""
        return list(self.conn.execute(
            "SELECT coin, symbol, added_ts, note FROM monitored_coins ORDER BY added_ts ASC, coin ASC"
        ).fetchall())
```

> 注：`executemany` 的 `rowcount` 在 SQLite 删除多行时返回累计影响行数（已实证 sqlite3 行为）；保守用 `>= 0` 守卫。

- [ ] **Step 5: 加迁移** — 在 `db.py::__init__` 末尾（所有 `_ensure_columns` 之后）插入：

```python
        # 一次性迁移：旧 harmonic_collected → monitored_coins（仅当后者空且前者非空）
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM monitored_coins").fetchone()
            if row and row[0] == 0:
                legacy = self.conn.execute(
                    "SELECT coin, symbol, added_ts FROM harmonic_collected"
                ).fetchall()
                if legacy:
                    self.add_monitored_coins(
                        [(c, s, ts, "migrated:harmonic_collected") for c, s, ts in legacy]
                    )
        except Exception:  # noqa: BLE001 — 迁移失败不阻塞启动
            pass
```

- [ ] **Step 6: 跑测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_monitored_coins.py -q`
Expected: PASS（6 passed）

- [ ] **Step 7: 提交**

```bash
git add src/smc_tracker/storage/db.py tests/test_monitored_coins.py
git commit -m "feat(db): monitored_coins 表 + CRUD + harmonic_collected 迁移"
```

---

### Task 2: 配置 — `MonitoredCoinsCfg` + `Config` 接线 + `resolve_monitored_universe`

**Files:**
- Modify: `src/smc_tracker/config.py`（新增 dataclass、`Config` 字段、`load` 透传+校验、纯函数）
- Test: `tests/test_monitored_coins_config.py`（新建）

**Interfaces:**
- Consumes: `bitget.rest.GRANULARITY_MS`（周期校验）。
- Produces:
  - `MonitoredCoinsCfg(enabled: bool=False, timeframes: list[str]=[...7tf], collect_interval_sec: float=300.0)`
  - `Config.monitored_coins: MonitoredCoinsCfg`
  - `resolve_monitored_universe(monitored: dict[str,str], base_map: dict[str,str], tickers: dict[str,dict]) -> dict[str,str]` — 按成交额降序的 `{coin: symbol}`，symbol 缺失回退 base_map 反查或 `coin+'USDT'`。

- [ ] **Step 1: 写失败测试** — `tests/test_monitored_coins_config.py`

```python
"""MonitoredCoinsCfg + resolve_monitored_universe 单测（合成数据，纯函数）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import (
    MonitoredCoinsCfg,
    resolve_monitored_universe,
    Config,
)


def test_cfg_defaults():
    c = MonitoredCoinsCfg()
    assert c.enabled is False
    assert c.timeframes == ["15m", "1H", "4H", "6H", "12H", "1D", "1W"]
    assert c.collect_interval_sec == 300.0


def test_resolve_orders_by_volume():
    """清单 {BTC, ETH}；ETH 成交额更高 → 排前。symbol 用清单存的。"""
    monitored = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
    base_map = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
    tickers = {"BTCUSDT": {"quoteVolume": "500"}, "ETHUSDT": {"quoteVolume": "900"}}
    out = resolve_monitored_universe(monitored, base_map, tickers)
    assert list(out.keys()) == ["ETH", "BTC"]
    assert out["BTC"] == "BTCUSDT"


def test_resolve_symbol_fallback():
    """清单 symbol 缺失 → 回退 base_map 反查；再缺 → coin+'USDT'。"""
    monitored = {"SOL": "", "XYZ": ""}
    base_map = {"SOLUSDT": "SOL"}  # XYZ 不在 base_map
    tickers = {}
    out = resolve_monitored_universe(monitored, base_map, tickers)
    assert out["SOL"] == "SOLUSDT"   # base_map 反查
    assert out["XYZ"] == "XYZUSDT"   # 兜底拼接


def test_resolve_empty():
    assert resolve_monitored_universe({}, {}, {}) == {}


def test_config_load_defaults(tmp_path: Path):
    """config.yaml 无 monitored_coins 段 → 默认 enabled=False。"""
    p = tmp_path / "c.yaml"
    p.write_text("markets: [BTC]\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.monitored_coins.enabled is False


def test_config_load_filters_invalid_tf(tmp_path: Path):
    """非法周期 8h 被剔除（Bitget 不支持）；合法保留。"""
    p = tmp_path / "c.yaml"
    p.write_text(
        "monitored_coins:\n"
        "  enabled: true\n"
        "  timeframes: ['15m', '8h', '1D']\n",
        encoding="utf-8",
    )
    cfg = Config.load(p)
    assert cfg.monitored_coins.enabled is True
    assert "8h" not in cfg.monitored_coins.timeframes
    assert cfg.monitored_coins.timeframes == ["15m", "1D"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_monitored_coins_config.py -q`
Expected: FAIL（`ImportError: cannot import name 'MonitoredCoinsCfg'`）

- [ ] **Step 3: 加 dataclass + 纯函数** — `config.py` 中 `UniverseCfg`/`resolve_universe` 之后插入：

```python
@dataclass(slots=True)
class MonitoredCoinsCfg:
    """监控币种清单配置（watchlist-multi-tf）。

    enabled=True：采集器/谐波/BB 选币改为 DB 清单驱动（替换 all_perp/top_n）。
    enabled=False（默认）：完全旁路，现有行为不变（零回归）。
    timeframes：清单币的多周期采集集（默认 7 周期；6H 替代 Bitget 不支持的 8h）。
    """
    enabled: bool = False
    timeframes: list[str] = field(
        default_factory=lambda: ["15m", "1H", "4H", "6H", "12H", "1D", "1W"])
    collect_interval_sec: float = 300.0


def resolve_monitored_universe(
    monitored: dict[str, str],
    base_map: dict[str, str],
    tickers: dict[str, dict],
) -> dict[str, str]:
    """把 DB 监控清单解析为 {coin: symbol}，按 24h 成交额降序。

    纯函数（确定性、无副作用、可测）：
      - symbol 优先用清单存的；为空时回退 base_map 反查（{symbol:base} 反向），再缺则 coin+'USDT'。
      - 成交额取 tickers[symbol].quoteVolume（缺失=0）。
    """
    if not monitored:
        return {}
    # base_map 是 {symbol: base}，反查 {base: symbol} 供 symbol 兜底
    base_to_sym: dict[str, str] = {}
    for sym, base in base_map.items():
        if base and base not in base_to_sym:
            base_to_sym[base] = sym

    enriched: list[tuple[str, str, float]] = []
    for coin, sym in monitored.items():
        symbol = sym or base_to_sym.get(coin) or f"{coin}USDT"
        vol = _safe_vol((tickers.get(symbol) or {}).get("quoteVolume"))
        enriched.append((coin, symbol, vol))
    enriched.sort(key=lambda t: t[2], reverse=True)

    out: dict[str, str] = {}
    for coin, symbol, _ in enriched:
        if coin not in out:
            out[coin] = symbol
    return out
```

- [ ] **Step 4: 加 `Config` 字段** — 在 `Config` dataclass 字段区（`universe: UniverseCfg = ...` 附近）加：

```python
    monitored_coins: MonitoredCoinsCfg = field(default_factory=MonitoredCoinsCfg)
```

- [ ] **Step 5: `Config.load` 透传 + 周期校验** — 在 `load` 中 `univ_raw` 处理之后、`return cls(` 之前加：

```python
        # MonitoredCoinsCfg：timeframes 强制 list + 用 GRANULARITY_MS 剔非法周期（数据质量守卫）
        mc_raw: dict[str, Any] = dict(raw.get("monitored_coins") or {})
        if "timeframes" in mc_raw:
            from .bitget.rest import GRANULARITY_MS  # noqa: PLC0415
            tfs = list(mc_raw["timeframes"]) if mc_raw["timeframes"] else []
            mc_raw["timeframes"] = [t for t in tfs if t in GRANULARITY_MS]
```

并在 `return cls(` 参数表末尾加：

```python
            monitored_coins=MonitoredCoinsCfg(**mc_raw),
```

- [ ] **Step 6: 跑测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_monitored_coins_config.py -q`
Expected: PASS（6 passed）

- [ ] **Step 7: 提交**

```bash
git add src/smc_tracker/config.py tests/test_monitored_coins_config.py
git commit -m "feat(config): MonitoredCoinsCfg + resolve_monitored_universe + 周期校验"
```

---

### Task 3: 对账纯函数 `reconcile_universe`

**Files:**
- Modify: `src/smc_tracker/config.py`（加纯函数）
- Test: `tests/test_reconcile_universe.py`（新建）

**Interfaces:**
- Produces: `reconcile_universe(current: dict[str,str], target: dict[str,str]) -> tuple[dict[str,str], set[str]]`
  返回 `(added, removed)`：`added` = target 有而 current 无的 `{coin:symbol}`；`removed` = current 有而 target 无的 `{coin}`。symbol 变化视为 added（更新）。

- [ ] **Step 1: 写失败测试** — `tests/test_reconcile_universe.py`

```python
"""reconcile_universe 对账纯函数单测（热载入增删逻辑核心）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import reconcile_universe


def test_add_only():
    added, removed = reconcile_universe({"BTC": "BTCUSDT"}, {"BTC": "BTCUSDT", "ETH": "ETHUSDT"})
    assert added == {"ETH": "ETHUSDT"}
    assert removed == set()


def test_remove_only():
    added, removed = reconcile_universe({"BTC": "BTCUSDT", "ETH": "ETHUSDT"}, {"BTC": "BTCUSDT"})
    assert added == {}
    assert removed == {"ETH"}


def test_add_and_remove():
    added, removed = reconcile_universe({"BTC": "BTCUSDT"}, {"ETH": "ETHUSDT"})
    assert added == {"ETH": "ETHUSDT"}
    assert removed == {"BTC"}


def test_symbol_change_is_add():
    added, removed = reconcile_universe({"BTC": "BTCUSDT"}, {"BTC": "BTCUSDT_NEW"})
    assert added == {"BTC": "BTCUSDT_NEW"}
    assert removed == set()


def test_empty_target_removes_all():
    added, removed = reconcile_universe({"BTC": "BTCUSDT", "ETH": "ETHUSDT"}, {})
    assert added == {}
    assert removed == {"BTC", "ETH"}


def test_no_change():
    added, removed = reconcile_universe({"BTC": "BTCUSDT"}, {"BTC": "BTCUSDT"})
    assert added == {} and removed == set()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_reconcile_universe.py -q`
Expected: FAIL（`ImportError: cannot import name 'reconcile_universe'`）

- [ ] **Step 3: 实现** — `config.py` 中 `resolve_monitored_universe` 之后加：

```python
def reconcile_universe(
    current: dict[str, str],
    target: dict[str, str],
) -> tuple[dict[str, str], set[str]]:
    """对账当前币集与目标币集，返回 (added, removed)。

    供运行时热载入用（增删都反映；现有 merge 只加不删，replace 模式必须支持删）：
      - added：target 中 current 缺失或 symbol 不同的 {coin: symbol}（需加入/更新）。
      - removed：current 中 target 不再包含的 {coin}（需移走）。
    纯函数，不修改入参。
    """
    added = {c: s for c, s in target.items() if current.get(c) != s}
    removed = {c for c in current if c not in target}
    return added, removed
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_reconcile_universe.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add src/smc_tracker/config.py tests/test_reconcile_universe.py
git commit -m "feat(config): reconcile_universe 对账纯函数（热载入增删）"
```

---

### Task 4: `app.py` 接线 — 选币替换 + 周期对账热载入 + 空清单守卫

**Files:**
- Modify: `src/smc_tracker/app.py`（选币分支 ~698–905；`_periodic_candle_collect` ~1441；`_periodic_harmonic_board` ~1558；`_periodic_bb_board` ~1522）
- Test: `tests/test_app_monitored_wiring.py`（新建，测可提取的纯决策，不起网络）

**Interfaces:**
- Consumes: `resolve_monitored_universe`, `reconcile_universe`（Task 2/3）；`store.get_monitored_coins()`（Task 1）；`cfg.monitored_coins`（Task 2）。
- Produces: 运行时行为（无新公开符号）。新增内部辅助 `_apply_reconcile(monitor, target)`（模块级纯函数，可测）。

- [ ] **Step 1: 写失败测试** — `tests/test_app_monitored_wiring.py`

```python
"""app 监控集热载入对账应用单测（不起网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.app import _apply_reconcile


class _FakeMon:
    def __init__(self, c2s):
        self.coin_to_symbol = dict(c2s)
        self.top_n = len(c2s)


def test_apply_reconcile_adds_and_removes():
    mon = _FakeMon({"BTC": "BTCUSDT", "ETH": "ETHUSDT"})
    changed = _apply_reconcile(mon, {"BTC": "BTCUSDT", "SOL": "SOLUSDT"})
    assert mon.coin_to_symbol == {"BTC": "BTCUSDT", "SOL": "SOLUSDT"}
    assert mon.top_n == 2
    assert changed is True


def test_apply_reconcile_noop_returns_false():
    mon = _FakeMon({"BTC": "BTCUSDT"})
    changed = _apply_reconcile(mon, {"BTC": "BTCUSDT"})
    assert changed is False
    assert mon.coin_to_symbol == {"BTC": "BTCUSDT"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_app_monitored_wiring.py -q`
Expected: FAIL（`ImportError: cannot import name '_apply_reconcile'`）

- [ ] **Step 3: 加模块级辅助** — `app.py` 顶部（import 区之后、类定义之前）加，并确保 `from .config import resolve_monitored_universe, reconcile_universe` 已导入（若文件已 `from .config import ...` 则并入）：

```python
def _apply_reconcile(monitor: Any, target: dict[str, str]) -> bool:
    """把 monitor.coin_to_symbol 对账到 target（增删都应用）；有变更返回 True。

    monitor 需有 .coin_to_symbol(dict) 属性，可选 .top_n。供 collector/谐波/BB 复用。
    """
    from .config import reconcile_universe  # noqa: PLC0415
    added, removed = reconcile_universe(monitor.coin_to_symbol, target)
    if not added and not removed:
        return False
    for c in removed:
        monitor.coin_to_symbol.pop(c, None)
    monitor.coin_to_symbol.update(added)
    if hasattr(monitor, "top_n"):
        monitor.top_n = len(monitor.coin_to_symbol)
    return True
```

> `Any` 已在 app.py 顶部 typing import 中（若无则加 `from typing import Any`）。

- [ ] **Step 4: 选币分支（替换）** — 在 `_run` 构建 `vol_c2s` 处（约 700–714），把 `resolve_universe` 调用包成分支：

找到：
```python
                    from .config import resolve_universe  # noqa: PLC0415
                    vol_c2s = resolve_universe(base_map, tickers_map, self.cfg.universe)
```
改为：
```python
                    from .config import resolve_universe, resolve_monitored_universe  # noqa: PLC0415
                    if self.cfg.monitored_coins.enabled:
                        _monitored = self.store.get_monitored_coins()
                        if not _monitored:
                            log.warning("监控清单为空(monitored_coins.enabled=true)，本轮不纳入任何币；"
                                        "用 `watch add` 或 dashboard 添加")
                        vol_c2s = resolve_monitored_universe(_monitored, base_map, tickers_map)
                    else:
                        vol_c2s = resolve_universe(base_map, tickers_map, self.cfg.universe)
```

- [ ] **Step 5: 谐波宇宙分支** — 在谐波 `harm_umode` 选币处（约 740–761），令 enabled 时直接用 vol_c2s 且不并 harmonic_collected：

找到 `if harm_umode == "all_perp":` 块的整体 if/else，在其**前面**加一个最高优先分支：
```python
                if self.cfg.monitored_coins.enabled:
                    # 监控清单模式：谐波宇宙=清单（与采集集一致），不再 all_perp/top_n，也不并 harmonic_collected
                    harm_c2s = dict(vol_c2s)
                    log.info("谐波 monitored_coins 模式：纳入清单 %d 币", len(harm_c2s))
                elif harm_umode == "all_perp":
                    ...  # 原有 all_perp 分支不动
                else:
                    ...  # 原有 top_n 分支不动
```
并把原来的 `harm_c2s.update(self.store.get_harmonic_collected())` 用 `if not self.cfg.monitored_coins.enabled:` 包住（enabled 时清单已是真相源，不再并旧表）。

- [ ] **Step 6: 采集器周期集 + 币集** — 在采集器构建处（约 886–904），enabled 时覆盖 tfs/coins：

在 `cc_tfs = list(dict.fromkeys(...))` 之后加：
```python
                if self.cfg.monitored_coins.enabled:
                    cc_c2s = dict(vol_c2s)  # 清单即采集集
                    cc_tfs = list(self.cfg.monitored_coins.timeframes) or cc_tfs
```

- [ ] **Step 7: `_periodic_candle_collect` 热载入对账** — 在该方法 `while not self._stopping:` 循环体**开头**加（enabled 时每轮对账 + 用 collect_interval_sec）：

```python
            # 监控清单热载入：每轮对账采集器币集（增删都反映，无需重启）
            if self.cfg.monitored_coins.enabled and self.candle_collector is not None:
                try:
                    target = self.store.get_monitored_coins()
                    if not target:
                        log.warning("监控清单为空，本轮跳过采集")
                        await asyncio.sleep(self.cfg.monitored_coins.collect_interval_sec)
                        continue
                    if _apply_reconcile(self.candle_collector, target):
                        log.info("采集器币集已对账监控清单：%d 币", len(target))
                except Exception as exc:  # noqa: BLE001
                    log.warning("采集器清单对账失败: %s", exc)
```
并把方法末尾 `if not is_cold: await asyncio.sleep(every)` 的 `every` 在 enabled 时取 `self.cfg.monitored_coins.collect_interval_sec`（循环顶部设 `every = self.cfg.monitored_coins.collect_interval_sec if self.cfg.monitored_coins.enabled else every`，注意 every 是参数，用局部变量 `_sleep_s` 承载避免覆盖原值）。

- [ ] **Step 8: `_periodic_harmonic_board` + `_periodic_bb_board` 对账** — 在 `_periodic_harmonic_board` 的 while 循环里，把现有"并入 harmonic_collected"块替换为：

```python
                # 监控清单热载入：enabled 时全量对账（增删）；否则保留旧 harmonic_collected 加性并入
                try:
                    if self.cfg.monitored_coins.enabled:
                        _apply_reconcile(self.harmonic_monitor, self.store.get_monitored_coins())
                    else:
                        coll = self.store.get_harmonic_collected()
                        if coll and any(c not in self.harmonic_monitor.coin_to_symbol for c in coll):
                            self.harmonic_monitor.coin_to_symbol.update(coll)
                            self.harmonic_monitor.top_n = len(self.harmonic_monitor.coin_to_symbol)
                except Exception:  # noqa: BLE001
                    pass
```
在 `_periodic_bb_board` 的 while 循环开头加（仅 enabled）：
```python
            if self.cfg.monitored_coins.enabled and self.bb_monitor is not None:
                try:
                    _apply_reconcile(self.bb_monitor, self.store.get_monitored_coins())
                except Exception:  # noqa: BLE001
                    pass
```

- [ ] **Step 9: 跑单测 + 编译检查**

Run: `./.venv/bin/python -m pytest tests/test_app_monitored_wiring.py -q && ./.venv/bin/python -m py_compile src/smc_tracker/app.py`
Expected: PASS（2 passed）+ 无编译错误

- [ ] **Step 10: 提交**

```bash
git add src/smc_tracker/app.py tests/test_app_monitored_wiring.py
git commit -m "feat(app): 监控清单选币替换 + 周期对账热载入 + 空清单守卫"
```

---

### Task 5: CLI `watch` 子命令（add / rm / list）

**Files:**
- Modify: `src/smc_tracker/cli.py`（新增 `_cmd_watch` handler + `build_parser` 注册）
- Test: `tests/test_cli_watch.py`（新建）

**Interfaces:**
- Consumes: `Store.add/remove/list_monitored_coins`（Task 1）。
- Produces: 子命令 `watch add <coins...> [--note N] [--db PATH]` / `watch rm <coins...> [--db]` / `watch list [--db]`。
  handler `_cmd_watch(args)`，`args.action ∈ {add, rm, list}`。

- [ ] **Step 1: 写失败测试** — `tests/test_cli_watch.py`

```python
"""CLI watch 子命令单测：解析 + handler 直跑（tmp db，无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.cli import build_parser
from smc_tracker.storage import Store


def test_watch_add_then_list(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    ap = build_parser()
    # add
    args = ap.parse_args(["watch", "add", "BTC", "ETH", "--note", "core", "--db", db])
    args.handler(args)
    assert Store(Path(db)).get_monitored_coins() == {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
    # list 打印含 BTC
    args = ap.parse_args(["watch", "list", "--db", db])
    args.handler(args)
    assert "BTC" in capsys.readouterr().out


def test_watch_rm(tmp_path):
    db = str(tmp_path / "t.db")
    ap = build_parser()
    args = ap.parse_args(["watch", "add", "BTC", "ETH", "--db", db])
    args.handler(args)
    args = ap.parse_args(["watch", "rm", "BTC", "--db", db])
    args.handler(args)
    assert Store(Path(db)).get_monitored_coins() == {"ETH": "ETHUSDT"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_cli_watch.py -q`
Expected: FAIL（argparse `invalid choice: 'watch'`）

- [ ] **Step 3: 加 handler** — `cli.py` 中（其它 `_cmd_*` 附近）加：

```python
def _cmd_watch(args: argparse.Namespace) -> None:
    """监控币种清单增删查（写本地 SQLite，运行中监控进程周期对账热载入）。"""
    try:
        import time as _t
        from .storage import Store

        store = Store(Path(args.db))
        if args.action == "add":
            now = int(_t.time() * 1000)
            note = args.note or ""
            items = [(c.upper(), f"{c.upper()}USDT", now, note) for c in args.coins]
            store.add_monitored_coins(items)
            print(f"[watch] 已加入 {len(items)} 币: {', '.join(c.upper() for c in args.coins)}")
        elif args.action == "rm":
            n = store.remove_monitored_coins([c.upper() for c in args.coins])
            print(f"[watch] 已移除 {n} 币")
        else:  # list
            rows = store.list_monitored_coins()
            if not rows:
                print("[watch] 监控清单为空（用 `watch add BTC ETH` 添加）")
            else:
                print(f"[watch] 监控清单（{len(rows)} 币）:")
                for coin, sym, ts, note in rows:
                    note_s = f"  # {note}" if note else ""
                    print(f"  {coin:<10} {sym:<14}{note_s}")
        store.close()
    except Exception as exc:
        print(f"[watch] 出错：{exc}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 4: 注册子命令** — `build_parser` 中（其它 `sub.add_parser` 附近）加：

```python
    # ---- watch（监控币种清单）----
    p_watch = sub.add_parser("watch", help="监控币种清单增删查（驱动多周期采集，热载入）")
    watch_sub = p_watch.add_subparsers(dest="action", metavar="<add|rm|list>", required=True)
    _w_add = watch_sub.add_parser("add", help="加入币种（如 watch add BTC ETH）")
    _w_add.add_argument("coins", nargs="+", metavar="COIN", help="币种符号（如 BTC ETH）")
    _w_add.add_argument("--note", default="", metavar="N", help="可选备注（为什么加）")
    _w_add.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                        help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    _w_rm = watch_sub.add_parser("rm", help="移除币种（如 watch rm BTC）")
    _w_rm.add_argument("coins", nargs="+", metavar="COIN", help="币种符号")
    _w_rm.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                       help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    _w_list = watch_sub.add_parser("list", help="打印当前监控清单")
    _w_list.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                         help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_watch.set_defaults(handler=_cmd_watch)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_cli_watch.py -q`
Expected: PASS（2 passed）

- [ ] **Step 6: 提交**

```bash
git add src/smc_tracker/cli.py tests/test_cli_watch.py
git commit -m "feat(cli): watch 子命令（监控清单 add/rm/list）"
```

---

### Task 6: Dashboard — `/api/monitored` + discover 改写真相源

**Files:**
- Modify: `src/smc_tracker/dashboard.py`（加纯函数 `apply_monitored_action` + handler + 路由 + discover 改写）
- Test: `tests/test_dashboard_monitored.py`（新建，测纯函数）

**Interfaces:**
- Consumes: `Store.add/remove/list_monitored_coins`（Task 1）。
- Produces:
  - 模块级 `apply_monitored_action(store, action: str, coins: list[str], note: str, now_ms: int) -> dict` —
    `action ∈ {list, add, rm}`，返回 `{"monitored": [...], "changed": int}`，纯逻辑可测。
  - 路由 `GET/POST /api/monitored`（handler 调用上面函数）。
  - `handle_harmonic_discover` 改用 `add_monitored_coins`（统一真相源）。

- [ ] **Step 1: 写失败测试** — `tests/test_dashboard_monitored.py`

```python
"""dashboard 监控清单 API 纯逻辑单测（tmp db，无 HTTP）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.dashboard import apply_monitored_action
from smc_tracker.storage import Store


def _store():
    return Store(Path(tempfile.mkdtemp()) / "t.db")


def test_add_then_list():
    s = _store()
    r = apply_monitored_action(s, "add", ["BTC", "eth"], "core", 123)
    assert r["changed"] == 2
    r2 = apply_monitored_action(s, "list", [], "", 0)
    coins = {row["coin"] for row in r2["monitored"]}
    assert coins == {"BTC", "ETH"}  # 大写归一


def test_rm():
    s = _store()
    apply_monitored_action(s, "add", ["BTC", "ETH"], "", 1)
    r = apply_monitored_action(s, "rm", ["BTC"], "", 0)
    assert r["changed"] == 1
    coins = {row["coin"] for row in r["monitored"]}
    assert coins == {"ETH"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_dashboard_monitored.py -q`
Expected: FAIL（`ImportError: cannot import name 'apply_monitored_action'`）

- [ ] **Step 3: 加纯函数** — `dashboard.py` 模块级（靠近其它 `build_*` 函数）加：

```python
def apply_monitored_action(
    store: Any, action: str, coins: list[str], note: str, now_ms: int,
) -> dict:
    """监控清单 API 纯逻辑：执行 add/rm/list，返回 {"monitored": rows, "changed": n}。

    coins 统一大写归一；add 用 coin+'USDT' 作 symbol。可单测，不碰 HTTP。
    """
    changed = 0
    cs = [c.upper() for c in (coins or []) if c]
    if action == "add" and cs:
        store.add_monitored_coins([(c, f"{c}USDT", now_ms, note or "") for c in cs])
        changed = len(cs)
    elif action == "rm" and cs:
        changed = store.remove_monitored_coins(cs)
    rows = [
        {"coin": coin, "symbol": sym, "added_ts": ts, "note": n}
        for coin, sym, ts, n in store.list_monitored_coins()
    ]
    return {"monitored": rows, "changed": changed}
```

- [ ] **Step 4: 加 handler + 路由** — 在 `handle_harmonic_discover` 附近加 handler，并在路由注册区加两行：

```python
    async def handle_monitored(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET=list；POST body {action, coins, note} 执行 add/rm。监控进程周期对账热载入。"""
        import time as _t  # noqa: PLC0415
        now = int(_t.time() * 1000)
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            action = str(body.get("action") or "list")
            coins = body.get("coins") or []
            if isinstance(coins, str):
                coins = [coins]
            note = str(body.get("note") or "")
        else:
            action, coins, note = "list", [], ""
        try:
            result = apply_monitored_action(store, action, list(coins), note, now)
            return aiohttp.web.json_response(result, dumps=lambda o: json.dumps(o, default=str))
        except Exception as exc:  # noqa: BLE001
            return aiohttp.web.json_response({"monitored": [], "error": str(exc)}, status=500)
```
路由（`app.router.add_*` 区）：
```python
    app.router.add_get("/api/monitored", handle_monitored)
    app.router.add_post("/api/monitored", handle_monitored)
```

- [ ] **Step 5: discover 改写真相源** — 在 `handle_harmonic_discover` 中，把
`store.add_harmonic_collected([(c, candidates[c], now) for c in found])`
改为
`store.add_monitored_coins([(c, candidates[c], now, "discover") for c in found])`
并把上方排除集 `current |= set(store.get_harmonic_collected())` 增补一行
`current |= set(store.get_monitored_coins())`（避免重复扫已在清单的币）。

- [ ] **Step 6: 跑测试确认通过 + 编译检查**

Run: `./.venv/bin/python -m pytest tests/test_dashboard_monitored.py -q && ./.venv/bin/python -m py_compile src/smc_tracker/dashboard.py`
Expected: PASS（2 passed）+ 无编译错误

- [ ] **Step 7: 提交**

```bash
git add src/smc_tracker/dashboard.py tests/test_dashboard_monitored.py
git commit -m "feat(dashboard): /api/monitored 增删查 + discover 写监控清单"
```

---

### Task 7: 文档/示例配置 + 全量回归 + 零孤儿核对

**Files:**
- Modify: `config/config.example.yaml`（加 `monitored_coins` 段说明）
- Modify: `CLAUDE.md`（§五 入口处补 `watch` 子命令一行）

- [ ] **Step 1: 示例配置** — `config/config.example.yaml` 末尾加：

```yaml
# 监控币种清单（watchlist-multi-tf）：enabled=true 时只为清单内币种采集多周期数据，
# 不再监控全市场。清单用 `PYTHONPATH=src ./.venv/bin/python -m smc_tracker watch add BTC ETH`
# 或 dashboard /api/monitored 增删，运行中热载入（≤1 个采集周期生效）。enabled=false 保持现状。
monitored_coins:
  enabled: false
  timeframes: ["15m", "1H", "4H", "6H", "12H", "1D", "1W"]   # 6H 替代 Bitget 不支持的 8h
  collect_interval_sec: 300
```

- [ ] **Step 2: CLAUDE.md 入口补一行** — §五「统一 CLI」子命令列表追加：
`watch add/rm/list`(监控币种清单，驱动多周期采集，热载入)。

- [ ] **Step 3: 零孤儿自查** — 确认每个新符号都被消费：

Run:
```bash
cd /Users/zhaoleon/Desktop/smc
grep -rn "monitored_coins\|resolve_monitored_universe\|reconcile_universe\|_apply_reconcile\|apply_monitored_action\|_cmd_watch" src/smc_tracker --include="*.py" | grep -v "def \|class " | head
```
Expected: 每个符号在 db/config/app/cli/dashboard 至少各有 1 处消费（无定义而不用）。

- [ ] **Step 4: 全量回归**

Run: `./.venv/bin/python -m pytest -q`
Expected: PASS（≥ 357 + 新增 24 测试，全绿）

- [ ] **Step 5: 提交**

```bash
git add config/config.example.yaml CLAUDE.md
git commit -m "docs: monitored_coins 示例配置 + CLI 入口说明"
```

---

## Self-Review

**1. Spec coverage：**
- 3.1 DB 表/CRUD/迁移 → Task 1 ✓
- 3.2 MonitoredCoinsCfg + load + resolve_monitored_universe + 周期校验 → Task 2 ✓
- 3.3 选币替换接线 + 谐波/BB timeframes 对齐 → Task 4 Step 4–6（谐波默认 timeframes 调整在 Task 4 Step 5 的谐波分支用 vol_c2s/cc_tfs 覆盖，不依赖改默认值，避免 enabled=false 回归）✓
- 3.4 热载入对账（reconcile + 周期任务）→ Task 3 + Task 4 Step 7–8 ✓
- 3.5 CLI watch → Task 5 ✓
- 3.6 Dashboard /api/monitored + discover 改写 → Task 6 ✓
- 3.7 边界（空清单守卫、symbol 兜底）→ Task 2 resolve 兜底 + Task 4 Step 4/7 空清单 warning ✓
- 四、测试 → 各 Task 自带 + Task 7 全量回归 ✓

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整代码。Task 4 涉及定位现有行的改写，已给出"找到 X 改为 Y"的精确锚点。

**3. Type consistency：**
- `add_monitored_coins(items)` 行格式 `(coin, symbol, added_ts, note)` 在 Task 1 定义，Task 5/6 调用一致（4 元组）。
- `resolve_monitored_universe(monitored, base_map, tickers)` 签名 Task 2 定义，Task 4 调用一致。
- `reconcile_universe(current, target) -> (added: dict, removed: set)` Task 3 定义，`_apply_reconcile` Task 4 消费一致。
- `apply_monitored_action(store, action, coins, note, now_ms) -> dict` Task 6 定义/测试一致。

> 备注：3.3 说"谐波默认 timeframes 改 30m→6H"。为**严守零回归**，本计划改为：enabled=true 时用 `cc_tfs/harm_c2s` 在运行时覆盖（Task 4 Step 5–6），**不改 HarmonicCfg 默认值**，从而 enabled=false 路径完全不受影响。spec 意图（清单模式采 7 周期）完全达成，且回归风险更低。
