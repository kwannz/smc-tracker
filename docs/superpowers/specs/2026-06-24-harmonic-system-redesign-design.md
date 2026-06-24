# 谐波系统重设计 — 设计 Spec（2026-06-24）

## 目标
把谐波系统升级为「数据驱动、低延迟、配置化、可深挖」的完整子系统，并以此为样板把**低延迟工程与代码简化覆盖全系统**。

用户确认的核心需求（按对话）：
1. **主-详情 HTML**：左 661 币可排序/过滤列表 → 点币 → 右详情。
2. **细致图表**：SVG 蜡烛图(OHLC) + XABCD 全叠加 + PRZ 区带 + Fib 位 + 压力/支撑(S/R) + 进场/止损/目标线；周期 tab 切换(15m/1h/4h/6h/12h/1d/1w)。
3. **多周期压力/支撑显示**（7 周期 S/R）。
4. **历史记录**可查、每币有效形态。
5. **全部 661 Bitget 永续**（TradFi+加密）。
6. **ATR2(SFG)** 集成（动量确认 + ATR 止损）。
7. **数据采集/清洗/计算/低延迟工程**完善，**低延迟覆盖整个系统**。
8. **配置化管理币种**（不硬编码）。
9. **简化代码、完整功能、提高代码质量**（横切）。

## 非目标（YAGNI）
- 不追求方向预测 60-70%（已实证数据不支持，诚实保留 ~50%）。
- 不引入前端框架/CDN（自包含单页内联 SVG）。
- 不做实时 tick 级（周期任务 + DB 缓存 + 前端轮询足够）。

## 架构
```
配置(universe.yaml/config) → 选币(全661 或 过滤集)
  → BitgetCandleCollector(增量轮转, 每轮N币, 清洗校验) → bitget_candles(DB)
  → numpy 计算(从DB批量读, 低延迟): 谐波/BB/ATR2
      → harmonic_setups(+XABCD点+历史) / bb_levels(7周期S/R) (DB)
  → dashboard(独立进程, 只读DB):
      /harmonic                  主-详情页(自包含SPA)
      /api/harmonic/list         币列表(排序/过滤元数据)
      /api/harmonic/coin/<coin>?tf=  详情(OHLC+setup+XABCD+S/R+历史)
  → 前端: 左列表 → 点币 → 右 SVG蜡烛+全叠加 + 多周期S/R + setup + 历史
```

## 组件与接口（每个单一职责、可独立测试）

### A. 配置化币种管理 `config`
- 新 `UniverseCfg`：`mode: "all" | "top_n" | "list"`，`top_n: int`，`include: list[str]`，`exclude: list[str]`，`asset_filter: "all"|"crypto"|"tradfi"`。
- 选币函数 `resolve_universe(perp_base_coins, tickers, cfg) -> dict[coin,symbol]`（成交额序；all=全661；可 include/exclude/类别过滤）。纯函数可测。

### B. 采集层 `monitor/candle_collector.py`（增量轮转）
- `collect_batch(offset, batch_size)`：轮转采 batch_size 币 × tfs，offset 滚动覆盖全集；落 `bitget_candles`。
- **清洗**：`_clean_rows`——`to_float` 拒 NaN/inf、ts 严格递增去重、价格>0、缺口标记；脏行计数日志。
- 低延迟：共享 session、Semaphore 限流、429 退避（已有）。

### C. DB 层 `storage/db.py`
- `bitget_candles`（已有）+ 清洗保证。
- `harmonic_setups` **加列**：x_idx,x_px,a_idx,a_px,b_idx,b_px,c_idx,c_px,d_idx,d_px（XABCD 供图表）；**改保留历史**（带 ts 不再 DELETE-snapshot；`recent_harmonic_setups()` 取最新快照 = max(ts) 批；`harmonic_history(coin, limit)` 取该币历史形态）。
- 新 `bb_levels(coin,tf,ts,upper,mid,lower,pct_b,squeeze)` + `recent_bb_levels(coin)`（7 周期 S/R）。
- 保留策略：harmonic_setups/bb_levels 加入 `_DB_RETAIN`（如 7 天）。

### D. 计算层（已有，增强）
- 谐波 `analyze_candles` 输出含 points（已有）；`to_records` 带 XABCD 点 + Fib 关键位。
- BB `analyze_tf` 输出 upper/lower/pct_b/squeeze（已有）→ 落 bb_levels。
- ATR2 `indicators/atr2_signals.py`（归一化动量 confirmation + ATR）→ trade_setup 用作方向确认 + ATR 止损（atr_stop/atr2_bias 字段）。

