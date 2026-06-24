# 路线 A — 地基加固（诚实可验证测量基础设施）实现级 Spec

> 范围：本段只规划**本地实现**（TDD + 单测），**部署须用户单独批准**（CLAUDE.md / harmonic-redesign-state）。
> 原则：诚实区分**真 bug**（已实证）vs **过度工程**（明确排除）。**不引入时序 DB、不引入连接池**——单机 SQLite + 单监控进程 + 单 dashboard 进程的数据规模（OI/sm_events 7-30 天、谐波 7 天）完全不需要。

---

## A0. 现状与隐患实证（file:line）

| # | 隐患 | 实证位置 | 真 bug? |
|---|------|---------|---------|
| 1 | `Store.__init__` PRAGMA 缺 `busy_timeout` | `db.py:359-364`（仅 WAL/synchronous/foreign_keys） | **真**：两进程并发写 → `database is locked` 立即抛 |
| 2 | 多进程并发写同一 `.db` | 监控进程 `app.py:1329` `insert_harmonic_setups` + dashboard **独立进程**（`cli.py:465 asyncio.run(serve)` → `dashboard.py:940 Store(db_path)`）的 `/api/harmonic/discover` 端点 `dashboard.py:1052` 同样 `insert_harmonic_setups` | **真**：两个 `Store`，两条连接，WAL 下读不阻塞但**写写互斥** |
| 3 | 热路径同步 SQLite | `_on_sm_event` WS 回调直接 `self.store.insert_sm_event(...)`（`app.py:249`）；`_on_structure` 同步 `self.store.oi_change(...)` 子查询（`app.py:352`，`db.py:451-459` 内含一次 `SELECT ... ORDER BY ts DESC LIMIT 1`） | **真**：在 WS 协程内同步 I/O；加了 `busy_timeout=5000` 后写锁等待会**阻塞整个 event loop 最长 5s**，使该问题从"偶发抛错"恶化为"周期性全系统冻结" |
| 4 | 顶层 `gather` 无 `return_exceptions`/无 supervisor | `app.py:1521-1549`（25 个 coro 裸 gather） | **真（静默死）**：任一 `_periodic_*` 抛非 `CancelledError` 异常 → gather 取消其余全部任务、`run()` 整体崩。WS `run()` 自身有自重连（`ws_client.py:90-124` 网络异常 backoff），但**非网络异常 / 任一 periodic 任务异常**无人兜底 |
| 5 | 推送 Queue 无界 | `app.py:162 asyncio.Queue()`（无 maxsize），drain worker `app.py:484` 每条 `sleep(1.6s)` ≈ 37.5 条/min 上限；爆发推送 `put_nowait`（`app.py:471/477`）无界堆积 | **真（长跑 OOM）**：生产快于消费时队列单调增长 |
| 6 | `harmonic_setups` 无 `ts` 索引 | 建表 `db.py:286-317` 无任何 INDEX；查询 `db.py:686 WHERE ts=(SELECT MAX(ts)...)`、`db.py:729 WHERE coin=? ORDER BY ts`、cleanup `prune_before(harmonic_setups, ts, ...)`（`app.py:772`）全走全表扫 | **真（性能，非正确性）**：7 天 × 多周期 × 多币累积后 `MAX(ts)` 子查询与按币历史扫全表 |

**明确排除（过度工程，不做）**：连接池（单进程单连接够用，`check_same_thread=False` 已支持 to_thread）；时序 DB / DuckDB；WAL checkpoint 调参（默认 PASSIVE 够用）；多线程写池（写已串行化到单 flush 协程即可）。

---

## A1. db.py PRAGMA 加固（busy_timeout 为核心，其余评估后择优）

**目标**：根治多进程 `database is locked`；在不引入复杂度前提下提升只读吞吐。

**改动 1.1** — `db.py:361-363`（PRAGMA 块）追加：
```
PRAGMA busy_timeout=5000;   -- 写锁竞争时等待最多 5s 而非立即抛 locked（多进程根治）
```
位置：紧接 `journal_mode=WAL` 之后、`executescript(SCHEMA)` 之前，使**所有** `Store` 连接（监控进程 + dashboard 进程）一致生效。

**改动 1.2 — 评估后纳入（低风险高收益，纳入）**：
- `PRAGMA cache_size=-16000;`（16MB 页缓存，负值=KB；默认 ~2MB。谐波/OI 历史扫描读多，收益明确，内存可控）→ **纳入**
- `PRAGMA temp_store=MEMORY;`（临时 B-tree/排序走内存，`ORDER BY confidence DESC` / `MAX(ts)` 子查询受益）→ **纳入**

