# 谐波系统 MVP 收口 + 强化 — 设计 spec

> 状态：用户已批准（2026-06-25）。本文件是 sonnet 执行 agent 的**唯一事实源**。
> 执行模型：Opus 规划（本 spec + workflow），Sonnet 执行（workflow 内各 agent）。
> 强制规范见仓库根 `CLAUDE.md`；谐波 MVP 锚点见 `HARMONIC_MVP.md`。

## 0. 全局铁律（每个 agent 必须遵守）

- **venv / 测试命令**：`PYTHONPATH=src ./.venv/bin/python -m pytest -q`（Python 3.14.3）。
  基线 **2096 tests collected，全绿**——只增不减，绝不允许使已通过用例转红。
- **编译检查**：改完文件先 `./.venv/bin/python -m py_compile <file>`。
- **零孤儿**：新模块同迭代接入运行时 + 从所在目录 `__init__.py` 导出。
- **不重写 `harmonic.py` 几何**：新形态走**新文件** `indicators/harmonic_ext.py`，import 复用既有 helper。
- **诚实标注**：置信封顶不变（completed≤0.90 / forming≤0.85）；不恢复任何"Fib 加分虚高"；
  新形态/高灵敏附实测警示。KNN≈随机基线照旧。
- **风格**：中文注释 + 英文标识符 + 类型注解；slots dataclass；与现有代码一致。
- **不 commit**：agent 只写文件**不执行 git commit**（并行会撞 index 锁），由 Opus 收口统一提交。
- **文件归属**：同一文件只允许其归属 agent 写（见 §6 表），严禁越界改他人文件。

---

## 1. A — MVP 100% 收口（接线 + 修孤儿）

### A1 修孤儿 / 文档对齐（归属：Parity agent）
- `src/smc_tracker/indicators/__init__.py` 导出 `HarmonicState`（当前缺失，与 HARMONIC_MVP.md S2 声称矛盾）。
- 新建 `tests/test_harmonic_state_parity.py`：对多组合成 K 线序列断言
  1. 逐根 `HarmonicState.update()` 后 `snapshot()` == `analyze_candles(candles, order, tol)` **逐字段完全相等**；
  2. **每个前缀 k** 都相等（`analyze_candles(candles[:k])` == 喂前 k 根后的 snapshot）；
  3. no-repaint：前缀 pivots 是更长序列 pivots 的前缀（不回改）。
  - order/tol 用**与运行时一致的新默认值**（见 C：order=2, tol=0.07）。

### A2 candle_ingest 接线 / S3 收尾（归属：Wire agent）
- `monitor/candle_ingest.py` 的 `detect_and_fill_gap` / `backfill` / `ingest_ws_closed_bar` 已建好+导出+有测，
  但**无运行时调用点**。在 `app.py` 冷启动/周期采集补缺口处调用 `detect_and_fill_gap`；
  WS 收盘 bar 落库统一走 `ingest_ws_closed_bar`（替代散落的 `upsert_candles` 直调）。
- 不改 `candle_ingest.py` 签名/行为；只在 app/monitor 增加调用点。

### A3 HarmonicState 接线 / S6+S7（归属：Wire agent，**依赖 A1 parity 绿 + B 形态接入完成**）
- `monitor/harmonic_monitor.py`：维护 `dict[(coin,tf)] -> HarmonicState`，refresh 时**增量喂新收盘 bar**
  （只喂自上次以来的新 bar），替代每轮全量 `analyze_candles`。结果结构必须与全量逐字段一致（parity 守护）。
- `monitor/harmonic_candle_ws.py`：收盘 bar 调对应 `(coin,tf)` 的 `HarmonicState.update()`。
- **正确性优先**：若增量与全量出现任何不等，回退该 (coin,tf) 全量重算并 log.warning，绝不静默漂移。

### A4 tf 自适应窗口 / S4（归属：Cfg agent）
- `HarmonicCfg` 增 per-tf bars 自适应（小周期多根、大周期少根），配置化、向后兼容（默认行为不变）。

