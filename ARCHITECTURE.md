# 架构总览 — SMC 双所聪明钱追踪系统

> 全栈架构梳理（只读分析文档）。本系统在 **Hyperliquid（链上地址级）** 与 **Bitget（CEX 市场级）** 双数据源之上，
> 融合 SMC 市场结构、聪明钱（庄）地址流向、链上大额转账、技术指标 / ML，落地 SQLite 并多渠道推送信号。
>
> 入口：`src/smc_tracker/app.py`（流式实时）与 `scripts/poll_monitor.py` → `src/smc_tracker/monitor/poll_monitor.py`（轮询）。

---

## 一、分层架构图

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  数据接入层 (Data Ingest)                                                        │
│  ┌── System 1: Hyperliquid ──────────┐   ┌── System 2: Bitget ───────────────┐  │
│  │  hyperliquid/ws_client.py  (WS)    │   │  bitget/ws_client.py   (WS)        │  │
│  │  hyperliquid/info_client.py (REST) │   │  bitget/rest.py        (REST)      │  │
│  │  hyperliquid/constants.py          │   │                                    │  │
│  └────────────────────────────────────┘   └────────────────────────────────────┘  │
│  ┌── 链上 (Onchain, 零鉴权公开 RPC) ──────────────────────────────────────────┐  │
│  │  onchain/evm.py (ETH/BSC/BASE Transfer)   onchain/solana.py (SOL 供应)      │  │
│  └────────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │ Trade / Fill / Candle / OI / Transfer (models.py)
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  监控层 (Monitor)                                                                │
│  address_monitor   meme_trade_monitor   bitget_oi_monitor   onchain/monitor     │
│  whale_discovery   whale_momentum   address_analyzer   address_correlation      │
│  → 产出：聪明钱事件 / meme 净流向 / OI 异动 / 链上巨鲸转账 / 庄持仓快照            │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  SMC 结构层 (smc/)                指标 / ML 层 (indicators/)                      │
│  structure  BOS/CHoCH            technical(10指标)  combo  price_action          │
│  zones      FVG/OB/溢价折价       fibonacci  levels  patterns  volume  sessions  │
│  liquidity  扫荡/等高等低         knn(ML 相似态预测)  engine(汇总)                │
│  feed       WS→结构                                                              │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  信号层 (signals/)                                                               │
│  engine(SMC共振打分)  divergence(CEX⟂DEX背离)  consensus(多庄共识)               │
│  confluence(多源叠加)  position_tracker(换仓)  flow_predictor(前瞻资金流)         │
│  pump_radar(暴涨暴跌)  ta_signal(技术多因子)  risk(入场/止损/目标/RR)             │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  存储层 (storage/db.py + onchain 自管表)  — 本地 SQLite (WAL, synchronous=NORMAL) │
│  16 张表：原始成交/OI/事件 → 信号/背离/共识/共振 → 地址画像/PnL/链上转账           │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  通知层 (notify/)  multi(聚合) → webhook(Discord/Slack/通用) + telegram          │
│                    report(摘要日报，从 SQLite 聚合)                               │
└──────────────────────────────────────────────────────────────────────────────┘
```

数据流向：**接入层 → 监控层 → (SMC 结构 / 指标 ML / 链上) → 信号层 → SQLite → 通知层**。
SMC 结构、指标、链上三层并行地为信号层提供「环境」（结构偏向、区域共振、链上加成），由信号层融合打分。

---

## 二、逐模块文件清单 + 一句话职责

### 顶层 `src/smc_tracker/`
| 文件 | 职责 |
|---|---|
| `app.py` | 流式实时入口：System 1 + System 2 并发编排器（`TradingSystem`），全程落 SQLite。 |
| `config.py` | 配置加载：从 YAML 读取并提供带默认值的强类型访问。**[禁改]** |
| `models.py` | 核心数据模型（slots dataclass：Trade/Fill/Position/Candle/Side 等，低延迟）。 |
| `memecoins.py` | Meme 币定义与跨交易所符号归一化（`normalize`）。 |
| `util.py` | 共享工具：安全数值解析 + 时间格式（消除跨模块重复）。 |

### 数据接入 — Hyperliquid `hyperliquid/`
| 文件 | 职责 |
|---|---|
| `ws_client.py` | 异步 WS 客户端（重连 / 心跳 / 断线重订阅，低延迟）。**[禁改]** |
| `info_client.py` | REST Info 客户端（POST /info：持仓 / 成交 / K 线 / allMids / l2Book）。**[禁改]** |
| `constants.py` | Hyperliquid API 常量。**[禁改]** |

### 数据接入 — Bitget `bitget/`
| 文件 | 职责 |
|---|---|
| `ws_client.py` | Bitget V2 公共 WebSocket 客户端（低延迟，OI/ticker 订阅）。**[禁改]** |
| `rest.py` | Bitget V2 REST 客户端（USDT-M 永续 + 币种链上合约地址 + tickers）。**[禁改]** |

### 监控层 `monitor/`
| 文件 | 职责 |
|---|---|
| `events.py` | 聪明钱事件模型（`EventType` 开/加/减/平/反手 + `SmartMoneyEvent`）。**[禁改]** |
| `address_monitor.py` | 实时监控 watchlist 聪明钱地址：成交分类（开/加/减/平/反手）+ 净流向。**[禁改]** |
| `meme_trade_monitor.py` | System 1 meme 永续成交地址监控（含买卖双方地址）→ 地址级净主动流向。**[禁改]** |
| `bitget_oi_monitor.py` | System 2 meme OI 实时流监控 + OI 异动检测。**[禁改]** |
| `poll_monitor.py` | 轮询入口逻辑（纯 REST，cron 友好）：发现庄→拉持仓→快照 diff→共识/背离/动量。**[禁改]** |
| `whale_discovery.py` | 聪明钱（庄）地址自动发现（从排行榜），抓庄前提。**[禁改]** |
| `whale_momentum.py` | 庄 PnL 动量追踪：谁现在最火 / 正在变热或变冷。**[禁改]** |
| `address_analyzer.py` | 聪明钱地址深度分析 / 画像（落 address_profiles）。**[禁改]** |
| `address_correlation.py` | 地址关联性分析：发现协同行动的地址群（疑似庄家集团多钱包）。**[禁改]** |

### SMC 结构层 `smc/`
| 文件 | 职责 |
|---|---|
| `structure.py` | 市场结构引擎：增量识别摆动高低点 + BOS + CHoCH。**[禁改]** |
| `zones.py` | 区域识别：FVG（公允价值缺口）+ Order Block + 回补检测 + 溢价折价区。**[禁改]** |
| `liquidity.py` | 流动性：等高/等低 + buy/sell-side liquidity + 流动性扫荡（stop hunt）。**[禁改]** |
| `feed.py` | 实时 K 线接入：把 HL candle WS 推送喂给 MarketStructure。**[禁改]** |

### 指标 / ML 层 `indicators/`
| 文件 | 职责 |
|---|---|
| `technical.py` | 10 个核心技术指标（numpy，低延迟）。 |
| `combo.py` | 4 个 combo 复合指标：融合基础指标成趋势/动量/波动/反转判断。 |
| `price_action.py` | 价格行为：K 线形态识别 + PA 特征（供 KNN 用）。 |
| `fibonacci.py` | 斐波那契回撤/扩展（含黄金口袋 OTE）。 |
| `levels.py` | 支撑/压力位：枢轴点 + 摆动点聚类成 S/R 区。 |
| `patterns.py` | 经典图表形态：双顶/双底 + 道氏理论趋势。 |
| `volume.py` | 每币种成交量监控（numpy，低延迟）。 |
| `sessions.py` | 时间策略：交易时段 + SMC killzone（基于 UTC 小时）。 |
| `knn.py` | KNN 预测器（ML）：用技术+PA 特征向量找历史 K 个最相似态预测走向。 |
| `engine.py` | 统一技术分析：一段 K 线上算齐 指标/combo/PA/斐波/S-R/KNN/时段，汇总成一张图。 |

### 信号层 `signals/`
| 文件 | 职责 |
|---|---|
| `engine.py` | 共振信号引擎（可解释打分）：融合 SMC 结构偏向 + 聪明钱流向 + OI + 区域 + 扫荡 + 链上。 |
| `divergence.py` | 三源背离信号：CEX 散户拥挤方向（资金费）⟂ DEX 聪明钱流向。 |
| `consensus.py` | 多庄共识信号：多个庄同时同向押注同一 coin = 强信号（含 `positioning` 面板）。 |
| `confluence.py` | 多信号叠加共振：多个独立信号源同 coin 同向 = 超级信号。 |
| `position_tracker.py` | 庄换仓预警：持仓快照 diff 检测平仓/反手/大幅减仓。 |
| `flow_predictor.py` | 前瞻资金流预测：净流向加速度 + 订单簿挂单意图 + OI 速度（含 `orderbook_imbalance`）。 |
| `pump_radar.py` | 暴涨暴跌实时预警：把历史回测出的高 lift 规则操作化。 |
| `ta_signal.py` | TA 复合信号：纯技术分析多因子共振。 |
| `risk.py` | 信号风险参数：基于 SMC 结构计算入场/止损/目标/盈亏比（RR）。 |

### 链上层 `onchain/`
| 文件 | 职责 |
|---|---|
| `evm.py` | EVM Transfer 监控（纯公开 RPC，零鉴权，不依赖 web3；ETH/BSC/BASE）。 |
| `monitor.py` | 链上 meme 巨鲸转账编排（多链增量轮询 + 落库；**自管 onchain_transfers 表**）。 |
| `solana.py` | Solana 供应量监控（mint/burn，无 API key；**自管 sol_supply 表**）。 |

### 存储层 `storage/`
| 文件 | 职责 |
|---|---|
| `db.py` | SQLite 存储（WAL + synchronous=NORMAL，热路径批量 executemany；定义 14 张表 SCHEMA + 全部读写 API）。**[禁改]** |

### 通知层 `notify/`
| 文件 | 职责 |
|---|---|
| `multi.py` | 多渠道推送聚合：同一条消息推到所有已配置渠道。**[禁改]** |
| `webhook.py` | Webhook 推送（无 API key，兼容 Discord/Slack/通用）。**[禁改]** |
| `telegram.py` | Telegram 推送（Bot API，HTTP，无额外库）。**[禁改]** |
| `report.py` | 摘要日报：从 SQLite 聚合近窗信号/背离/净流向/链上活动生成文本。**[禁改]** |

### 回测 `backtest/`
| 文件 | 职责 |
|---|---|
| `engine.py` | SMC 结构信号回测引擎（事件驱动、逐根重放）。 |

---

## 三、数据流（从 WS/REST 进来 → 各 monitor → signals → SQLite → 通知）

以**流式 `app.py`** 为主线：

```
[WS] HL candle ──────────────► StructureFeed.on_candle_ws
                                  └► MarketStructure 增量 → BOS/CHoCH (StructureEvent)
                                       └► _on_structure 回调：
                                            刷新 meme 净流向 / OI 变化 / 区域共振 / 扫荡确认 / 风险位
                                            └► SignalEngine.on_structure → Signal ─► signals 表 ─► 推送