**改动 1.3 — 评估后排除或限定**：
- `mmap_size`：跨平台（本机 darwin + 部署目标）行为不一，WAL 下与 mmap 交互有历史坑 → **排除**（诚实：收益不确定，不值得引入平台差异风险）
- `PRAGMA optimize`：**限定**为在 `close()`/进程退出前调用一次（或 `_periodic_cleanup` 末尾每 N 轮一次），**不**放 `__init__`（`optimize` 在空库 `__init__` 期无意义）。本段**仅在 `_periodic_cleanup` 现有循环末尾**追加 `self.store.conn.execute("PRAGMA optimize")`（`app.py:776-795` 内，已有 every=600s 节流）→ **纳入（轻量）**

**新增方法**（可选，供测试断言 PRAGMA 生效，零孤儿——被 test 引用即接入）：在 `Store` 加 `def pragma(self, name: str) -> int|str: return self.conn.execute(f"PRAGMA {name}").fetchone()[0]`。签名确定性、纯读。

---

## A2. 热路径异步化（与 busy_timeout **成对**，最关键）

**目标**：把 WS 回调里的同步 DB I/O 移出 event loop，使 A1 的 `busy_timeout=5000` 不会在写锁竞争时冻结 loop。沿用 **OI monitor 已验证的 buffer→periodic flush 模式**（`bitget_oi_monitor.py:53/99/149-155`：`_buffer` list、append、`flush()` executemany），不发明新机制（去重）。

### 2a. `sm_events` 写入：回调入缓冲，`_periodic_flush` 批量落

**数据流**（新）：
```
WS thread → _on_sm_event(evt)
  ├─ 内存逻辑保持同步（whale_acc 累积、_emit 推送）—— 不碰 DB
  └─ self._sm_buffer.append(<13列 tuple>)   # 替代 app.py:249 的 self.store.insert_sm_event(...)

_periodic_flush (app.py:746, 已存在, every=5s)
  └─ 新增：self._flush_sm_events()  # swap buffer + asyncio.to_thread(store.insert_sm_events_batch, rows)
```

**改动 2a.1** — `app.py` `__init__`（约 `app.py:182` 邻近 `_bg_tasks` 处）新增 slots/字段：
```
self._sm_buffer: list[tuple] = []   # sm_events 热路径缓冲（WS 回调 append，_periodic_flush 批量落）
```
**改动 2a.2** — `app.py:249` 替换 `self.store.insert_sm_event((...))` → `self._sm_buffer.append((...))`（同一 13 列 tuple，**列序不变**）。
**改动 2a.3** — `db.py` 新增 `def insert_sm_events_batch(self, rows: Iterable[tuple]) -> int:`（executemany，复用 `insert_sm_event` 的 SQL，空 rows 安全返回 0；复用 `BEGIN/COMMIT/ROLLBACK` 事务模式如 `db.py:652-668`）。
**改动 2a.4** — `app.py:746 _periodic_flush` 内追加（**用 `asyncio.to_thread` 包**，db.py:6/358 已注明 WAL + check_same_thread=False 下安全）：
```
if self._sm_buffer:
    rows, self._sm_buffer = self._sm_buffer, []
    await asyncio.to_thread(self.store.insert_sm_events_batch, rows)
```
**改动 2a.5** — `stop()`（`app.py:1551`）与异常退出路径冲刷 `_sm_buffer`（不丢事件），同步落库即可（退出非热路径）。

> 延迟权衡（诚实标注）：sm_events 落库最多延后 5s 才可被 dashboard/查询看到。whale 信号**推送**仍即时（推送链路不依赖 DB 读）；`sm_events` 仅作历史/复盘，5s 延迟无害。

### 2b. `oi_change` 子查询：热路径改读内存缓存

`_on_structure`（`app.py:343`，结构突破回调）同步调 `store.oi_change(symbol, 600_000, now)`（`db.py:451`，含一次磁盘 SELECT）。结构事件**频率低**（远低于 sm_events），但同样在 loop 内同步 I/O 且会被 busy_timeout 阻塞。

**方案对比**：
- 方案 A（选）：复用 OI monitor 内存最新值。`oi_monitor` 已持 ticker/OI（`app.py:231 self.oi_monitor.ticker(sym)`）。新增 OI monitor 内存环形缓存 `{symbol: deque[(ts, oi)]}`（窗口 ≥ 600s），`_on_structure` 改读内存算变化，**完全不碰 DB**。
- 方案 B（弃）：把整个 `_on_structure` 用 to_thread 包——结构回调里还有 numpy/内存逻辑，包整段过重且引入跨线程共享态。
- 方案 C（兜底，若 2b-A 工作量超预算）：仅把 `oi_change` 调用用 `asyncio.to_thread` 包（`chg = await asyncio.to_thread(self.store.oi_change, symbol, 600_000, now)`）——但 `_on_structure` 是**同步回调**，需先确认其调用点是否在 async 上下文（StructureFeed `on_event`）。**实证后决定**：若回调为同步则取方案 A；本段**默认方案 A**。