### A5 收口 / S8+S9（归属：Verify 阶段 / Opus）
- 全量 pytest 绿；`scripts/verify_harmonic_e2e.py` 复跑真实数据；`HARMONIC_MVP.md` 全项勾 `- [x]` + 迭代日志追加。

---

## 2. B — 形态多样性：Cypher + Shark + AB=CD（归属：Ext agent + Merge agent）

新建 `src/smc_tracker/indicators/harmonic_ext.py`，每形态独立检测函数 +（可选）前瞻投射函数。
**返回 dict 契约必须与 `harmonic.detect_xabcd` 完全一致**：
`{pattern, direction, points{X..D or A..D}, prz:(lo,hi), completed:bool, confidence, confluence}`
（这样 trade_setup / db / dashboard 零改动即可消费）。置信封顶沿用（completed≤0.90, forming≤0.85）。

### 比率（pyharmonics / djoffrey/HarmonicPatterns / Carney 交叉校验；agent 必须实证不可臆测）
- **Cypher**（XC-anchored，bull 点序 X(L)A(H)B(L)C(H,**C>A**)D(L)）：
  - B = 0.382–0.618 retrace of XA
  - C = 1.272–1.414 extension of XA（C 超过 A，故现有 `X<B<C<A` 结构校验会拒，需独立写）
  - D = 0.786 retrace of XC（从 X 到 C 测量）
- **Shark**（5-0 / OXAB 标定，bull）：
  - 用 pyharmonics 公开 Shark 定义为准（B 沿 AB 超过 X：B=1.13–1.618 XA；C=1.618–2.24 AB；C=0.886–1.13 OX）。
  - **agent 必须先读 pyharmonics 源/文档实证精确比率**，不确定的写进测试用合成几何验证。
- **AB=CD**（4 点，bull 点序 A(H)B(L)C(H)D(L)）：
  - BC = 0.382–0.886 retrace of AB
  - CD = 1.272–1.618 extension of BC，且 CD ≈ AB（对称等长，±tol）
  - D 投射：bull `D = C - |A-B|`（CD=AB 等长），PRZ 以 D 为中心 ±tol。

### 验证（TA-Lib 无谐波 → 不做 TA-Lib parity）
- `tests/test_harmonic_ext.py`：**合成几何构造测试**——按上述比率构造一个完美 Cypher/Shark/ABCD 的枢轴序列，
  断言对应函数能检出且方向/点位正确；构造一个比率偏离的序列断言不检出（防过检测）。
- 各形态附实测胜率未知警示位（诚实，照 Crab 先例）。

### 接入（归属：Merge agent，**依赖 Ext 完成**）
- `harmonic.py::analyze_candles`：新增**一处** merge——调用 `harmonic_ext` 的检测函数，把结果并入 completed/forming
  （同 D_idx 去重保留最高 confidence，与现有逻辑一致）。抽出共享 merge helper。
- `harmonic_state.py::_compute`：调用**同一** merge helper，保证增量路径与全量路径并入逻辑完全一致（parity 不破）。
- 接入后 `tests/test_harmonic_state_parity.py` 必须仍绿。

---

## 3. C — 灵敏度提升（归属：Cfg agent + Draw agent 标注）

- `config.py::HarmonicCfg`：`order 3→2`、`tol 0.05→0.07`。同步确保 `analyze_candles` / `HarmonicState` /
  `MarketStructure(lookback=order)` 全用此值，parity 不破。
- **诚实对冲（Draw agent）**：dashboard 谐波页头 + 形态卡标注
  「⚡高灵敏模式 (order=2 / tol=7%)：含更多早期形态，误检率上升，止损必执行」。
- 几何质量门槛（≥3 腿含 D 约束、结构次序校验、置信封顶）**不动**。

---

## 4. D — 斐波那契入场强化（归属：Fib agent）

- `indicators/fibonacci.py` 增：
  - `golden_pocket_zone(high, low, direction) -> tuple[lo,hi]`（0.618–0.786 段，复用 fib_levels）。
  - `intersect_zone(a_lo,a_hi, b_lo,b_hi) -> tuple[lo,hi] | None`（无交集返回 None）。
