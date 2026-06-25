# 监控清单驱动多周期采集（watchlist-multi-tf）设计

> 状态：已批准（用户 2026-06-26 确认设计 → "执行"）
> 目标分支：`feat/watchlist-multi-tf`
>
> **命名修订（实证发现）**：`config.py::Config.load` 已有顶层 `watchlist` 键（追踪**地址**
> `list[WatchAddress]`）。为避免冲突，本特性的**配置段/dataclass/DB 表/方法**统一用
> `monitored_coins`（"监控清单"），不复用 `watchlist`。CLI 子命令仍叫 `watch`（地址侧无 CLI，无冲突）。

## 一、问题与目标

当前系统 `harmonic.universe_mode: all_perp` 让谐波监控**全部 ~661 个 Bitget 永续币**，K 线采集器
（`BitgetCandleCollector`）也轮转采集全集多周期落 DB。用户要求：

> **基于一份「监控币种清单」才采集多周期数据（15m/1h/4h/8h/12h/1d/1w）；不用一直监控所有币种；支持热载入。**

即把"白名单驱动"从现有的**补充模式**（`harmonic_collected` 表补充 top_n）升级为**主模式**：
清单内的币才采集多周期数据，清单外的币不采。增删清单支持热载入（运行中无需重启）。

### 已实证的约束（第一性原理，CLAUDE.md §一-3）

- **Bitget K 线接口不支持 `8h`**。`bitget/rest.py::GRANULARITY_MS` 实际支持
  `1m/3m/5m/15m/30m/1H/4H/6H/12H/1D/3D/1W/1M`（无 8H）。用户已拍板用 **6H** 替代。
  → 最终多周期集：**`15m / 1H / 4H / 6H / 12H / 1D / 1W`（7 周期）**。

### 用户已确认的三项决策

1. **清单载体**：DB 表 + CLI/dashboard 增删（复用现有热载入路径）。
2. **采集范围**：**替换** —— 只采集清单内币种（采集器与谐波/BB 监控宇宙都改为清单驱动）。
3. **8h 一档**：用 **6H** 替代。

## 二、方案选择

采用 **方案 A：新建专用 `monitored_coins` 表 + `monitored_coins.enabled` 主开关**。

- 新表作为「监控清单」唯一真相源，语义干净。
- `monitored_coins.enabled=true` → 采集器/谐波/BB 的币集全部 = DB 清单；
  `monitored_coins.enabled=false` → 完全是现状 all_perp 行为（**零回归**，新路径完全旁路）。
- 把现有 `harmonic_collected`（discover 按钮）统一并入新表（一次性迁移 + discover 改写新表），
  避免两个重叠概念。

> 否决的方案 B（复用 `harmonic_collected`）：命名误导（"谐波补充"→"全局主清单"），
> 且现有它是**补充** top_n 的语义，硬改成**替换**会让正在用的表行为突变，回归风险高。

## 三、详细设计

### 3.1 数据模型（`storage/db.py`）

新表，镜像 `harmonic_collected` 的方法风格：

```sql
CREATE TABLE IF NOT EXISTS monitored_coins (
    coin     TEXT    NOT NULL PRIMARY KEY,
    symbol   TEXT    NOT NULL,
    added_ts INTEGER NOT NULL,
    note     TEXT    NOT NULL DEFAULT ''   -- 可选备注：为什么加这个币
);
```

方法（均空安全、幂等、异常向上抛由调用方 warn）：

- `add_monitored_coins(items: Iterable[tuple]) -> None`：`(coin, symbol, added_ts, note)` 幂等 upsert
  （`ON CONFLICT(coin) DO UPDATE SET symbol=excluded.symbol, note=excluded.note`）。
- `remove_monitored_coins(coins: Iterable[str]) -> int`：删除指定 coin，返回删除行数。
- `get_monitored_coins() -> dict[str, str]`：返回 `{coin: symbol}`（供运行时对账）。
- `list_monitored_coins() -> list[tuple]`：返回 `(coin, symbol, added_ts, note)` 行（按 added_ts 升序，供 CLI/dashboard 展示）。

**一次性迁移**：`Store` 初始化建表后，若 `monitored_coins` 为空且 `harmonic_collected` 非空，
把后者拷入前者（`note='migrated:harmonic_collected'`）。保证历史"发现搜集"的币无缝接入新清单。

### 3.2 配置（`config.py`）

新增 `MonitoredCoinsCfg` dataclass + 解析：

```yaml
monitored_coins:
  enabled: true                                       # 主开关；false=现状 all_perp（零回归）
  timeframes: ["15m", "1H", "4H", "6H", "12H", "1D", "1W"]   # 多周期采集集（7 周期）
  collect_interval_sec: 300                           # 采集轮询间隔（稳态）
```

```python
@dataclass(slots=True)
class MonitoredCoinsCfg:
    enabled: bool = False
    timeframes: list[str] = field(
        default_factory=lambda: ["15m", "1H", "4H", "6H", "12H", "1D", "1W"])
    collect_interval_sec: float = 300.0
```

`Config.load` 透传 `monitored_coins` 段（`timeframes` 列表强制 `list()`，与现有 bb/harmonic 一致）；
对 `timeframes` 用 `GRANULARITY_MS` 校验，剔除非法周期并 warn（数据质量守卫，CLAUDE.md §三-3）。

纯函数（可单测，不碰 DB）：