### E. 后端 API `dashboard.py`
- `build_harmonic_list(store) -> list[{coin,asset_class,best_conf,direction,has_pattern,n_setups}]`（左列表，按 conf 排序，前端可再排/过滤）。
- `build_coin_detail(store, coin, tf) -> {coin,asset_class,tf,candles:[OHLC...],setups:[含XABCD/PRZ/Fib/entry/stop/target/ATR2],sr:[7周期],history:[...]}`。
- 路由：`/api/harmonic/list`、`/api/harmonic/coin/<coin>`、`/harmonic`(新页)。防御查询(表缺/空→[])。

### F. 前端（自包含单页，内联 SVG，无 CDN）
- 左：可排序(置信/类别/方向)、可过滤(🏦TradFi/₿加密、有形态、周期)的币列表；5s 轮询 /api/harmonic/list。
- 右详情(点币拉 /api/harmonic/coin/<coin>)：
  - **SVG 蜡烛图**：OHLC 矩形+影线，价格/时间轴；叠加 XABCD 连线+点标注、PRZ 半透明区带、Fib 水平线、S/R(BB上下轨)线、进场/止损/目标线。周期 tab 切换。
  - 多周期 S/R 表(7 周期压力/支撑/挤压)。
  - setup 明细(进场/止损/目标/盈亏比/仓位/置信/KNN/订单流/ATR2)。
  - 历史有效形态列表(该币)。
  - 傻瓜解释 + 诚实声明(确认层非投资建议/墙可能spoof/KNN≈随机)。

## 横切：低延迟覆盖全系统 + 简化 + 质量
- **低延迟**：热路径只读 DB 预计算结果，无 live 网络在请求/渲染路径；numpy 向量化；批量 executemany；前端按需拉详情(非全量)。对全系统周期任务复核「是否阻塞、是否可缓存」。
- **简化/质量**：本轮在改的文件顺手简化(消重复、缩臃肿函数、统一 util)；不做无关重构。所有新代码 TDD + 类型注解 + 中文注释。保持 pytest 全绿基线(当前 1076)。

## 数据流（点币详情）
前端点 BTC → GET /api/harmonic/coin/BTC?tf=4H → 后端读 bitget_candles(BTC,4H,~200根) + harmonic_setups(BTC,4H 最新) + bb_levels(BTC,7周期) + harmonic_history(BTC) → JSON → 前端 SVG 绘制。

## 测试
- 每组件 TDD 合成数据：resolve_universe、清洗、新 DB 列/历史/bb_levels 往返、build_coin_detail、ATR2、前端 render(关键字+无CDN+SVG元素)。
- 真实数据冒烟：采集落库、详情 API 真实 OHLC、页面渲染。
- 全量 pytest 全绿。

## 实现分解（workflow 阶段，写集隔离）
1. **配置+选币**（config.py + 选币函数 + 测试）
2. **DB schema**（db.py: XABCD 列/历史/bb_levels + 测试）
3. **采集清洗+BB落库+to_records XABCD**（candle_collector + bb_monitor + harmonic_monitor + 测试）
4. **ATR2**（atr2_signals + trade_setup + 测试）
5. **后端 API**（dashboard.py: list/detail build + 路由 + 测试）
6. **前端**（dashboard.py: 主-详情 SPA + SVG 蜡烛叠加 + 测试）
7. **app.py 接线 + 661**（采集器轮转/选币/落库/gather + 测试）
8. **集成验证 + 部署**（全量 pytest + 真实冒烟 + 双服务部署）

依赖：6←5←(2,3,4)；7←(1,2,3)。同文件(app.py/dashboard.py/db.py)阶段串行或 worktree 隔离。

## 质量与验证要求（用户#强制）
- **opus 规划 / sonnet 执行**：本会话(Opus)规划+审核+集成；实现 agent 用 sonnet 模型。
- **找隐性问题**：专门验证阶段——对谐波/BB/ATR2 计算做对抗性审查，找边界/数值/几何隐性 bug（如此前发现的 look-ahead、Cypher 误标、k 币单位、setup 错配类）。
- **计算函数数值精确性**：关键数值函数交叉验证——
  - 谐波比率/Fib：与 Scott Carney 标准值 + djoffrey/pyharmonics 对齐；
  - BB：与 **TA-Lib BBANDS** 浮点级平价(已有 test_talib_parity 模式)；
  - ATR2：与 SFG Pine/Rust 参考定义对齐；ATR 与 TA-Lib ATR 平价；
  - 滚动 std/SMA：与 numpy 基准一致。importorskip 零硬依赖。
- **测试覆盖率目标 100%**：用 `pytest --cov` 度量，新代码逼近 100% 行/分支覆盖；补边际功能(空/单点/NaN/inf/极端比率/数据不足/并发)用例。诚实标注无法覆盖的(如 talib 回退分支 pragma)。
- **基线**：全程 pytest 全绿(当前 1076)，py_compile 干净，真实数据冒烟。
- **实现工具**：用 **workflow** 编排各阶段(用户#)，Opus 阶段间审核+集成验证(不盲信 agent 自报)。