[WS] HL meme 成交 ─► MemeTradeMonitor ─► hl_meme_trades 表(批量flush)
                       ├► coin_net() 净流向 → 喂 SignalEngine
                       └► 可疑激进建仓 → _on_suspicious → flagged_addresses + 升级全量追踪

[WS] HL watchlist 成交 ─► AddressMonitor ─► 开/加/减/平/反手 (SmartMoneyEvent)
                            └► _on_sm_event ─► sm_events 表
                                 └► 3min 窗口累积净建仓越阈 → whale_signals 表 ─► 跟庄推送

[WS] HL allMids ─► _on_all_mids ─► 全市场中间价(共识估值用)

[WS] Bitget OI/funding ─► BitgetOIMonitor ─► bitget_oi 表(批量flush) + OI 异动告警

[REST 周期] onchain.poll_once(EVM) ─► onchain_transfers 表 ─► set_onchain 喂 SignalEngine(信心加成)
[REST 周期] sol_monitor.poll_once ─► sol_supply 表 ─► 增发/销毁告警

[周期任务，读已落库数据二次聚合] →
   _periodic_consensus  : 庄持仓 + allMids → WhaleConsensus(consensus表)
                          + WhalePositionTracker(position_changes表) + Confluence(confluence_signals表)
   _periodic_divergence : Bitget 资金费/OI ⟂ DEX 净流向 → DivergenceDetector(divergence表)
   _periodic_correlation: 近30min meme 成交 → 协同地址群(疑似庄家集团)推送
   _periodic_flow_predict: 净流向加速度 + 订单簿 + OI 速度 → FlowPredictor 前瞻预测推送
   _periodic_report     : 从 SQLite 聚合 → build_report 摘要日报 → 推送
   _periodic_flush      : 周期 flush meme/OI 批量缓冲