```python
def resolve_monitored_universe(
    monitored: dict[str, str],      # {coin: symbol}，来自 store.get_monitored_coins()
    base_map: dict[str, str],       # {symbol: baseCoin}，来自 perp_base_coins()
    tickers: dict[str, dict],       # {symbol: ticker}，按成交额排序用
) -> dict[str, str]:
    """把 DB 清单解析为 {coin: symbol}，按 24h 成交额降序。
    - symbol 优先用清单存的；缺失/与 base_map 冲突时回退 base_map 反查或 coin+'USDT'。
    - 纯函数：确定性、无副作用、可测。"""
```

### 3.3 选币接线 —— 替换（`app.py`）

在 `_run`（构建监控器处，约 690–905 行）按 `monitored_coins.enabled` 分支：

- **enabled=true**：
  - `vol_c2s = resolve_monitored_universe(store.get_monitored_coins(), base_map, tickers_map)`
    （谐波/BB 共用基础集，均改为清单驱动）。
  - 采集器 `cc_c2s = dict(vol_c2s)`；`cc_tfs = cfg.monitored_coins.timeframes`（7 周期），
    `cc_bars = max(bb.bars, harmonic.bars)`。
  - 谐波/BB 仍各自取 `timeframes`；为命中 DB 缓存，谐波/BB 的 `timeframes` 应是采集集的子集
    （默认值对齐到 7 周期，谐波默认 30m → 6H）。非子集的 tf 由现有 live 回退兜底（不崩，仅多打 API）。
- **enabled=false**：现有分支完全不变（`resolve_universe` + `harmonic.universe_mode`）。

谐波默认 `timeframes` 由 `["15m","30m","1H","4H","12H","1D","1W"]` 调整为
`["15m","1H","4H","6H","12H","1D","1W"]`（与用户 6H 决策一致）；BB 默认对齐为采集集子集。

### 3.4 热载入（运行中无需重启）

扩展两个周期任务，每轮先读 `get_monitored_coins()` 做**全量对账**（关键：现有 merge 只加不删，
replace 模式必须支持删）：

- `_periodic_candle_collect`：`enabled` 时每轮把 `candle_collector.coin_to_symbol`
  对账为 `get_monitored_coins()`（新增加入、删除移走）；空清单时本轮跳过采集（见 3.6）。
- `_periodic_harmonic_board`：把现有"并入 harmonic_collected"逻辑改为对账 `get_monitored_coins()`
  （`enabled` 时全量替换 `harmonic_monitor.coin_to_symbol`；删除的币移走，`top_n` 同步）。
  BB 监控器同样对账。

CLI/dashboard 写 DB（SQLite WAL，跨进程可见），运行中的监控进程 ≤1 个采集周期内自动生效。
对账逻辑抽成一个纯函数 `reconcile_universe(current, target) -> (added, removed)` 便于单测。

### 3.5 CLI（`cli.py`）

新子命令 `watch`：

- `watch add BTC ETH SOL [--note "理由"]`：解析 coin → symbol（`coin.upper()+"USDT"`，
  或经 `perp_base_coins()` 校验存在），写 DB。
- `watch rm BTC [ETH ...]`：从清单删除。
- `watch list`：表格打印当前清单（coin / symbol / 加入时间 / 备注）。

CLI 与监控进程共享同一 SQLite 文件，靠 3.4 周期对账热生效。`watch` 子命令注册进
`cli.py` 的 argparse 子命令体系（与现有 `run/poll/report/...` 一致）。

### 3.6 Dashboard（`dashboard.py`）

- 新增 `/api/monitored`（GET=list、POST add/rm，body `{action, coins, note}`）。
- 页面加一个小面板：展示当前清单 + 输入框增删（沿用现有无 CDN 单页风格）。
- `handle_harmonic_discover`：把"发现"的币写入 **`monitored_coins`**（替代 `add_harmonic_collected`），
  统一真相源。

### 3.7 边界处理（诚实优先，CLAUDE.md §四-3）

- **清单为空 + enabled=true**：**不静默退回监控全市场**（违背初衷）。采集/监控本轮跳过，
  `log.warning` 提示"监控清单为空，请用 `watch add` 或 dashboard 添加币种"。
- **清单币不在 base_map**（下架/拼错）：采集用 `coin+"USDT"` 猜测符号；抓取失败由现有
  `_fetch_one` 重试/跳过逻辑诚实跳过，**保留在清单**（复牌即恢复），不静默删。
- **enabled 从 false→true 热切换**：mtime 配置热加载已支持；切换后下一个采集周期生效。

## 四、测试（合成数据，确定性；CLAUDE.md §四）

- DB：`add/get/remove/list_monitored_coins` 幂等性、删除返回计数、迁移逻辑（harmonic_collected→monitored_coins）。
- 配置：`MonitoredCoinsCfg` 默认值、`timeframes` 非法周期剔除、`resolve_monitored_universe` 纯函数
  （按成交额排序、symbol 回退）。
- 对账：`reconcile_universe(current, target)` 的 added/removed 正确（增、删、增删混合、空集）。
- 边界：空清单守卫不崩、enabled=false 时旧路径不受影响。
- 回归：全量 `./.venv/bin/python -m pytest -q` 保持全绿（基线 357 passed）。

## 五、零孤儿核对（CLAUDE.md §三-1）

- `MonitoredCoinsCfg` → `config.py` 导出 + `Config` 字段 + `app.py` 消费。
- `monitored_coins` 表方法 → `db.py` 定义 + `app.py`/`cli.py`/`dashboard.py` 消费。
- `resolve_monitored_universe` / `reconcile_universe` → `app.py` 消费 + 单测覆盖。
- `watch` CLI 子命令 → `cli.py` 注册可达。
- `/api/monitored` → dashboard 路由注册 + 前端面板调用。

改完用 grep 自查无孤儿。