**改动 2b.1** — `bitget_oi_monitor.py` 新增 `def oi_window(self, symbol: str, window_ms: int, now_ms: int) -> tuple[float, float] | None`（返回 `(latest_oi, past_oi)`，纯内存、确定性、numpy 不必要）。
**改动 2b.2** — `app.py:352` 改 `chg = self.oi_monitor.oi_window(symbol, 600_000, now)`（回退：内存无足够历史时返回 None，与现有 `if chg and chg[1]` 守卫兼容，不裸下标）。

> 注：`app.py:851/1021` 的 `oi_change` 在 `_periodic_*`（非 WS 热路径）协程内，**不属本段范围**，保持磁盘查询（已在独立 periodic 任务，被 A3 supervisor 兜底；如需可后续 to_thread，列入风险表备注，不在本段实现以控制 blast radius）。

---

## A3. 顶层 gather → per-task supervisor（不静默死）

**目标**：任一后台任务异常 → 捕获 + log + 指数退避**重启该任务**，不连累其余；WS `run()` 保留自重连（`ws_client.py:90`），supervisor 只兜"逃逸到任务边界"的异常。

**方案对比**：
- 方案 A（选）：新增 `supervise(coro_factory, name, max_backoff)` helper，包裹每个 `_periodic_*`。`coro_factory` 为 `Callable[[], Awaitable]`（因重启需重新创建 coro，coro 不可复用）。
- 方案 B（最低保障）：`gather(..., return_exceptions=True)` + 看门狗——能防"一损俱损"，但**不重启**，死了的任务静默不再跑，与"不静默死"目标弱。→ 仅作 fallback 文档。

**采用方案 A**。

**改动 3.1 — 新文件** `src/smc_tracker/supervisor.py`（零孤儿：app.py 导入 + 从 `smc_tracker/__init__.py` 不导出顶层但 app 直接 import；若项目惯例要求，加入 `monitor/__init__.py` 风格——**确认**：放 `src/smc_tracker/supervisor.py`，`app.py` `from .supervisor import supervise` 即接入运行时）。

签名：
```python
async def supervise(
    factory: Callable[[], Awaitable[None]],
    *, name: str, base_backoff: float = 1.0, max_backoff: float = 60.0,
    log: logging.Logger,
) -> None:
    """无限重启监督：调用 factory() 得到 coro 并 await；
    正常返回 → 记 info 后按 base_backoff 重启（periodic 任务本应永不返回，返回视为异常退出）；
    抛非 CancelledError 异常 → log.exception + 指数退避后重启；
    CancelledError → 向上抛（响应 stop()，不吞）。"""
```
- 退避：成功运行 ≥ `reset_after`（如 30s）则 backoff 复位为 base（避免崩溃循环放大退避）。
- 中文注释 + 类型注解，纯异步、无 I/O，**确定性可单测**（注入假 factory + fake clock/计数）。

**改动 3.2** — `app.py:1521-1549` `gather` 重构：每个 `self._periodic_*` 与 `self.hl_ws.run`/`self.bg_ws.run` 包成 `supervise(lambda: self._periodic_xxx(), name="...", log=log)`。WS 的 `run()` 自身重连，supervise 仅兜其逃逸异常（退避更大，如 `max_backoff` 沿用 WS 自身策略，不重复退避——WS 传 `base_backoff=5, max_backoff=60`）。
- 保留外层 `gather`（现在每个 arg 都是 supervise 包裹的、永不正常返回的 coro），可加 `return_exceptions=True` 作**第二道**防线（supervise 已吞，理论不触发；防御性）。

> 不静默：supervise 用 `log.exception`（含 traceback）+ 可选 hook 到 `HealthMonitor`（`app.py:200 self.health`）记录"任务重启计数"，使 dashboard `/health` 能暴露。本段**最小实现**：仅 log.exception + 计数器字段 `self._task_restarts: dict[str,int]`，health 暴露列入风险表备注（不强制本段）。

---

## A4. 推送 Queue 背压（maxsize + 满时丢最旧/合并）

**目标**：长跑不 OOM；爆发推送有界。