```

所有信号统一经 `_push()` 走 `MultiNotifier` → webhook + Telegram。
启动前 `_seed()` 用 REST 播种 watchlist 持仓、SMC 历史 K 线、Bitget 符号映射与 meme 合约地址。

---

## 四、SQLite 全部表清单 + 用途

**16 张表**：14 张在 `storage/db.py` 的 `SCHEMA` 集中定义；另 2 张由 onchain 模块「自管」（`CREATE TABLE IF NOT EXISTS`，刻意不写进 db.py 以避免改动冲突）。

### A. `storage/db.py` SCHEMA（14 张）
| # | 表 | 用途 |
|---|---|---|
| 1 | `meme_contracts` | 各 meme 的链上合约地址（coin → chain → contract），主键 (coin,chain)。 |
| 2 | `bitget_oi` | Bitget USDT-M 永续 OI/资金费/标记价 时间序列，主键 (symbol,ts)。 |
| 3 | `hl_meme_trades` | Hyperliquid meme 成交（含买卖双方地址 + taker 主动方），供地址轨迹/净流向。 |
| 4 | `sm_events` | 聪明钱地址事件（开/加/减/平/反手，含 pos_before/after、closed_pnl）。 |
| 5 | `signals` | SMC 共振信号（带符号分 + 结构/流向偏向 + 入场/止损/目标/RR；迁移补 status/exit_*/realized_r）。 |
| 6 | `divergence` | 三源背离信号（CEX 资金费/OI ⟂ DEX 聪明钱净流向，bullish 吸筹 / bearish 分销）。 |
| 7 | `whale_signals` | 跟庄信号（窗口累积净建仓越阈：OPEN/ADD/FLIP × long/short）。 |
| 8 | `whale_positions` | 庄持仓快照（轮询模式跨运行接力，主键 (address,coin)，覆盖式写）。 |
| 9 | `position_changes` | 庄换仓事件（exit 平仓 / reversal 反手 / reduce 减仓）。 |
| 10 | `consensus` | 多庄共识（n_agree/n_oppose + 净名义 + 评分 + labels）。 |
| 11 | `confluence_signals` | 多信号叠加共振（n_sources + sources + opposing + 评分）。 |
| 12 | `flagged_addresses` | 可疑地址标记（首见/末见 + 触发 coin/原因/净建仓，promoted=是否升级全量追踪）。 |
| 13 | `address_profiles` | 聪明钱地址画像（综合评分/账户净值/PnL/胜率/偏好币，主键 address）。 |
| 14 | `whale_pnl_snapshots` | 庄 PnL 动量快照（day/week/month/alltime PnL + 账户净值，主键 (address,ts)）。 |

### B. onchain 模块自管（2 张）
| # | 表 | 定义位置 | 用途 |
|---|---|---|---|
| 15 | `onchain_transfers` | `onchain/monitor.py` | EVM 链上大额 meme Transfer（coin/chain/合约/from/to/amount/usd/block/tx_hash）。 |
| 16 | `sol_supply` | `onchain/solana.py` | Solana meme 供应量快照（mint/supply/decimals，主键 (mint,ts)），用于 mint/burn 检测。 |

> 说明：本次梳理在源码中实测到 **16** 张 `CREATE TABLE`（任务简报标称「17 张」，以源码为准记为 16；运行态 `data/smc.db` 当前物化 13 张，其余表由对应模块首次运行时惰性建表）。索引另有 `bitget_oi`、`hl_meme_trades`、`sm_events`、`signals`、`divergence`、`whale_signals`、`position_changes`、`consensus`、`confluence_signals`、`whale_pnl_snapshots` 及 onchain/sol 表的二级索引。

---

## 五、两种运行模式 + 入口

### 模式 1：流式实时 `app`（常驻进程，逐事件实时推送）
- **入口**：`PYTHONPATH=src ./.venv/bin/python -m smc_tracker.app --config config/config.yaml`
- **核心**：`src/smc_tracker/app.py` 的 `TradingSystem`，`asyncio.gather` 并发跑 HL WS + Bitget WS + 多个周期任务。
- **特点**：双 WS 长连接（自动重连/心跳/重订阅）+ 链上/共识/背离/前瞻/日报等周期协程；逐事件落库 + 逐事件推送；端到端新鲜成交 ~230ms。
- **适用**：实时盯盘、即时跟庄/SMC 突破/暴涨暴跌预警。

### 模式 2：轮询 `poll_monitor`（cron 友好，纯 REST 无 WS）
- **入口**：
  - 单次：`./.venv/bin/python scripts/poll_monitor.py`
  - 持续：`./.venv/bin/python scripts/poll_monitor.py --loop --interval 3600`
- **核心**：`scripts/poll_monitor.py`（CLI wrapper）→ `src/smc_tracker/monitor/poll_monitor.py` 的 `PollMonitor.run_once`。
- **每轮**：发现庄 → REST 拉所有庄当前持仓 → 与上次 SQLite 快照 diff（平仓/反手/减仓）→ 多庄共识 + 三源背离 + 多信号共振 + 持仓面板 + 庄 PnL 动量 → 落库 + 摘要推送 + 保存本轮快照。
- **特点**：状态存 SQLite 跨运行接力，无需常驻；庄是持仓型，小时级快照 diff 正好捕获「庄动作」。
- **适用**：低成本定时监控、无常驻环境（cron / serverless）。

> 注：根目录 `README.md` 仍写 `smc_tracker.main`，为历史残留；当前真实流式入口是 `smc_tracker.app`。

---

## 六、规模统计

| 维度 | 数值 | 说明 |
|---|---|---|
| SQLite 表 | **16** | 14 张集中于 `storage/db.py` SCHEMA + `onchain_transfers` + `sol_supply`（任务标称 17，以源码实测为准）。 |
| 源文件 | **~66** | `src/smc_tracker/` 下 62 个 `.py`（含 11 个 `__init__.py`，51 个实现文件）+ `scripts/` 24 个脚本入口；核心实现约 66 量级。 |
| 测试 | **196 passed** | `tests/` 下 32 个测试文件，`pytest -q` 全绿（基线）。 |
| 数据源 | 2 所 + 链上 | Hyperliquid（地址级）+ Bitget（市场级）+ EVM/Solana 公开 RPC（零鉴权）。 |
| 运行模式 | 2 种 | 流式 `app`（WS 常驻）+ 轮询 `poll_monitor`（REST，cron 友好）。 |

---

*本文档为只读架构梳理，不构成投资建议；链上跟单存在滑点、抢跑、地址误判等风险。*