- `signals/trade_setup.py::build_setups`：
  - **入场精炼**：completed 用 XA 段黄金口袋，与 (D±1.5%) 求交集；forming 用黄金口袋与 PRZ 求交集。
    有交集→**入场区收窄到交集**（最高概率位）；无交集→回退原区 + `fib_note` 标「无Fib汇合，用形态区」（不强凑）。
  - **Fib 扩展目标**：target1/target2 用 AD 段 1.272 / 1.618 扩展位，与现有 RR 目标**取更保守者**；`fib_note` 标来源。
  - **confidence 不动**（仍不加分，维持 🟡-2 诚实化）。
  - 新增字段（如 `entry_src` / 扩展目标）须向后兼容，不破坏现有 `tests/test_trade_setup.py`。

---

## 5. E — 形态绘制可视化（completed + forming 都画，归属：Draw agent）

- 数据源：`harmonic_setups` 的 XABCD 坐标（x_idx..d_px）+ `bitget_candles` 价格序列。
  - db.py 加只读方法（如 `candles_for_draw(coin, tf, limit)`）取价格序列供绘制（只增不改现有方法）。
- `dashboard.py` 新增手绘 **inline SVG**（无 CDN，复用现有 `svgEsc` 及 SVG 风格）：
  - **completed**：X-A-B-C-D 五段**实线** + 枢轴标签(X/A/B/C/D+价) + PRZ 阴影带 + 黄金口袋∩PRZ 入场高亮 + Fib 目标线；bull 绿 / bear 红。
  - **forming**：X-A-B-C 实线 + C→**预期 D 虚线** + PRZ 投射阴影（虚线/半透明区分"未完成"）。
- 接入 `/api/harmonic/coin/{coin}` 详情页渲染；新增渲染配套测试（合成 setup → 断言 SVG 含关键元素，不脆断字符串）。

---

## 6. 文件归属表（workflow 并行·文件零冲突）

| Agent | 拥有文件（只此 agent 写） | 阶段 |
|-------|--------------------------|------|
| **Ext** | `indicators/harmonic_ext.py`, `tests/test_harmonic_ext.py` | P1 |
| **Parity** | `indicators/__init__.py`, `tests/test_harmonic_state_parity.py` | P1 |
| **Fib** | `indicators/fibonacci.py`, `signals/trade_setup.py`, `tests/test_trade_setup.py`(增量) | P1 |
| **Cfg** | `config.py` | P1 |
| **DbRead** | `storage/db.py`(只增只读取数方法) | P1 |
| **Merge** | `indicators/harmonic.py`, `indicators/harmonic_state.py` | P2（依赖 Ext+Parity） |
| **Wire** | `monitor/harmonic_monitor.py`, `monitor/harmonic_candle_ws.py`, `app.py` | P3（依赖 Merge+parity 绿） |
| **Draw** | `dashboard.py`, `tests/test_dashboard*.py`(增量) | P4（依赖 Fib+DbRead+落库结构稳定） |
| **Verify/Opus** | `HARMONIC_MVP.md`, 全量 pytest, E2E | P5 |

依赖链（硬序）：P1（并行）→ P2 Merge（parity 绿）→ P3 Wire（增量==全量）→ P4 Draw → P5 收口验证 + 修复循环。

## 7. 验收（P5）
1. `PYTHONPATH=src ./.venv/bin/python -m pytest -q` 全绿（≥2096 + 新增）。
2. `tests/test_harmonic_state_parity.py` 绿（增量==全量护栏）。
3. `tests/test_harmonic_ext.py` 绿（3 新形态合成几何）。
4. `scripts/verify_harmonic_e2e.py` 真实数据复跑成功（诚实报告检出形态，不编造）。
5. `HARMONIC_MVP.md` 全项 `- [x]` + 迭代日志。
6. grep 自查无孤儿（HarmonicState/harmonic_ext/candle_ingest 均有运行时调用点）。