**改动 4.1** — `app.py:162`：`asyncio.Queue()` → `asyncio.Queue(maxsize=2000)`。
**改动 4.2** — 入队点 `app.py:471`（`_push`）/ `app.py:477`（`_push_harmonic`）：`put_nowait` 改为带背压的私有 helper `self._enqueue_push(text, notifier)`：
```python
def _enqueue_push(self, text: str, notifier: Any) -> None:
    """背压入队：满时丢最旧一条（get_nowait 弃头）再入新，保证最新告警不丢、队列有界。"""
    try:
        self._push_queue.put_nowait((text, notifier))
    except asyncio.QueueFull:
        try:
            self._push_queue.get_nowait()      # 弃最旧
            self._push_queue.task_done()
        except asyncio.QueueEmpty:
            pass
        self._push_queue.put_nowait((text, notifier))
        self._push_dropped += 1                # 计数，health/log 可见（不静默丢）
```
- `self._push_dropped: int = 0` 字段（`__init__` 邻近 push_queue）。
- **合并**（可选，本段纳入轻量版）：丢弃时若新旧文本同 category 前缀可合并——**评估为过度**（drain worker 已 1.6s 节流 + HLDigest `app.py:160` 已在上游做分类合并）→ 本段**只做"丢最旧 + 计数"**，合并交由既有 HLDigest，避免重复造轮子（去重原则）。
**改动 4.3** — `_periodic_push_drain`（`app.py:479`）不变（已健壮：失败 log.warning 不中断）。

---

## A5. harmonic_setups 索引

**目标**：消除全表扫（`MAX(ts)` 子查询 / 按币历史 / prune）。

**改动 5.1** — `db.py:317` 建表后（紧随 `);`）追加：
```sql
CREATE INDEX IF NOT EXISTS ix_harmonic_ts      ON harmonic_setups(ts);
CREATE INDEX IF NOT EXISTS ix_harmonic_coin_ts ON harmonic_setups(coin, ts);
```
覆盖：`db.py:686 WHERE ts=(SELECT MAX(ts))`（ix_harmonic_ts）、`db.py:729 WHERE coin=? ORDER BY ts`（ix_harmonic_coin_ts）、`prune_before` 删旧（ix_harmonic_ts）。`IF NOT EXISTS` → 旧库 `executescript(SCHEMA)` 幂等自动建（迁移免代码，与现有索引风格一致 `db.py:42-350`）。

---

## A6. TDD 测试计划（合成数据，确定性）

新文件 `tests/test_foundation_hardening.py`（或拆 3 文件，按既有 test 粒度）。全部用合成数据、无网络、无真实 DB 文件依赖（`tmp_path` fixture）。

**T1 PRAGMA 生效**（A1）
- `Store(tmp_path/"x.db")` → 断言 `store.pragma("busy_timeout") == 5000`、`pragma("journal_mode")=="wal"`、`pragma("cache_size")` 为设定值、`pragma("temp_store")` 内存档。

**T2 多进程 busy_timeout 不抛 locked**（A1+A2，核心回归）
- 两个 `Store` 指向**同一** `tmp_path` 文件；线程 A 开写事务 hold 短暂；线程 B `insert_harmonic_setups` → 断言**不抛** `OperationalError: database is locked`（在 busy_timeout 窗口内完成）。对照：临时设 `busy_timeout=0` 应抛（证明测试有效）。

**T3 sm_events 缓冲→批量落**（A2a）
- 构造 app 部分实例 / 或直接测 `_sm_buffer` 语义：调 `_on_sm_event`（合成 `SmartMoneyEvent`）N 次 → 断言 `len(self._sm_buffer)==N` 且**期间 store.sm_events 表行数仍为 0**（证明未在热路径写）；手动跑一次 flush 逻辑 → 断言表行数==N、buffer 清空、列序与 `insert_sm_event` 一致（取一行比对 13 列）。
- `insert_sm_events_batch([])` 返回 0、不抛。

**T4 oi_window 内存查询**（A2b）
- 喂 OI monitor 合成 `(ts, oi)` 序列；`oi_window(sym, 600_000, now)` 断言返回 `(latest, past)` 数值正确；窗口内无历史 → None；与旧 `db.oi_change` 同输入**数值一致**（交叉验证，确定性 golden）。

**T5 supervise 重启 + 退避**（A3）
- 注入 `factory` 抛 ValueError 前 3 次、第 4 次挂起 → 断言被调用 4 次、退避序列单调（用注入的 `sleep` spy 记录 backoff，断言 `[1,2,4]` 或封顶逻辑）；注入 `CancelledError` → 断言向上传播（不吞）；成功运行 > reset_after 后再崩 → 断言退避复位。全部用 fake sleep（`monkeypatch asyncio.sleep`），无真实等待。

**T6 push 背压**（A4）
- `maxsize=3` 小队列填满后 `_enqueue_push` 第 4 条 → 断言队列长度仍==3、**队头（最旧）被弃**、新条在队尾、`_push_dropped==1`。

**T7 harmonic 索引存在 + 被用**（A5）
- 建 `Store` → `PRAGMA index_list(harmonic_setups)` 含 `ix_harmonic_ts`/`ix_harmonic_coin_ts`；`EXPLAIN QUERY PLAN SELECT ... WHERE ts=(SELECT MAX(ts)...)` 断言 plan 含 `USING INDEX`（不再 `SCAN harmonic_setups` 全表）。

**全量基线**：跑 `./.venv/bin/python -m pytest -q` 必须保持全绿（基线 357 passed，新增 ≥7 用例后数字上升）。`python -m py_compile` 改动文件。

---

## A7. 风险与回滚

| 改动 | 风险 | 缓解 / 回滚 |
|------|------|-------------|
| A1 PRAGMA | cache_size 占内存 16MB/连接 | 数值可配；回滚=删 PRAGMA 行（无 schema 变更，零迁移风险） |
| A2a 缓冲 | 进程崩溃丢未 flush 的 ≤5s sm_events | sm_events 仅复盘用，可接受；`stop()` 冲刷覆盖正常退出。回滚=改回 `app.py:249` 直接 insert |
| A2b 内存 OI | OI monitor 重启后窗口内历史为空 → 短暂 None | 守卫 `if chg and chg[1]` 已兼容 None（降级=本次结构事件不带 OI 上下文，非崩溃）。回滚=改回 `store.oi_change` |
| A3 supervisor | helper 逻辑错误反而吞 CancelledError 致 stop() 卡住 | T5 显式断言 CancelledError 传播；回滚=`gather(..., return_exceptions=True)` 最低保障 |
| A4 背压 | 弃最旧丢告警 | 只在 >2000 积压（异常工况）才弃 + 计数可见；正常 37.5 条/min 远不触发。回滚=`maxsize=0`（无界，回退旧行为） |
| A5 索引 | 写入轻微变慢（索引维护） | harmonic 写为低频批量（每周期一次），可忽略；回滚=`DROP INDEX`（幂等，无数据风险） |

**整体回滚粒度**：6 项相互独立，可逐项 revert。A1+A2 **成对**（A1 加 busy_timeout 而不做 A2 会恶化 loop 阻塞），二者须同迭代落地或同迭代回滚——**Sonnet 实现时 A1、A2 必须同 PR**。

---

## A8. 本段涉及/修改文件清单（供跨路线冲突检测）

**修改**：
- `src/smc_tracker/storage/db.py` — A1（PRAGMA `db.py:361` 区）、A2a（新增 `insert_sm_events_batch`）、A5（`db.py:317` 后加 2 索引）、A1 新增 `pragma()` 读方法
- `src/smc_tracker/app.py` — A2a（`__init__` 加 `_sm_buffer`；`app.py:249` 改 append；`_periodic_flush` 746 加 to_thread flush；`stop()` 冲刷）、A2b（`app.py:352` 改 `oi_window`）、A3（`gather` `1521-1549` 包 supervise + import）、A4（`162` maxsize、`471/477` 改 `_enqueue_push`、新增字段+helper）、A1（`_periodic_cleanup` 末尾 `PRAGMA optimize`）
- `src/smc_tracker/monitor/bitget_oi_monitor.py` — A2b（新增 `oi_window` 内存窗口查询 + 内部 `(ts,oi)` 缓存维护）

**新增**：
- `src/smc_tracker/supervisor.py` — A3 `supervise()`（app.py import 接入；如项目惯例需导出，`src/smc_tracker/__init__.py` 加 `from .supervisor import supervise`）
- `tests/test_foundation_hardening.py` — A6 全部用例

**冲突热点提示**（给编排器）：
- `app.py` 改动密集（`__init__`、`_periodic_flush`、`gather`、push 路径）——**任何同时改 app.py 顶层 `gather` 或 `__init__` 的其他路线必须与本路线串行 / 同 worktree 协调**。
- `db.py` 的 PRAGMA 块、SCHEMA 字符串、`harmonic_setups` 区——若其他路线也改 db.py schema/索引，需 rebase 协调。
- `bitget_oi_monitor.py` 若被"信号/OI velocity"路线（仓库已有 `signals/oi_velocity.py`）同时改，需协调内存缓存字段命名。
- **无新增运行时入口**（CLI/__main__ 不改），降低与 dashboard/CLI 路线冲突面。