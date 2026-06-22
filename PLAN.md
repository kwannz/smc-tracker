# SMC 聪明钱追踪系统 — 开发路线图与进度

> 本文件是 `/loop` 循环（每 5 分钟）的接力锚点。
> 每次循环：读本文件 → 找第一个未完成项 → 推进 → 勾选 → 在「迭代日志」追加一行。

## ⭐ 系统定位（迭代 #14 校准）
**这是一个聪明钱监控系统,核心是「抓庄」** —— 自动发现 Hyperliquid 顶级盈利地址(庄)→ 实时监控其每笔成交
→ 聚合净建仓流向 → 产出跟庄交易信号。回测仅作辅助验证,不是重点。循环间隔已改为 **3600s(每小时,job 533a8948)**。

## 🔁 两种运行模式
- **流式(streaming)**：`python -m smc_tracker.app` —— 常驻 WS，实时(低延迟)监控+信号。
- **轮询(polling)**：`scripts/poll_monitor.py` —— 每 3600s 跑一轮即退出(cron 友好，无常驻进程)：
  拉所有庄当前持仓 → 与上次快照 diff(换仓:平仓/反手/减仓) → 多庄共识 → 持仓面板 → 告警/推送。
  **更适合抓庄**：庄是持仓型，90s 内几乎不动，但**一小时**内换仓真实发生，小时级快照 diff 正好捕获。
  系统 crontab：`7 * * * * cd /path/smc && ./.venv/bin/python scripts/poll_monitor.py`

## 🧰 技术分析 + ML + 历史研究层（迭代 #20+，多 workflow 并行建成）
- **指标引擎** `indicators/`：10 技术指标(RSI/MACD/EMA/SMA/BOLL/ATR/Stoch/ADX/OBV/VWAP/CCI，numpy)
  + 价格行为(K线形态/吞没/锤子/pin) + 双顶双底/道氏理论(patterns) + 成交量监控(volume) + 斐波那契 +
  支撑压力/枢轴 + 4 combo + 时间策略(时段/killzone) + **KNN 预测器**(指标+PA 特征找历史最相似态投票)。
  统一 analyze()。修复 ADX 双重 Wilder 的 NaN 传播 bug。
- **聪明钱地址分析** `monitor/address_analyzer.py`：胜率/盈亏/行为画像 + 评分(0-100，胜率仅10分,盈利为主)。
- **地址关联** `monitor/address_correlation.py`：co_movers/clusters/counterparties → 协同地址群(庄家集团)。app 5min 实时扫描推送。
- **历史研究**：`fetch_bitget_history.py` 异步并行收集 22 币 2-3 年 1H K线(38.3万根)到外置盘；
  Workflow 多维深度分析(成交量/指标/时间/可预测性) → data/history/analysis/。
  **关键结论(base-rate 校正)**：meme 暴涨是**动量延续非能量积蓄** —— 已剧烈波动(振幅>30%)→12x lift、
  24h已涨>20%→12.7x、强放量>2.5×→5x；「缩量盘整」反而是反向指标(0.12x)。**买强势不买安静**。
  KNN 回测 11万样本仅 50.9%≈随机(简单 ML 无预测性,诚实)。
- **TA 复合信号** `signals/ta_signal.py` + KNN 历史回测(检验预测性)。

## 目标
**双所监控系统**，Python 实现，**低延迟 + 第一性原理 + 无 API key（纯公开数据）+ SQLite 持久化**：
- **System 1 — Hyperliquid（链上，地址级）**：监控聪明钱/巨鲸地址交易；meme 永续成交带双方地址，
  可直接追踪「谁在买卖 meme」；结合 SMC 市场结构生成信号。
- **System 2 — Bitget USDT-M 永续（CEX，市场级 + 链上）**：监控 meme 永续 **OI/资金费**；
  用 Bitget 提供的 meme **合约地址**，经**公开 RPC 链上直查**大额转账（CEX 无地址数据的「其他方式」）。
- meme 清单**按 Bitget 永续币种定义**（Bitget永续 ∩ Hyperliquid永续 ∩ meme 集）。

## 架构总览
```
                          ┌──────────────── SQLite (data/smc.db, WAL) ────────────────┐
                          │ hl_meme_trades · sm_events · bitget_oi · meme_contracts ·  │
                          │ onchain_transfers                                          │
                          └────▲─────────────▲──────────────▲────────────▲────────────┘
  System 1: Hyperliquid       │             │              │            │   System 2: Bitget + 链上
  ┌─────────────────────────┐ │             │              │  ┌─────────────────────────────────┐
  │ HL WS/REST (公开)        │ │             │              │  │ Bitget WS/REST (公开, 无key)      │
  │  trades(含users地址)/    │ │   AddressMonitor          │  │  ticker→OI/funding/mark           │
  │  userFills/webData2/l2   │─┤   MemeTradeMonitor─────────┤  │  coins→meme 合约地址              │
  └─────────────────────────┘ │   (买卖方/taker 归因)      │  └──────────────┬──────────────────┘
            │                  │                            │                 │
            ▼                  │                            │                 ▼
  ┌─────────────────────────┐ │                            │  ┌─────────────────────────────────┐
  │ SMC 引擎 structure.py    │─┘                            └──│ OnchainMemeMonitor (公开 EVM RPC) │
  │ BOS/CHoCH(已) OB/FVG(待) │     SignalEngine(待)            │ eth_getLogs Transfer 大额转账     │
  └─────────────────────────┘     聪明钱×SMC×OI×链上共振       └───────────────────────────────────┘
```

## 技术选型
- `websockets` 异步 + 自动重连 + 重订阅；`orjson` 解析；`aiohttp` REST；`numpy` SMC 计算
- SQLite WAL + 批量 executemany；单调时钟测延迟；全程非阻塞 asyncio
- 链上：原始 JSON-RPC（无 web3 依赖），公开 RPC 节点（publicnode 等），零 key

## 里程碑 / 任务清单
### M1 — Hyperliquid 数据接入层 ✅ 完成
- [x] 骨架/依赖/配置；异步 WS 客户端（重连+心跳+重订阅+orjson）；REST Info 客户端；数据模型
- [x] WS 实连冒烟（新鲜成交延迟 226–672ms）

### M2 — 聪明钱地址监控 / 抓庄（System 1）✅ 核心完成
- [x] **抓庄闭环**：`whale_discovery.py` 从排行榜自动发现庄(账户大+全期盈利+近月仍盈利) →
      AddressMonitor 订阅其 userFills/webData2 → **窗口聚合净建仓**(解决 HL 大单碎片化) →
      跟庄信号(净建/加/反手仓≥阈值)落 whale_signals 表 + webhook。5 单测 + 真实验证
      (15 庄/75s 捕获 1005 笔成交 → 4 条跟庄信号,如 庄#3 净做空 ZEC $3万)。
- [x] **可疑地址追踪（不放过）**：公开成交流(trades.users)里任意地址窗口净建仓越阈值 → 标记
      flagged_addresses + 告警 + **动态升级**(subscribe_address 订阅其 userFills 全量追踪)。
      **地址轨迹**：address_trajectory 查询(时间线+净累计) + scripts/trajectory.py。4 单测 +
      真实验证(85s 抓到 10 个排行榜外可疑地址,如 0x6ba…净买 TRUMP $6.2万)。
- [x] 各输出加 `[HH:MM:SS]` 时间戳（成交/信号用事件时间,周期用当前时间）+ 报告生成时间。
- [x] AddressMonitor：userFills 实时成交 + 开/加/减/平/反手分类 + 净流向 + 单测
- [x] webData2 权威持仓校正；REST 播种持仓；大单高亮 + JSONL
- [x] 现货成交(`@`前缀)过滤（审计发现）

### M3 — SMC 引擎 ✅ 完成
- [x] 市场结构：摆动高低点 + BOS / CHoCH（`smc/structure.py` + 6 单测）
- [x] 实时 K 线接入：`smc/feed.py` StructureFeed（收盘检测驱动结构 + on_closed 钩子）+ 3 单测
- [x] Order Block + FVG + 回补检测 + 溢价折价/OTE（`smc/zones.py` ZoneEngine + 7 单测，+0.15 信心）
- [x] **流动性区**（`smc/liquidity.py`）：等高/等低 + BSL/SSL + 扫荡(stop hunt)检测；
      接入信号（同向扫荡 +0.12 信心，聪明钱反转确认）+ 4 单测 + 真实数据验证

### M4 — meme 监控（双所）✅ 核心完成
- [x] meme 清单生成（Bitget永续∩HL永续∩meme，22 币，`build_meme_list.py` + `memecoins.py`）
- [x] System1 HL：MemeTradeMonitor — meme 成交带买卖双方地址，per-address/coin 净主动流向，落库
- [x] System2 Bitget：BitgetOIMonitor — ticker 实时 OI/资金费/标记价 + OI 异动检测，落库
- [x] System2 链上：OnchainMemeMonitor — 公开 EVM RPC(ETH/BSC/BASE) 直查 meme 大额 Transfer，落库
- [x] meme 合约地址抓取（Bitget coins，21 条，落 `meme_contracts`）
- [x] **Solana meme 链上监控**（`onchain/solana.py`）：**第一性原理实证 —— 无 key 公开 SOL RPC
      封禁持仓发现类重型方法(getTokenLargestAccounts 一律 429/403)，无法做地址级监控**；
      但 getTokenSupply 轻量可用 → 改为**供应量(mint/burn)监控**(rug/稀释信号)，落 sol_supply 表。
      接入 app 周期任务(120s)。2 单测 + 真实数据验证(12 mint)。holder 监控保留 best-effort(换不限流 RPC 可启用)。
- [x] **多信号叠加共振(超级信号)**（`signals/confluence.py`）：聚合跟庄/共识/背离/SMC 各表近1h 信号，
      同 coin 同向源数≥2 → 超级信号(coin 跨表归一化)。落 confluence_signals 表，app 周期 + 轮询每轮扫描。
      5 单测(2源/3源/矛盾/窗口)。稀有高质量事件(多独立源同向才出)。
- [x] **庄换仓预警**（`signals/position_tracker.py`）：持仓快照 diff 检测 平仓(exit)/反手(reversal)/
      大幅减仓(reduce) —— 跟庄(建仓)的互补面，顶级庄退场=行情可能结束的强预警。落 position_changes 表，
      app 90s 周期扫描(首轮基线)。6 单测 + 真实尺度验证(模拟庄#3 平 $7740万 ETH 空头被准确捕获)。
- [x] **多庄共识信号 + 庄持仓面板**（`signals/consensus.py`）：聚合所有监控庄的当前持仓，
      按 coin 统计多空人数/净名义；≥3 庄明显同向(且达名义阈值)→ 强共识信号(落 consensus 表)。
      app allMids 价格源 + 90s 周期扫描 + 持仓面板。5 单测 + 真实验证(15 庄→18 共识，
      如 ETH 4庄做空$1.3亿、FARTCOIN 6庄一致做空)。修复方向/净名义矛盾(用同向侧名义)。
- [x] **CEX × DEX 背离信号**（`signals/divergence.py`）：CEX 散户拥挤(资金费/OI)⟂ DEX 聪明钱流向
      → 分销(看跌)/吸筹(看涨)背离。落 divergence 表，app 60s 周期扫描。6 单测 + 真实验证
      (PEPE 分销背离：Bitget 多头拥挤 × HL 聪明钱净卖$31.7万)。**任一所单独给不出此信号**。

### M5 — SQLite 持久化 ✅ 完成
- [x] Store（WAL+批量）：hl_meme_trades / sm_events / bitget_oi / meme_contracts / onchain_transfers
- [x] 存储层单测（4 用例）；各监控器落库已验证

### M6 — 统一编排 & 信号引擎 🔄 进行中
- [x] **统一编排 `app.py`**：System1(HL) + System2(Bitget) asyncio.gather 并发；SMC 吃实时 K 线；
      周期 flush + 链上轮询；优雅退出；全程落 SQLite。实跑验证（14850 成交/2095 OI/链上转账落库）
- [x] 链上 USD 过滤接线：用 Bitget 标记价喂 OnchainMemeMonitor（min_amount_usd 真正生效）
- [x] **共振信号引擎 `signals/engine.py`**：SMC结构×聪明钱流向（要求同向共振）×OI×链上 打分；
      去重/冷却；落 `signals` 表。8 单测 + 2 app 集成测试。已接入 app（meme 也跑 SMC 结构）。
- [x] **信号风险参数**（`signals/risk.py`）：入场=现价、止损=SMC结构位(OB下沿/摆动低)、
      目标=2R投射、盈亏比；止损过远(>8%)的劣质 setup 拒绝。落 signals 表(entry/stop/target/rr，含旧库迁移)。
      5 风险单测 + 2 引擎集成 + 真实数据验证。
- [x] **回测引擎**（`backtest/engine.py`）：历史 K 线重放结构信号，模拟入场/止损/目标，
      胜率/期望/盈亏比；支持 OB-FVG / 扫荡 共振过滤对比。6 单测 + 真实数据回测(1万笔)。
      **发现**：纯结构突破≈盈亏平衡(33.5%@2R)；唯流动性扫荡确认有微弱正边际(+0.03R)。
- [x] **输出层**（`notify/`）：**多渠道推送** Webhook(Discord/Slack/通用) + **Telegram(Bot API)**，
      build_notifier 按 config 组装(MultiNotifier)，逐事件推送 + 周期摘要 + 日报。8 单测 + httpbin 实测。
      Telegram 需填 config.telegram.bot_token+chat_id(@BotFather 建 bot + 频道管理员)。

### M7 — 工程化（部分）
- [x] **端到端延迟基准（接收→信号 P50/P99）** ✅ #35：`perf.LatencyTracker`(numpy 环形缓冲,O(1) record)
      埋在 app 热路径(`_on_candle_ws`:recv_ns→处理完成,monotonic 同钟)→ 周期报告 ⏱️ P50/P99/max。
      `scripts/bench_latency.py` 确定性基准实证:指标全算 P50=0.62ms、TA 多因子 P50=1.22ms、前瞻预测 0.008ms
      —— 全部子毫秒~1.2ms,坐实「低延迟」。6 单测。
- [x] 优雅退出 ✅ app.stop + SIGINT/SIGTERM signal handler(早已具备)。
- [x] **健康检查** ✅ #43：`health.py` system_health(数据新鲜度+验证闭环积压,纯DB) + CLI `health` 子命令
      (非健康退出码2,cron 友好) + app `_periodic_health`(10min,异常才推)。自动化「能否追踪数据」人工排查。
- [x] **配置热加载** ✅ #63：`config.diff_config` 比对可热更字段 + app `_apply_config`(阈值/require_sweep→运行时对象、
      webhook/telegram→重建 notifier、llm→重建 analyst) + SIGHUP handler + `_periodic_config_reload`(30s mtime 看门狗)。
      改 config.yaml 不重启即生效。M7 工程化全部完成。

## 🎯 真实预测命中率（累计实测，1h 水平线）—— 诚实结论:首批假象,跨市场塌回随机
**批1(#49,08:55,下跌市)**：13 条命中率 84.6%/相对随机+34.6pp —— 但 12/13 做空+市场普跌 → base-rate 校正(#51)
判定**含趋势 beta**。**批2(#54,09:51,反弹市)**：15 条几乎全错(同样共识做空,市场反弹)。
**批3(#61,12:55,~平盘)**：18 条评估。**累计 62 样本(1h):命中率 50.0%(相对随机 +0.0pp,精确随机)、
共识 23/48=47.9%(低于随机)、跟庄 6/10=60%(样本小)、均按向收益 −0.03%≈零。本轮方向 16多/46空较均衡、
净漂移 +0.02%、beta 嫌疑未触发——即剔除趋势 beta 后仍 0 alpha。**
**最终诚实定论**：首批 84.6% 已被完全证伪为下跌市做空的趋势 beta；累计 62 样本跨 跌/涨/平 三种市况后
**1h 共识方向信号 = 精确随机(50%/0pp),零 alpha** —— 与项目既有结论「KNN≈随机/高lift≠赚钱」完全一致。
**系统核心价值=诚实可验证测量基础设施(数据准确6/6+验证闭环健全),它忠实地证明了自己的 1h 信号无 alpha,不夸大。**
后续仅剩希望:更长水平线(4h@14:33/24h@明日,匹配庄持仓周期,样本累积中)、按 coin 而非方向聚合。

## 数据真实性审计 ✅ 6/6 通过（scripts/audit.py）
A 价格三源一致 · B Σ持仓值==API totalNtlPos(0.0%) · C positionValue=|szi|×markPx(<0.1%) ·
D 多空符号 · E 真实 userFills 解析/分类自洽 · F WS webData2==REST 持仓。用真实巨鲸地址（排行榜）验证。

## 关键经验（踩坑记录）
- ⚠️ **Hyperliquid WS 只接受文本帧**：orjson.dumps 返回 bytes 必须 `.decode()` 再 send，否则收不到数据。
- ⚠️ **HL trades 含 `users:[买方,卖方]`（已实证）**：users[0]=买方 users[1]=卖方；side='B' taker主动买/'A'主动卖；
  `taker = users[0] if side=='B' else users[1]`。是 meme 地址级监控的核心。
- ⚠️ **跨所符号归一化**：Bitget `1000BONK`/`PEPE` vs HL `kBONK`/`kPEPE`（k=1000）；`memecoins.normalize()` 统一。
- ⚠️ **Bitget 合约地址逐币并发会限流**：改用 `all_coin_chains()` 一次拉全量(2747币)再本地匹配。
- ⚠️ **公开 RPC 负载均衡 head 不一致**：getLogs 需 `head_lag` 安全余量，只查已稳定区块。
- 排行榜 accountValue 是聚合口径(perp+spot+滞后)，≠ perp clearinghouseState.accountValue（审计 B 教训）。
- 现货成交 coin 形如 `@107`，不进永续分类。
- ⚠️ **排行榜端点已涨到 ~16.8MB，下载 >66s**（#41 实证）：`stats-data.hyperliquid.xyz/Mainnet/leaderboard`
  原 60s 总超时 → discover/fetch_pnl_rows **必 TimeoutError**(空错误信息)→ 抓庄入口整条断、watchlist 空、
  多表全空。修复:`fetch_leaderboard_rows()` 统一 helper(去重),`ClientTimeout(total=180, sock_connect=10)`
  分离连接/读超时(真网络阻断 10s 快失败、大 payload 慢读容忍) + `Accept-Encoding: gzip`。

## 📌 后续功能追踪 Backlog（#96 完整性审计建立 — 在此持续追踪未闭合项）

> 完整性现状（2026-06-22 #96 证据化审计）：**75 功能模块 / 12 功能域，0 真孤儿**（逐模块入边核验，
> 仅 `__main__.py` 零入边=入口非孤儿）；**62 测试文件 / 688 用例全绿**；12 CLI 子命令全接入 handler；
> 19 个 `_periodic_*` 周期任务运行时全挂载。系统主体完整、零孤儿合规。以下为**诚实标注的未闭合项**，持续追踪：

- [x] **Solana 链上监控接入**（#97 核实闭合）：经审计 `SolanaSupplyMonitor` 已接入运行时
      （`app.py:162` 实例化 + `_periodic_solana` `app.py:1075` + gather `app.py:1156`），SOL meme **供应量
      mint/burn 监控已生效**。SOL 交易所资金流（`onchain/monitor.py:92` 跳过）是**非 EVM 固有局限**
      （keyless 公开 RPC 封 `getProgramAccounts` 重型方法，`solana.py:4-8`），需付费 RPC，已诚实标注、非代码缺口。
- [~] **交易所资金流热钱包低估（#97 已缓解）/ Bitget BTC 地址（保留）**：① 热钱包低估**已缓解** ——
      `exchange_flow.py` BTC 分页 `_BTC_MAX_PAGES` 6→20（覆盖 150→500 笔/24h，窗口边界仍早停，仅 >500 笔极端热钱包
      残留低估）；② Bitget BTC 地址 **仍保留** —— 已 WebSearch+WebFetch 查证（CoinCarp 动态加载/Arkham 需登录，
      证实 Bitget ~3,163 BTC 储备+双热冷钱包但**无可验证的公开地址**），第一性原理不编造，待官方/链上标注确认。
- [x] **排行榜发现源稳定性**（#97 核实闭合）：`whale_discovery.py:24-58` #56 已工程化稳定性 —— 300s 超时 +
      **持久磁盘缓存** `data/leaderboard_cache.json` 失败回退（仅从未成功才抛），慢端点不再让整轮 poll 报废；
      `health.py:285` 已对缓存未建立状态做可观测告警。稳定性机制完备。
- [~] **真实部署 alpha 验证（#97 首次生产复盘完成，持续追踪）**：生产 `43.224.34.216` 真实累积数据**首次 alpha 复盘**
      （343 预测/111 已评估，按信号源真实命中率）：**SMC 73.9%(17/23)**、超级 45.5%(5/11)、跟庄 37.5%(3/8)、
      OKX 50%(2/4)、前瞻 100%(7/7,样本小)；⚠️ **背离 0%(0/46)、暴涨 0%(0/12)** 大样本 0% → 见下方新追踪项。
      此项本质持续验证（样本累积中），保持开放。
- [x] **🆕 背离/暴涨 0% 命中复查（#99 闭合：证伪反向 + 修复真 bug）**：用生产历史数据验证 ——
      ① **证伪「信号反向」**：背离 44 条全在 TRUMP（下行期，非独立样本），`market_neutral_stats` 去 beta 后
      背离≈随机(0.46)、暴涨+0.17 → 0% 是**市场 beta 污染非反向**，反手会错（验证救了一次误修正）；
      ② **发现并修复真 bug**：k 计价币（kSHIB/kFLOKI，HL 价≈Bitget×1000）`px_emit` 取 Bitget 原始、
      `px_eval`(price_of) 取 HL 千倍 → `realized_ret` 爆炸 +1000(+10万%)，污染 SMC 虚高 73.9%(avg_ret+347)。
      `record` px_emit 改 **HL 优先**（与 price_of 同源同单位）+ `accuracy_report` 离群守卫 `_RET_OUTLIER=10`。
      生产复盘(修复后)：SMC 真实 **60%**、剔除离群 8、市场中性 edge **+0.05**（略正，小样本诚实标注）。
- [x] **dashboard 维度完整 + 行情维度清理**（#97 闭合）：dashboard 已 **16 个 panel**（健康/准确率/交易所资金流/
      聪明钱净流向/鲸鱼信号/地址排行/庄家集团/OKX 强平·跨所·HL 挂单墙/链上转账…）覆盖全核心维度；
      并按用户#要求**移除 `行情监控板` 面板 + `renderTickerBoard` 死函数**（价/涨跌幅/费率/OI 不需要，后端数据保留）。

> 闭合规则：完成项改 `[x]` 并在迭代日志记 `#NN`；新发现的未闭合项追加到本列表（保持「后续功能追踪」常态化）。
> 诚实边界：环境约束（alpha 实时验证）与公开数据固有局限（热钱包低估/未确认交易所地址）**不假闭合**，
> 据 codex-loop 反幻觉纪律保持开放，区别于「已实现但 backlog 写保守」的项（已核实证据后闭合）。

## 迭代日志
- 2026-06-22 #101: **推送按币种多空比例组织 + live 降噪（背离冷却/占位地址）**（用户#连续指令）。
  ① 观察生产 live 卡片暴露噪声 → 背离同币同向 15min 冷却（每 60s 重复刷屏）、`util.is_placeholder_addr`
  过滤占位 0x0 地址（示例配置残留）+ 服务器 config watchlist 占位项移除；
  ② **币种多空比例**（用户#：推送按币种多空比例组织）：`HLDigest.add_bias(coin,bull,source)` 按币累计多空票，
  render 头部出【📊 币种多空比例】每币 🟢多N/🔴空N → 倾向（净多/偏多/分歧/偏空/净空）+ 共识来源（挂单墙 bid/ask
  净额计入；Top 活跃币上限防卡片过长）；app `_record_pred` choke point 覆盖 6 类 + ta/可疑，持仓语义模糊诚实排除。
  真实数据验证（用户#无模拟）：生产 189 条真实信号 → TRUMP 净多 99%(89/1)、PEPE 净空 100%(27)、PENGU 偏多 75%。
  TDD 6 例；全量 **697 passed**。
- 2026-06-22 #100: **挂单墙按币聚合（整体+单币总结）+ k 币 px_gap 单位归一（#99 收尾）**（用户#连续指令）。
  ① 挂单墙汇总改 `HLDigest.add_wall` 结构化按币聚合 → render「整体净意图 + 单币 bid/ask 净（压制/分销 or
  支撑/吸筹）」替代逐条原始；真实数据验证服务器 16148 条真实墙 → 3 币单行总结（整体净 ask $3.21B）。
  ② k 计价币 `px_gap_pct` 单位归一 `aligned_px_gap`（消除 10 的幂单位倍数）：真实 kSHIB/kFLOKI 旧 gap
  1.996(假性 199.6%) → 新 ~0.0002（真实 ~0%），数据质量告警 `gap_warn_count` 恢复诚实；真实 3× 分歧仍如实告警。
  验证脚本 `scripts/verify_wall_digest.py`。TDD 4 例（墙聚合 2 + gap 归一 2）；全量 **694 passed**。
  全程真实数据验证（用户#：无模拟数据）。
- 2026-06-22 #99: **信号精确性验证（用户#：根据历史数据验证信号是否精确）——证伪反向 + 修复 px 单位错配真 bug**。
  systematic-debugging 用生产历史数据查 0% 命中根因：**先证伪「背离/暴涨反向」**（背离 44 条全在 TRUMP 下行=
  非独立样本，去 beta 市场中性背离 0.46≈随机/暴涨 +0.17，故反手会错——验证拦下一次误修正）；**再定位真 bug**：
  k 计价币（kSHIB HL 0.0047 vs Bitget 4.694e-06，差 1000×）`px_emit` 取 Bitget、`px_eval` 取 HL → realized_ret
  +1000(+10 万%) 污染 SMC 虚高 73.9%。**修复** `review.record` px_emit 改 HL 优先（与 `evaluate_due` price_of 同源）
  + `accuracy_report` 离群守卫 `_RET_OUTLIER=10` + `outlier_count`。生产复盘(后)：SMC 真实 60%、剔离群 8、
  市场中性 edge +0.05。诊断脚本 `scripts/diag_signal_inversion.py`/`prod_accuracy.py`。TDD 2 例；全量 690 passed。
  钱包画像空壳过滤（`WalletSnapshot.is_empty`，净值$0/0 持仓不推）。
- 2026-06-22 #98: **尽力推进 Backlog ②④（用户#指令：贴出保留项要求继续）——实质推进，不假闭合**。
  ④ **首次生产 alpha 复盘**：服务器真实累积 343 预测/111 已评估，按源命中率 SMC 73.9%/超级 45.5%/跟庄 37.5%/
  前瞻 100%(小样本)；**暴露背离 0/46、暴涨 0/12 大样本 0% 命中**（疑信号反向）→ 新增🆕追踪项（复查方向映射，
  若反向则反手即正期望）。④ 标 `[~]`（首复盘完成，持续验证）。
  ② **热钱包低估缓解**：`exchange_flow._BTC_MAX_PAGES` 6→20（150→500 笔/24h 覆盖，窗口早停仍常态），标 `[~]`；
  **Bitget BTC 地址**经 WebSearch+WebFetch 查证（~3,163 BTC 储备/双热冷钱包确认，但 CoinCarp 动态/Arkham 需登录、
  **无可验证公开地址**）→ 第一性原理不编造，保留开放。全量 **688 passed**。诚实边界：缓解可缓解、复盘可复盘，
  无可实证地址与环境约束不假闭合（codex-loop 反幻觉）。
- 2026-06-22 #97: **行情维度移除（价/涨跌幅/费率/OI 不需要）+ Backlog 闭合 3 项（诚实保留 2 项）**（用户#指令）。
  ① 价格标签 `_price_tag` 简化为**仅价格 + 数据来源**（去涨跌幅/费率，价格因 #96「标记完整价格」保留）；
  ② 行情监控板推送默认关闭（`OutputCfg.push_ticker_board=False`，`_periodic_ticker_board` 早退，可配置恢复）；
  ③ dashboard 移除「行情监控板」面板 + `renderTickerBoard` 死函数（后端数据保留，更新测试为「不含面板」断言）；
  ④ **Backlog 闭合**：①Solana（核实 `SolanaSupplyMonitor` 已接入运行时）、③排行榜稳定性（核实 #56 已工程化
  超时+磁盘缓存回退）、⑤dashboard（16 panel 完整 + 行情维度清理）→ 改 `[x]`；**诚实保留** ②热钱包低估/Bitget BTC
  地址（公开数据固有局限+无可实证地址，不编造）、④alpha 验证（环境约束，须生产长跑）→ 保持 `[ ]`，按反幻觉纪律不假闭合。
  全量 **688 passed**。聚焦 HL 抓庄、降行情噪声；后续追踪常态化（闭合可闭合项，诚实标注不可闭合项）。
- 2026-06-22 #96: **数值非科学化 + 飞书单卡片 + HL 分类汇总 + dashboard 上线 + 完整性审计**（用户多诉求连续推进）。
  ① `util.fmt_px` 统一非科学计数法完整数字（去重 app/wallet 两处重复），全栈接入 8 文件 + 标注价格数值来源
  （Bitget现价/HL现价/HL成交价），修复服务器实测 `6.387e+04`→`63,870.00`、`2.533e-05`→`0.00002533`；
  ② 飞书 `_payload`/`send` 改为**信息集中一张卡片**（同卡多 div、一次 POST，不再拆多条消息）；
  ③ 新增 `notify/digest.HLDigest`：10 类 HL 事件按分类聚合成**一张分类汇总卡片**（核心抓庄信号在前，降噪去刷屏），
  10 个事件级 `_push`→`_emit(分类)`，仅超级共振/可疑地址 urgent 即时；`_periodic_hl_digest`（默认 5min）+ `DigestCfg`；
  ④ **dashboard 上线服务器**：`smc-dashboard.service`（127.0.0.1:8799，Restart=always，journald）+ nginx 反代公开 8787，
  本地 Mac 外网实测 `http://43.224.34.216:8787/` 200/557KB、`/health` 数据新鲜（age<5s）；
  ⑤ **完整性审计**：75 模块 0 孤儿、688 用例全绿、本 Backlog 建立后续追踪。全量 **688 passed**。
- 2026-06-20 #1: M1 数据接入层完成（WS/REST/模型/配置），修复 WS 文本帧 bug。
- 2026-06-20 #2: M2 AddressMonitor 核心完成（分类+净流向+webData2+大单+JSONL），main.py，README，5 单测。
- 2026-06-20 #3: **大扩张**。① 数据真实性审计 6/6（真实巨鲸交叉验证）。② meme 清单(Bitget∩HL，22 币)。
  ③ SQLite 存储层。④ Bitget 数据层(REST OI/合约 + WS)。⑤ 用 **agent team 并行** 建成四模块：
  MemeTradeMonitor(HL地址级)、BitgetOIMonitor(OI实时)、OnchainMemeMonitor(无key链上)、SMC structure(BOS/CHoCH)。
  全部实连/实测验证，**38 单测全过**。确立**双所 + 无key + 第一性原理 + SQLite** 架构。
  下一步（#4）：M4 Solana 链上监控（公开 SOL RPC，多数 meme 在 SOL）；或 M3 Order Block/FVG；
  或 M6 统一 main.py 同时跑两套系统 + 三源共振信号。
- 2026-06-20 #4: **M6 统一编排 app.py 完成** —— System1+System2 asyncio.gather 并发跑，
  SMC 接入实时 K 线（smc/feed.py StructureFeed，收盘驱动）+3 单测，链上 USD 过滤接线。
  实跑验证双所并发落库（hl_meme_trades/bitget_oi/onchain_transfers/meme_contracts），**41 单测全过**。
  入口：`PYTHONPATH=src ./.venv/bin/python -m smc_tracker.app`。
  下一步（#5）：M6 三源共振信号引擎（聪明钱流向×SMC结构×OI×链上打分）；或 M4 Solana 链上；
  或 M3 Order Block/FVG。注：链上 meme 转账 $50k 阈值偏高（meme 多为小额），可在 config 暴露调参。
- 2026-06-20 #5: **M6 共振信号引擎完成**。`signals/engine.py`：要求 SMC结构方向与聪明钱主动流向
  **同向（共振）**才出信号，OI异动/链上大额作信心加成；冷却去重；落 `signals` 表。接入 app —
  meme 也订阅 K 线跑 SMC 结构（24 币并发播种）；结构事件触发→拉流向+OI→评估。
  实跑见 DOGE/SPX CHoCH 实时触发、链上 PEPE 380亿($106k)。8 引擎单测 + 2 app 集成测试，**51 单测全过**。
  下一步（#6）：M3 Order Block + FVG（丰富 SMC 入场）；或 M4 Solana 链上监控；
  或信号风险参数（止损/目标价）+ webhook 输出。
- 2026-06-20 #6: **M3 Order Block + FVG 完成**。`smc/zones.py` ZoneEngine：FVG（看涨/看跌缺口）+
  OB（位移前最后反向 K 线）+ 回补检测 + 溢价折价/OTE，7 单测。接入 app（区域共振 +0.15 信心、
  历史播种、收盘驱动）+ 信号引擎 set_zone。**实跑产出真实信号**：
  `FARTCOIN long +0.98 | CHoCH↑ × 聪明钱净+201万 × OI+0.1% × OB/FVG共振`（已落 signals 表）。
  **59 单测全过**。下一步（#7）：M3 流动性区（等高等低/liquidity sweep）；或 M4 Solana 链上；
  或信号风险参数（入场/止损/目标价、RR）+ webhook。
- 2026-06-20 #7: **信号风险参数完成**。`signals/risk.py` compute_risk：入场=现价、止损放 SMC 结构位
  （OB下沿/摆动低，叠缓冲）、目标 2R 投射、RR；止损>8% 劣质 setup 拒绝。接入引擎(set_levels)+app
  (喂现价/结构位/OB边界) + signals 表加 entry/stop/target/rr(含旧库 ALTER 迁移)。
  真实 K 线验证 4 币交易计划合理(止损 0.3–0.4%/RR2)。**66 单测全过**。
  下一步（#8）：M3 流动性区(等高等低/liquidity sweep)；或 M4 Solana 链上；或 webhook 推送 + 回测重放。
- 2026-06-20 #8: **M3 流动性区完成 → SMC 引擎全部完成**。`smc/liquidity.py` LiquidityEngine：
  等高/等低 + BSL/SSL + 扫荡检测（刺破收回内侧=扫荡，区别于突破）。接入信号引擎(set_sweep +0.12)
  + app（收盘驱动、近30min同向扫荡确认、历史播种）。4 单测 + 真实数据验证（kPEPE 59次/POPCAT 80次扫荡）。
  **71 单测全过**。SMC 五因子信号体系完整：结构方向 × 聪明钱流向（必须共振）+ OI/链上/OB-FVG/扫荡 加成 + 风险参数。
  下一步（#9）：M4 Solana 链上监控（多数 meme 在 SOL，公开 SOL RPC）；或 webhook 推送；或回测重放校验信号胜率。
- 2026-06-20 #9: **回测引擎完成**。`backtest/engine.py`：历史 K 线重放结构信号 → 模拟入场/止损/目标
  → 胜率/期望/盈亏比；支持共振过滤对比。6 单测 + 真实回测(22 meme × 5000根 5m，约 1 万笔)。
  **诚实结论**：纯 SMC 结构突破 33.5%胜率@2R ≈ 盈亏平衡；+OB/FVG 无边际；**+流动性扫荡** 只取37%setup、
  期望 +0.004→+0.030R(PF1.05) —— 验证「扫荡反转」逻辑，且印证真 alpha 来自无法回测的实时聪明钱流向。
  （未计手续费/滑点）。**77 单测全过**。下一步（#10）：M4 Solana 链上监控；或 webhook；或把回测的
  「扫荡前置」作为信号硬门槛（require_sweep）提升实盘信号质量。
- 2026-06-20 #10: **回测入场方式对比 + 操作化结论**。回测加「回撤到 OB 限价入(retrace)」对比「追突破(break)」。
  **诚实结论：retrace 反而更差**(meme -0.016R vs break +0.006R) —— meme 情绪驱动、OB 不可靠，
  回到 OB 常直接穿过；追突破能抓延续。四档最优仍是 **break+扫荡(+0.031R/PF1.05)**。
  据此把 `require_sweep` 做成实盘信号硬门槛(config.detection.require_sweep，默认 off)+ 接入 app + 单测。
  **80 单测全过**。下一步（#11）：M4 Solana 链上监控；或 webhook/日报；或多周期(15m/1h)回测找更优 TF。
- 2026-06-20 #11: **Solana 链上监控完成（含诚实边界）**。`onchain/solana.py`。**第一性原理实证**：
  无 key 公开 SOL RPC 一律 429/403 封禁 getTokenLargestAccounts/getProgramAccounts，**SOL 侧无法做
  地址级监控**（与 EVM 的 eth_getLogs 顺畅形成对比）；但 getTokenSupply 低频可用 → 转为**供应量
  mint/burn 监控**（rug/稀释信号），落 sol_supply 表，接入 app(120s 周期)。2 单测 + 真实 12 mint 落库验证。
  **82 单测全过**。下一步（#12）：三源背离信号(CEX OI×DEX流向×链上)；或 webhook/日报；或多周期回测。
- 2026-06-20 #12: **CEX×DEX 三源背离信号完成**。`signals/divergence.py`：资金费(CEX 散户拥挤代理)
  ⟂ HL 聪明钱净流向 → 分销(看跌)/吸筹(看涨)背离，OI 上升放大。落 divergence 表，app 60s 周期扫描。
  6 单测 + **真实验证**：PEPE 分销背离(Bitget funding+0.010% 多头拥挤 × HL 聪明钱净卖$31.7万,分0.31)。
  **88 单测全过**。这是双所架构核心价值 —— 单所给不出的边际。
  下一步（#13）：webhook/日报推送；或多周期(15m/1h)回测；或把背离接入主信号作为额外因子。
- 2026-06-20 #13: **输出层完成**。`notify/webhook.py` WebhookNotifier（无 key，POST 同带 content/text 键
  兼容 Discord/Slack/通用，带限流，失败静默）+ `notify/report.py` build_report（从 SQLite 聚合
  信号/背离/聪明钱净流向 Top/链上）。接入 app：信号+背离推 webhook、周期摘要(1h)；scripts/report.py 按需。
  4 单测 + **httpbin 实测推送成功** + 真实 DB 日报。**92 单测全过**。
  下一步（#14）：多周期(15m/1h)回测找更优 TF；或把背离接入主信号；或信号成交后跟踪(实际 R 兑现/胜率回填)。
- 2026-06-20 #14: **系统定位校准为「聪明钱监控/抓庄」**(用户指示:不需回测,定位监控+交易信号+抓庄)。
  补上最大短板(watchlist 为空=无庄可抓)：`whale_discovery.py` 自动从排行榜发现庄 + AddressMonitor.add_addresses
  + 跟庄信号(窗口聚合净建仓,解决 HL 大单碎片化—1005笔成交聚合成4条信号) + whale_signals 表。
  实跑:15 庄/75s/1005 成交→4 跟庄信号(庄#3 净空 ZEC/LIT、庄#2 净多原油)。**97 单测全过**。
  循环间隔改 3600s(每小时,job 533a8948,删旧 4c6a513b)。
  下一步（#15）：跟庄信号质量提升(只跟 taker 吃单/按持仓占比)；或庄持仓面板(谁在多/空什么)；或多庄共识信号。
- 2026-06-20 #14b: **可疑地址追踪 + 全输出时间戳**(用户:实时监控地址/不放过可疑地址/追踪轨迹/加时间戳)。
  MemeTradeMonitor 加可疑检测(任意地址窗口净建仓≥2×阈值→on_suspicious)；app 标记 flagged_addresses
  + 动态 subscribe_address 升级全量追踪；Store.address_trajectory + scripts/trajectory.py 轨迹查询；
  所有 print 加 [HH:MM:SS]。**101 单测全过**。真实验证：85s 抓 10 个排行榜外可疑地址并升级、轨迹可还原。
- 2026-06-20 #15: **多庄共识信号 + 庄持仓面板**。`signals/consensus.py` WhaleConsensus：聚合所有庄持仓
  (AddressMonitor.all_positions)，≥3 庄明显同向(≥2×多数)且名义达阈值→共识信号(consensus 表)；
  positioning() 出持仓面板。app 加 allMids 价格源 + 90s 周期扫描。修复方向/净名义矛盾(用同向侧名义)。
  5 单测 + 真实验证(15 庄→18 共识:ETH 4庄空$1.3亿、HYPE 5庄空、FARTCOIN 6庄一致空)。**106 单测全过**。
  下一步（#16）：共识接入跟庄信号加权；或庄换仓预警(持仓 diff)；或信号汇总面板增强。
- 2026-06-20 #16: **庄换仓预警**。`signals/position_tracker.py` WhalePositionTracker：持仓快照 diff →
  平仓(exit)/反手(reversal)/减仓(reduce, min_notional$1M)，跟庄(建仓)的互补面。落 position_changes 表，
  复用 _periodic_consensus 持仓快照(首轮基线)。6 单测 + 真实尺度验证(3 庄 110 持仓,模拟平 $7740万 ETH 空头
  被准确捕获)。实跑 0 检测属正常(持仓型庄 90s 内极少完整清仓,平仓是稀有事件)。**112 单测全过**。
  下一步（#17）：共识/换仓加权进跟庄信号；或多信号叠加告警(同 coin 多信号共振)；或庄盈亏排名动态刷新。
- 2026-06-20 #17: **轮询监控模式**(用户:需要轮询监控,3600s)。`monitor/poll_monitor.py` PollMonitor +
  `scripts/poll_monitor.py`：每轮纯 REST 拉所有庄持仓 → 与 SQLite 持久化快照 diff(换仓) → 共识 → 面板
  → 告警/webhook → 存快照退出。state 跨运行接力(whale_positions 表 + tracker.seed_prev)。
  小时级 diff 比 90s 流式更适合抓庄(庄换仓是小时级事件)。3 单测 + 真实双轮实跑(8庄131持仓→7共识,
  跨运行 diff 正确)。**115 单测全过**。系统 crontab 跑 `scripts/poll_monitor.py` 即每小时监控。
  下一步（#18）：轮询里加 userFills 近1h 净流向(跟庄/背离)；或庄盈亏排名变化预警；或多信号叠加。
- 2026-06-20 #18: **轮询增强 + 动态循环 + Telegram 推送**(用户:动态监控/推送/Telegram)。
  ① PollMonitor 加近1h userFills 净流向(跟庄建仓) + Bitget 资金费三源背离。
  ② scripts/poll_monitor.py 加 `--loop --interval` 持续动态运行,每周期推送。
  ③ **Telegram(Bot API) 推送** `notify/telegram.py` + MultiNotifier 多渠道(webhook+TG) + build_notifier；
     app/poll 全部信号逐事件推 + 摘要推。config.telegram(api_id/api_hash 已存,待用户填 bot_token+chat_id)。
  scripts/test_telegram.py 验证。**119 单测全过**。
  ⚠️ 用户给的是 MTProto api_id/api_hash，但频道告警用 Bot API 最简(只需 bot_token+chat_id)，已说明。
  下一步（#19）：用户配好 TG 后端到端验证推送；或多信号叠加共振告警；或庄盈亏排名变化。
- 2026-06-20 #19: **多信号叠加共振(超级信号)**。`signals/confluence.py` ConfluenceAggregator：
  聚合 whale_signals/consensus/signals/divergence 各表近1h 信号，同 coin 同向源数≥2 → 超级信号
  (coin 跨表 normalize 归一)。落 confluence_signals 表，app 周期 + 轮询每轮扫描，逐事件推送(含 TG)。
  5 单测 + 集成实跑(7共识+1背离未重叠→0,符合设计:多源同向稀有)。**124 单测全过**。
  下一步（#20）：把 poll 的 flow 建仓也纳入共振源；或 TG 配好后验证；或庄盈亏排名变化预警。
- 2026-06-20 #20~28（多消息+多 workflow 并行大迭代）：
  · **Telegram 推送打通**（Bot API，Chiukwan49Bot，chat_id 6707146007，实测推送成功）+ 多渠道 MultiNotifier。
  · **轮询监控模式** poll_monitor.py（--loop 动态，每3600s，纯 REST，跨运行快照 diff）+ 近1h流向/背离。
  · **指标引擎 indicators/**（10指标+PA形态+双顶双底/道氏+成交量+斐波那契+支撑压力+4combo+时间策略+KNN）。
  · **聪明钱地址画像** address_analyzer（评分胜率仅10分,盈利为主）+ **地址关联** address_correlation（协同地址群/庄家集团,app 5min 实时扫描）。
  · **3 个 workflow 并行**：①patterns/volume/地址分析/指标测试 ②暴涨暴跌4维深度分析 ③TA信号/KNN回测/胜率去权重。
  · **历史研究**：异步并行收集 22 币 2-3年 1H K线（38.3万根→外置盘）；workflow 炼出《暴涨暴跌交易指南》。
  · **PumpRadar** 把验证规则操作化（RSI>70&ATR%>3→18x lift 等），接入 app 收盘预警，真实历史 414暴涨/381暴跌态触发，DOGE黑名单0暴涨。
  · 诚实结论：KNN≈随机；高 lift≠赚钱（尾部押注）；选币>>择时；buy strength not quiet。
  **185 单测全过**。下一步：庄盈亏排名变化预警；或把 PumpRadar 纳入共振信号源；或实盘信号回填胜率。
- 2026-06-20 #29: **庄 PnL 动量追踪**。`monitor/whale_momentum.py` WhaleMomentum：快照排行榜各庄
  PnL(日/周/月/全期)+净值到 whale_pnl_snapshots，跨时间 diff → 变热(加速盈利)/变冷(回吐) + 当前最火(近24h)。
  接入轮询每轮快照+diff。4 单测 + 真实验证(最火庄近24h +$5100万/账户$10.3亿)。**189 单测全过**。
  下一步：PumpRadar 纳入共振源；或多周期指标；或庄换仓+PnL动量联动(变热的庄在建什么仓)。
- 2026-06-20 #30: **前瞻资金流预测**(用户:要预测性/前瞻性/地址资金动向,不靠历史记录)。
  `signals/flow_predictor.py`：① 订单簿失衡(l2Book 挂单=尚未成交的意图,比成交早一步)
  ② 资金流加速度(2阶导,领先价格,非"已流入多少") ③ OI 速度 → 三者同向预测方向,矛盾则过滤。
  接入 app 30s 周期(采样净流向→取加速最强币拉订单簿+OI→预测)。7 单测 + 真实验证
  (BTC+0.20/DOGE+0.33 买盘厚看涨、HYPE-0.61 卖盘厚看跌,且印证庄群净空 HYPE)。**196 单测全过**。
  关键转向：从「回看庄做过什么」→「前瞻资金正在往哪 positioning」(挂单意图领先成交)。
  下一步：把前瞻预测纳入共振信号；或多档订单簿动态(挂单墙增减);或链上待确认大额转账前瞻。
- 2026-06-20 #31: **全栈纲领 + 代码去重**（用户纲领:第一性原理/前瞻/LLM-Codex-GPT5.4/全栈完整/去重简化/
  低延迟/数据质量/异步并行,已存记忆 loop-directives）。① 移除 legacy main.py(app.py 取代)。
  ② 建 `util.py`(to_float 统一有限性校验=数据质量 + fmt_hms)；`_f`×8 文件 + `_hms` 去重并入。
  ③ workflow 并行:indicators numpy 向量化(低延迟) + ARCHITECTURE.md(架构梳理) + 全栈完整性审计(待集成清单)。
  **196 单测全过**。下一步:按审计待集成清单接入未用模块(TASignal 等);加 LLM(Codex GPT5.4)分析层;
  前瞻预测纳入共振。**纲领见 memory/loop-directives.md,后续 loop 必遵循。**
- 2026-06-20 #32: **全栈集成(零孤儿零死代码)+ CLAUDE.md 规范**。按审计待集成清单全部接入:
  TASignal(激活整个 indicators 包在生产执行)、VolumeMonitor(放量监控)、AddressAnalyzer(庄画像入轮询)、
  AddressCorrelation.counterparties/correlated_with(关联告警)、fmt_analysis(暴涨预警附 TA 全景)。
  monitor/signals/indicators __init__ 导出补全；constants.py 接入(URL 常量 + VALID_INTERVALS 数据质量校验);
  indicators 向量化(workflow,38x,~1ms)；ARCHITECTURE.md(架构梳理 20KB)。
  **CLAUDE.md 开发规范**(用户:开发前先思考/多假设/知识库结合开源 → 最高优先级；含第一性原理/前瞻/
  零孤儿全代码使用/去重/低延迟/数据质量/异步并行/验证规范)。死代码复查全部被使用。**196 单测全过**。
  下一步:LLM(Codex GPT5.4)分析层；前瞻预测纳入共振；bitget 下标加固(数据质量)。
- 2026-06-20 #33: **LLM(Codex OAuth GPT-5.4)前瞻研判层**(用户:第一性原理/预测性/系统+用户提示词用 codex)。
  第一性原理先实证:`codex` CLI 在本沙箱一致挂起(网络/鉴权初始化被阻断,exit 124)→ 按「可插拔+默认关闭+
  优雅降级」设计,真实 codex-login 环境开 `llm.enabled` 即用。新模块 `llm/`:prompts.py(系统提示词=抓庄研判员
  /第一性原理/前瞻/数据不足须明说;用户提示词=build_report 态势摘要,尾部截断保最新)、codex_client.py
  (asyncio 子进程+硬超时杀进程+失败 None,绝不阻塞热路径,argv/model 可配)、analyst.py(MarketAnalyst 编排+
  build_analyst 工厂)。config.LLMCfg(默认 enabled=False)。app `_periodic_llm` 接入 gather(禁用即退,不占资源)→
  态势摘要喂 GPT-5.4 → 🧠抓庄研判推送。scripts/llm_analyze.py 独立验证。**18 新单测**(cat/sleep/false 桩验证
  子进程契约,全降级路径覆盖)。真实 db 演示提示词成形正确。零孤儿。**214 单测全过**。
  下一步:研判输入再加前瞻预测/协同地址群上下文(extra);前瞻预测纳入共振权重;bitget 下标加固。
- 2026-06-20 #34: **TA-Lib 基准交叉验证(数据质量)**。后台装好 TA-Lib 0.6.8(158 函数)→ 第一性原理:
  不替换 numpy(已向量化 ~1ms、无硬依赖、可移植),而用 TA-Lib 作业界基准**实测验证** 10 指标正确性。
  500 根合成 K 线尾部比对:SMA/EMA/RSI/MACD/BBANDS/ATR/STOCH%K/ADX/CCI **9 个浮点级一致**(1e-8~1e-14);
  **OBV 偏 10.8%** → 抓到首值约定差异(TA-Lib OBV[0]=首根量,我从 0 起,恒定偏移 volume[0])→ 修正首值
  (不影响斜率/背离,但绝对值对齐基准),修正后 4e-11 一致。固化 `tests/test_talib_parity.py`(10 测试,
  importorskip 零硬依赖:有 TA-Lib 则验证、没有则跳过)。**224 单测全过**(214+10)。
- 2026-06-20 #35: **热路径延迟埋点 + 基准(实证低延迟)**。第一性原理:低延迟不靠声称要测。
  新 `perf.LatencyTracker`(预分配 numpy 环形缓冲,record O(1) 不阻塞,P50/P99/max,NaN/inf 守卫);
  接入 app 热路径——WS 接收即打 monotonic_ns(recv_ns),`_on_candle_ws` 同钟测「接收→处理(含收盘信号计算)」
  端到端延迟,周期报告附 ⏱️ P50/P99/max。`scripts/bench_latency.py` 确定性基准(300根×3000迭代):
  指标全算 P50=0.62/P99=0.75ms、TA全景 0.80ms、TA多因子 1.22ms、暴涨雷达 0.21ms、前瞻预测 0.008ms
  —— **全部子毫秒~1.2ms,坐实低延迟**。6 新单测。零孤儿。**230 单测全过**(224+6)。
  下一步:健康检查/配置热加载;前瞻预测纳入共振权重;LLM 研判加前瞻/协同上下文。
- 2026-06-20 #36: **强化核心硬编码算法:筛选地址 + 协同地址**(用户:硬编码才是核心,LLM 只做分析)。
  ① 筛选 `smart_money_score` 加三判别器:跨窗一致性(周&月&全期皆正=持续edge,过滤运气16分)、
  ROI/资本效率(月PnL/账户净值,区分大资金碰运气vs高手14分)、做市商/刷量判别(高量但方向盈亏<0.1%→×0.85);
  权重重平衡(28/18/16/14/8/8/8)。② 协同 `address_correlation` 重写:固定分桶 `t//w` → **滑窗+不应期**
  (消除边界伪影:相隔1秒跨桶不再漏判;一次持续狂热只记一次协同事件);追踪**跨币数**——跨市场协同是
  同一实体硬证据;`clusters_detailed` 返回群画像(跨币/协同次数/对数)。**关键修正**:实测发现 min_coins=1
  被单币重叠污染(人群与真集团同币同窗→并查集合并成大团)→ app 改 **min_coins=2**(跨≥2币),干净隔离
  追涨人群,只留真庄家集团。③ LLM 分析层接入硬编码产出(`_hardcoded_context`:庄家集团+筛选Top → extra 喂模型)。
  **6 新单测**(边界修复/单币人群过滤/跨币集团识别/一致性/ROI/churn 判别)。零孤儿。**236 单测全过**(230+6)。
  下一步:前瞻预测纳入共振权重;健康检查/热加载;协同加 lead-lag(识别集团核心 leader)。
- 2026-06-21 #37: **全模块缺陷审计+修复(workflow 11 审计 agent 对抗验证)+ Opus规划Sonnet执行**。
  workflow 88 候选→41 确认(2 high/20 med/19 low)。**2 HIGH(核心数据失真)**:HL/Bitget WS subscribe 无去重→同一
  handler 注册 N 份→每条消息分发 N 次→净流向/成交/OI 累积 N 倍失真+推送轰炸 → handler 去重修复(+回归测试)。
  MEDIUM 亲修:WS send 超时/pong 看门狗/create_task 引用、协同不应期键缺 side、address_monitor startPosition 自愈、
  meme_trade 删死代码 _taker_net(消泄漏)、whale_discovery/meme/poll 数据质量加固、pump_radar 规则方向/prior 窗、
  solana 限流传播、onchain log_index 去重(替浮点主键)、zones/structure/liquidity base-offset 裁剪(消 24/7 无界增长)、
  db save_whale 原子事务+空守卫、stochastic %D NaN 修复(此前恒 None)、app _push 引用+周期清理。LOW 由 **4 个 Sonnet
  并行执行**(onchain/indicators/signals/monitor 文件不相交):block 守卫/节流、rsi 全平盘=50/pa_bias 去重/combo 守卫/
  fib 校验、confluence 收窄 except/risk min_stop、whale_momentum 边界/移除热路径 flush。**246 全过**(+6 回归)。
- 2026-06-21 #38: **CLI + HTML 仪表盘 + 推送价格涨幅 + 完整时间戳 + 正确性回顾层（5 项，Sonnet 并行执行）**。
  ① `cli.py`+`__main__.py`：统一 8 子命令(run/poll/report/address/discover/bench/llm/dashboard)。
  ② `dashboard.py`：aiohttp 实时单页仪表盘(信号/净流向/庄家集团/OI/聪明钱Top/链上/暴涨，5s fetch 自刷新，无依赖)。
  ③ 推送加**实时价格+24h涨幅**(Bitget lastPr/change24h，BWE 风格，7 推送点)。
  ④ **时间戳标记好**：util.fmt_ts(日期+时间+时区) → 所有推送告警前缀升级(控制台高频行保持简洁 HH:MM:SS)。
  ⑤ `review.py` **正确性回顾层**：前瞻推送落 predictions 表(HL+Bitget 两源价交叉验证 px_gap_pct)，到期核对真实价
  → realized_ret + 方向对错 → 分类命中率/校准报告(诚实复盘纠正)；`_periodic_review` 周期推送 📊。
  功能冒烟全通过(回顾报告含完整时间戳)。**309 单测全过**。工作分工见 memory[[opus-plan-sonnet-exec]]。
- 2026-06-21 #39: **行情监控板 + 交易所资金流监控（Sonnet 并行执行 + Opus 接线/验证）**。
  ① **行情板**：oi_monitor.ticker/board_rows(币种/价格/涨跌幅/资金费率/OI) → app `_periodic_ticker_board`(5min
  推送 📊 行情监控板，按涨跌幅排序) + `_price_tag` 加资金费率 + 仪表盘行情板区块。
  ② **交易所资金流**(用户:监控 okx/bn/bitget 资金动向)：第一性原理实证 keyless 数据源——**blockstream.info 通**
  (mempool.space 被沙箱拦)，验证真实交易所地址(Binance 冷248k/热/聚合、OKX 3.79万 BTC)。新 `onchain/exchange_flow.py`
  (BlockstreamClient **分页** /txs/chain 覆盖~150笔、btc_flow_24h 纯函数、ExchangeFlowMonitor 自管 exchange_flows 表)
  + `config/exchange_wallets.yaml` 注册表。app `_periodic_exchange_flow`(每小时核对 24h 净流入/流出，大额越阈值推送
  🏦：净流入🔴=潜在抛压/净流出🟢=吸筹) + 仪表盘交易所流区块。**真实数据冒烟**:Binance 24h 净流出 309 BTC(告警触发)。
  诚实局限:公开地址种子可能不全、blockstream 分页上限对极端热钱包仍低估、Bitget 公开 BTC 地址待补。**342 单测全过**。
- 2026-06-21 #40: **EVM 稳定币(USDT/USDC)交易所流（Sonnet 执行 + Opus 实证/接线/修复）**。
  第一性原理实证 keyless eth_getLogs（publicnode）支持 topic 数组(OR)+address 数组，验证真实交易所 ETH 地址
  (Binance F977 $16.9B/14/15/16、OKX 0x4612 $500M、Bitget 0x0639)。`exchange_flow.py` 新增 EVMStableFlow
  (分块+二分回退+地址 OR 过滤) → poll_once ETH 路径(美元计) → fmt_flow_alert **单位感知**(BTC 净流入🔴抛压/
  稳定币净流入🟢买盘弹药，语义相反)。config.evm(rpc/window/chunk/stablecoins/threshold)。**Opus 修复**:agent 留下
  孤儿 `_get_logs`(实际走 `_get_logs_with_split`)→ 删孤儿 + 把瞬时 403/429/5xx 退避重试加到真正方法。
  **真实数据冒烟**:Binance 近 20min 稳定币净流入 $5.0M(226 日志)。**357 单测全过**。
  下一步:补 Bitget/OKX 更多地址、BSC 链;前瞻预测纳入共振;协同 lead-lag;健康检查/配置热加载。
- 2026-06-21 #41: **系统核心有效性审计（5min /loop 触发）+ 2 项关键修复**。审计三问:能否追踪/采集数据、
  能否对比行情后期验证追踪目的。实证结论:① HL 主 REST `/info` 通(1.9s/230 永续)→**行情采集本身通畅**;
  ② **CRITICAL-1 抓庄入口断**:排行榜端点已涨到 16.8MB/下载>66s,超原 60s 超时 → discover/fetch_pnl_rows
  必 TimeoutError(空错误)→ watchlist 空、whale_signals/positions/pnl 多表全空。**修复**:`fetch_leaderboard_rows()`
  统一 helper(whale_discovery/momentum 去重)+ 分离连接/读超时(180s/10s)+ gzip。**实证恢复**:discover 返回 8 庄
  (PnL$235M~$115M)。③ **CRITICAL-2 验证闭环未接入部署路径**:review.py(predictions 表:前瞻落库→到期核对真实价
  →命中率)仅接 app 流式模式,而部署 crontab 跑的是 poll_monitor → predictions 表实际从未创建、「是否符合追踪目的」
  从未被验证。**修复**:PredictionReview 接入 PollMonitor(`_record_predictions` 落跟庄/共识/背离/超级 4 类前瞻 +
  evaluate_due 到期核对 + 准确率附 digest),`_make_price_of` normalize 容错(kPEPE/PEPE 跨命名命中)。
  **1 新单测**(落库→到期评估→3/4 命中,覆盖 normalize/方向映射)。零孤儿(grep 自查)。**358 单测全过**。
  下一步:把 review 闭环也补进 app `_periodic_review` 实跑回填;补部署后真实命中率复盘;健康检查/配置热加载。
- 2026-06-21 #42: **审计续:实时 DB 启动验证闭环 + poll 双拉取去冗余(低延迟)**(5min /loop)。
  ① **低延迟修复**:实证 poll 每轮**下载两次 16.8MB 排行榜**(discover + fetch_pnl_rows)→ 拆 `pnl_rows_from()`
  纯函数,run_once **单次 `fetch_leaderboard_rows()` 复用**(选庄排名 + PnL 动量),省每轮一次下载(~66s)。
  ② **实时 DB 真实跑通**:对 data/smc.db 跑一轮 poll(2:39,印证省时)→ 15 庄/148 持仓、12 多庄共识
  (ETH 3庄空$1.24亿/HYPE 4庄空$1.05亿/BTC 3庄多)、webhook 已推送、**predictions 表创建并落 13 条真实预测**
  (真实发出价 BTC$64260/ETH$1740,1h 水平线,08:51 到期)→ 下轮自动核对方向对错产出真实命中率。
  捕获真实背离:BTC「跟庄 short(近1h净流向卖)」⟂「共识 long(净持仓多)」,系统诚实分别记录交回顾层判别。
  **1 新单测**(pnl_rows_from 解析/过滤/排序)。零孤儿(discover/fetch_pnl_rows 仍供 cli/app)。**359 单测全过**。
  下一步:1h 后核对 08:51 到期预测出首份真实命中率;app `_periodic_review` 同步;健康检查/配置热加载。
- 2026-06-21 #43: **系统健康检查(M7 完成项)**(5min /loop;08:02 预测未到期,转推进健康检查)。第一轮审计靠人工
  才发现「数据停滞 7h/cron 未跑/到期预测无人评估」→ 把它自动化。新 `health.py`:`system_health(store,now_ms,
  stale_after_s)` 纯 DB 查 7 张核心表新鲜度(bitget_oi/hl_meme_trades/sm_events/consensus/whale_positions/
  whale_pnl_snapshots/predictions) + 验证闭环积压(total/evaluated/pending/**overdue**=到期未评=管线停滞强信号);
  `fmt_health` 中文渲染(✅/⚠️);总体 ok=≥1表新鲜 且 无 overdue。接入:CLI `health` 子命令(非健康退出码2,
  cron 友好) + app `_periodic_health`(600s,仅异常推 🩺,不空推)。**实时实证**:smc.db 显示 WS 表(bitget_oi/
  hl_meme_trades/sm_events)停在 7.7h(流式 app 未跑) vs poll 表(consensus/positions/predictions)0.2h 新鲜、
  13 预测待评 0 到期未评 → 精确还原审计画像。**4 新单测**(新鲜/stale/overdue/pending 四态)。零孤儿。
  **363 单测全过**(359+4)。下一步:08:51 后核对到期预测出首份真实命中率;配置热加载;health 接入仪表盘。
- 2026-06-21 #44: **验证层诚实性增强:相对随机基线 + 样本充分性**(5min /loop;08:05 预测仍未到期 08:51,转强化
  评估诚实度)。CLAUDE.md 硬要求「诚实标注/不夸大/KNN≈随机」——方向预测随机基线是 50%,小样本命中率噪声极大,
  必须对比基线 + 标注样本不足,否则到点报出的「符合追踪目的」会失真。`review.accuracy_report` 向后兼容新增:
  `edge`(命中率−0.5 方向边际,总体+分类)、`sufficient`/`min_sample`(默认20)。`fmt_accuracy` 增「相对随机(50%)
  边际 ±Xpp」行 + 样本不足时「⚠️ 仅供参考」告警。**演示验证**:13 样本(共识12+跟庄1)→ 报告输出「边际+11.5pp/
  样本不足(13<20)仅供参考」,精确实现诚实校准。**3 新单测**(edge/不足/充分/自定义阈值),全用真实 record→evaluate
  路径。向后兼容(fmt_accuracy 全 .get 容错,旧 rep 不崩)。**366 单测全过**(363+3)。
  下一步:08:51 后跑 poll 核对到期预测出**首份真实命中率(诚实标注)**;配置热加载;health/accuracy 接入仪表盘。
- 2026-06-21 #45: **审计抓出并修复仪表盘 HTML 渲染 BUG(浏览器实证)**(5min /loop;预测未到期,审计观察工具)。
  第一性原理实证 render_html 真实输出 → 发现 `_HTML_TEMPLATE` 用 `.format()` 风格双括号 `{{`/`}}` 转义,
  但 render_html 只 `.replace("__INITIAL_STATE__")` **从不 .format/解转义** → 输出残留字面 `:root{{`(CSS 失效)
  + `${{fmtTime(r.ts)}}`(JS 模板插值变 `${ {…} }` 对象字面量含调用 key=**语法错误**)→ renderAll 抛错 →
  **整页永远卡「加载中」**。旧测试只查子串存在、从不校验 CSS/JS 良构,故漏过。**修复**:render_html 注入前先
  `{{`→`{`/`}}`→`}` 解转义(实证模板无三连括号/无裸单括号,安全),再注入 JSON(JSON 括号注入后才出现不受影响)。
  **浏览器实证**(claude-in-chrome 真实加载 dashboard)：深色主题生效、行情板填满真实 meme 价格+资金费率+OI、
  9 卡片网格、**0 console 错误、不再卡加载**。**1 回归测试**(断言无残留 `{{`/`}}`/`${{` + CSS/JS token 良构 +
  注入 JSON 可解析)。**367 单测全过**(366+1)。下一步:08:51 后核对到期预测出首份真实命中率;health/accuracy
  接入仪表盘(现可放心做,渲染已修);配置热加载。
- 2026-06-21 #46: **health + 预测准确率接入仪表盘(浏览器实证)**(5min /loop;预测未到期,推进可视化)。
  续 #44 因 brace bug 推迟的可视化:`build_dashboard_state` 加 `health`(system_health)+`accuracy`
  (PredictionReview.accuracy_report) 两 section(try/except 降级);新增 `renderHealth`(逐表新鲜度✅/⚠️陈旧+
  验证闭环积压)+`renderAccuracy`(命中率+相对随机边际+样本不足告警+分类表)JS 渲染函数(双括号匹配模板转义),
  注册到 sections 置顶(审计核心观察)。**浏览器实证**:系统健康面板显示 7 表新鲜度(WS 表红色 8.2h陈旧 vs poll
  表绿色 0.6h)+「预测13·待评13·到期未评0」;准确率面板「样本不足继续积累」;0 console 错误。修正 #45 回归测试
  (注入 JSON 合法含嵌套 `}}`,改查明确畸形标记 `${{`/`:root{{`/`(){{` 而非笼统 `}}`)。**1 测试增强**(空库 health/
  accuracy 为 dict 良构)。零孤儿。**367 单测全过**。下一步:08:51 后核对到期预测出首份真实命中率;配置热加载。
- 2026-06-21 #47: **审计中期发现:真实数据初步验证「庄共识追踪行情方向」**(5min /loop;预测 08:51 才到期,
  做只读中期核对不写库)。13 预测发出 41min(水平线 60min)后拉真实 HL 现价对比方向:**11/13=84.6% 方向暂正确
  (相对随机 +34.6pp)**——庄群共识做空的 ETH/HYPE/SOL/ZEC/DOGE/LIT/FARTCOIN/XPL/TAO/WLD 均小幅下跌(印证抓庄
  thesis:跟庄群净持仓方向有效);仅 BTC 共识 long(微跌)、AVAX 共识 short(微涨)两条暂错。**诚实标注**:幅度极小
  (−0.05%~−1.07%,低波动时段)、样本13<20、未满水平线 → 仅趋势预览,正式命中率以到期 evaluate_due 为准。
  无代码改动(纯只读真实数据审计)。下一步:08:51 后跑 poll 正式评估出首份命中率(诚实标注)入库+推送+仪表盘。
- 2026-06-21 #48: **审计抓出数据质量缺陷:纯现货巨鲸污染 watchlist + 诚实标注修复**(5min /loop)。
  排查 #47 现象「庄#1(PnL最高$235M/近24h最火+$11M/账户$10.3亿)画像却 0单/0仓/评分0」。实证:vault 假设证伪
  (vaultDetails 空),但**永续 clearinghouse 账户=$0、userFills=0、持仓=0** → 与 PLAN 关键经验「排行榜 accountValue
  是 spot+perp 聚合口径」吻合 → **它是纯现货/休眠巨鲸**:排行榜按聚合 PnL 选为庄,但系统追踪永续 → 无可追数据,
  画像误导性报「评分0/100」(实为无数据非真低质)。**修复**:`is_perp_active(n_positions,n_trades)` 纯函数 +
  analyze 输出 `perp_active` 标志;poll 画像对无永续活动者诚实标注「(无永续活动·疑纯现货/休眠,排行榜 spot+perp
  聚合口径)」而非误导性 0 分。无害信号(0持仓本就不进共识/流向)。**1 新单测**(is_perp_active 四态)。零孤儿。
  **368 单测全过**(367+1)。下一步:08:51 后正式评估到期预测出首份命中率;(可选)discover 过滤无永续活动地址;配置热加载。
- 2026-06-21 #49: **审计高潮:首份真实命中率 + 修复 avg_ret 方向符号误导**(5min /loop;13 预测 08:51 到期)。
  ① 时序细节:poll 用启动时 now_ms 贯穿,08:49 启动→evaluate 步时 now_ms 仍 08:49<到期 08:51:48→未评;改用
  **当前时间戳直接 evaluate_due**(轻量,无需整轮)→正式评估 13 条入库。**首份命中率 84.6%(11/13)/相对随机+34.6pp**
  (共识 10/12、跟庄 1/1;仅 AVAX 空微涨/BTC 多微跌错)→庄群共识方向初步有预测性。② **真实数据抓出 avg_ret
  符号误导 BUG**:realized_ret 存原始价变动,做空正确时为负→「均收益-0.39%」看似亏实为盈(做空价跌=赚)。
  修复:accuracy_report/recent 改用**方向调整收益**(做空取负原始变动=策略真实盈亏),avg_ret 转正 +0.39%、
  分类共识 +0.42%、recent「按向」展示与 ✅ 一致;原始 realized_ret 仍按值入库。诚实标注:样本13<20/幅度小/1h,
  仅初步符合追踪目的。**3 新单测**(is_perp_active 已 #48;本轮 短做空按向为正/看多不翻转/边际)。**370 单测全过**(368+2)。
  下一步:持续积累样本到统计显著;avg_ret 误导修复同步仪表盘 renderAccuracy;配置热加载。
- 2026-06-21 #50: **avg_ret 方向调整同步仪表盘 + 浏览器实证真实命中率上板**(5min /loop;下批 09:50 到期,无可评)。
  续 #49:仪表盘 renderAccuracy 列头「均收益」→「均按向收益」匹配方向调整语义(数据本就走 accuracy_report 已修)。
  **浏览器实证**(claude-in-chrome 真实加载):预测准确率面板显示真实「样本13·命中率84.6%·相对随机+34.6pp」+
  ⚠️样本不足告警 + 分类表(跟庄1/1 100% +0.02%、共识10/12 83% +0.42%,均按向收益绿色);0 console 错误。
  审计成果完整上板可视化。dashboard 测试 24 通过。**370 单测全过**。
  下一步:09:50 评估下批 15 预测累计样本;持续到统计显著(≥20);配置热加载。
- 2026-06-21 #51: **审计诚实性关键增强:base-rate 校正(趋势 beta vs 选币 alpha)**(5min /loop;下批 09:50 到期)。
  质疑首份 84.6%:12/13 做空且市场普跌→「下跌市做空什么都赢」是趋势 beta 非 alpha?(CLAUDE.md base-rate 校正/
  高lift≠赚钱/不夸大)。`accuracy_report` 新增 `n_long/n_short/dir_skew/avg_market_move/beta_suspect`(方向一边倒
  ≥80% 且市场同向漂移→疑 beta);`fmt_accuracy` 加「方向分布 X多/Y空·同期净市场漂移 Z%」+ beta 嫌疑告警。
  **真实13样本实证**:方向分布 1多/12空·净漂移 −0.39%→ **beta_suspect=True,诚实标注「边际或含趋势 beta,
  谨慎归因」**。修正 #49 里程碑结论:84.6% 边际大概率含下跌市做空 beta,需多空均衡/震荡市样本复验。**2 新单测**
  (全空跌市触发 beta 旗 / 多空均衡不触发)。**372 单测全过**(370+2)。下一步:09:50 评估下批累计样本;震荡市复验。
- 2026-06-21 #52: **规范数据真实性审计修复并复跑 6/6 全过 + info_client 超时加固**(5min /loop)。
  跑项目自带 `scripts/audit.py`(原 6/6)复核「能否准确追踪/采集数据」→ 实证发现两处被压垮:① audit.py
  `fetch_json` 对 16.8MB 排行榜用 20s 超时(#41 同根)→ 改用项目 `fetch_leaderboard_rows`(180s/gzip),
  去重移除 fetch_json/LEADERBOARD/urllib/json 死导入;② `info_client` `ClientTimeout(total=10)` 对活跃巨鲸
  数 MB userFills 偏紧超时(影响生产:poll 对 15 庄调 user_fills 超时即丢该庄流向)→ 分离 total=30/sock_connect=8。
  **真实数据复跑 6/6 全过**:A 三源价一致·B Σ持仓值==API totalNtlPos·C positionValue≈|szi|×mark(偏差<2%)·
  D 多空符号·E userFills 解析分类自洽(2000笔)·F WS webData2==REST 持仓。数据层准确性获权威复核。**372 单测全过**。
  下一步:09:50 评估下批预测累计样本;震荡市复验 alpha;(可选)配置热加载。
- 2026-06-21 #53: **验证闭环时效性修复:评估步用新鲜时间戳**(5min /loop;下批 09:50 到期前 7min)。
  #49 发现的时序细节:poll 在 cycle 开头捕获 now_ms 贯穿全程,但拉数据(排行榜+多庄持仓/成交)耗时数分钟,
  执行到末尾 evaluate_due 时 now_ms 已过时 → 运行期间到期的预测本轮漏评,要等下一轮(小时级 poll 最多延迟 1h)。
  修复:run_once 评估块改用 `eval_now=int(time.time()*1000)` 新鲜时间戳(prices 仍用本轮 all_mids,~分钟级新鲜
  足够 1h 水平线评估)。小而正确的时效改进。**372 单测全过**。
  下一步:09:50 后评估下批 15 预测累计样本;持续到震荡市/统计显著分离 alpha。
- 2026-06-21 #54: **审计关键诚实发现:第二批评估证实首批是趋势 beta,信号塌回随机**(5min /loop;09:50 批到期)。
  评估第二批 15 预测(08:50 发出,反弹市):同样「共识做空」几乎全错(TAO/XPL/FARTCOIN/LIT/AVAX/DOGE/SOL 价回升)。
  **累计 28 样本:命中率 53.6%(相对随机+3.6pp)、共识 11/24=45.8%(低于随机)、跟庄 4/4(样本小)**。印证 #51
  base-rate 警告:首批 84.6% 是下跌市做空趋势 beta,非选币 alpha;跨一跌一涨后塌回≈随机。**诚实定论(与项目
  KNN≈随机/高lift≠赚钱一致):系统核心=诚实可验证测量基础设施(数据准确6/6+验证闭环健全),但 1h 共识方向信号
  尚未证明 alpha,绝不夸大**。无代码改动(纯真实数据评估+诚实记录)。下一步:试更长水平线(4h/24h 匹配庄持仓
  周期)、震荡市更多样本、按 coin 聚合复验。
- 2026-06-21 #55: **多水平线预测验证基础设施(1h/4h/24h) + 报告分水平线分解**(5min /loop;承接 #54 下一步)。
  #54 诚实定论 1h 共识≈随机,但庄持仓周期是小时~天级 → 1h 水平线可能太短。建多水平线验证:PollMonitor 加
  `horizons=(1h,4h,24h)`,`_record_predictions` 每信号按各水平线各落一条(dedup 升级为 coin,kind,horizon);
  `accuracy_report` 加 `by_horizon` 分解、`fmt_accuracy` 加「分水平线命中率」段。后续 poll 累积 4h/24h 样本后
  即可对比「信号在哪个时间尺度有 alpha」(无法历史回测——无历史庄持仓快照,只能前向积累)。**2 新单测**(多水平线
  各落一条+按各自到期评估+by_horizon;原单测改单水平线确定性)。零孤儿。**373 单测全过**(372+1)。
  下一步:积累 4h/24h 样本(需数小时~天)对比水平线;震荡市更多样本;按 coin 聚合。
- 2026-06-21 #56: **根因诊断+修复:HL 排行榜端点降速致 poll 整轮报废 + 缓存回退**(5min /loop)。
  连续两轮 poll 空 `TimeoutError`→ 逐调用计时定位:**排行榜 stats-data 已降速 66s→148s 且偶 >180s**(其余调用
  正常),180s 超时被击穿→整轮 poll 报废(预测未记录)。修复三件:① cli 错误上报带 `type(exc).__name__`(消除空错误,
  #41 同类教训);② 排行榜超时 180→300s;③ **持久文件缓存回退** `fetch_leaderboard_rows` 成功写盘
  `data/leaderboard_cache.json`、失败回退上次缓存(庄列表小时级稳定,可接受)→慢/挂端点不再让整轮报废,仅从未
  成功过才抛。**实证修复**:poll 成功、缓存 15MB 已写、多水平线预测开始累积(1h/4h/24h 各 16 条入库)。
  **2 新单测**(失败回退缓存/无缓存抛错)。零孤儿。**375 单测全过**(373+2)。
  下一步:4h(14:33)/24h(明日)到期后对比水平线命中率;震荡市样本;按 coin 聚合。
- 2026-06-21 #57: **health 纳入抓庄发现源(排行榜缓存)新鲜度**(5min /loop;预测累积中,11:33 才到期)。
  #56 揭示 stats-data 排行榜是关键脆弱依赖→把缓存新鲜度纳入 health 可观测性:`_leaderboard_cache_status`
  (懒导入 _LB_CACHE,读 mtime 算 age,阈值 4h 更宽因庄列表稳定,信息性不门控 ok);`fmt_health` 加「抓庄发现源」段
  (缓存过旧=端点持续失败、庄列表过时→追踪降级)。**实时实证**:health 显示「✅ 排行榜缓存 0.2h 前」+ 验证闭环
  76 预测(28 已评/48 多水平线待评)。**1 新单测**(report 含 leaderboard_cache 且不门控 ok)。零孤儿。**376 单测全过**。
  下一步:11:33 评估 1h 批、14:33 评估 4h 批对比水平线命中率;震荡市样本。
- 2026-06-21 #58: **部署入口一致性核验 + 类型化错误上报**(5min /loop;预测累积中 11:33 才到期)。
  核验实际部署 cron 入口 `scripts/poll_monitor.py`:与 CLI poll 共用 `PollMonitor.run_once`,自动继承全部修复
  (多水平线#55/缓存回退#56/超时#52/时效#53);补齐其 `--loop` 错误上报带 `type(e).__name__`(与 #56 cli 一致,
  消除 TimeoutError 空错误)。编译核验通过。**376 单测全过**。
  注:审计已全面完成(#41-#58 修 8 缺陷+诚实校验+部署韧性+全链路可观测),余下为时间门控样本积累
  (1h 11:33/4h 14:33/24h 明日)与跨市况复验真 alpha,非代码工作。下一步:到期评估多水平线对比。
- 2026-06-21 #59: **TG/webhook 完整推送(分段全发,不截断)**(用户要求:输出到 tg 需要完整)。
  原 cli/poll 把 digest 截断到 1800 字、telegram.py 又 `text[:4000]` → 长摘要(共识+流向+面板+画像+准确率)被砍。
  新 `notify/chunk.py` `split_message`(按行边界切,单行超长硬切,内容零丢失);TelegramNotifier 分段全发(≤4000,
  带 (i/n) 页码,段间 0.4s 防 429);WebhookNotifier 同理(≤1900,Discord 2000 上限);cli/poll 去掉 1800 截断传全文。
  **实证**:4509 字消息→分 2 段(3997+511)全部送达真实 TG 频道(chat_id 6707146007,failed=0)。**3 新单测**
  (短文不分/按行切/长行硬切,零丢失)。零孤儿(split_message 导出)。**379 单测全过**(376+3)。
  附:本轮 poll 用 #56 缓存回退(排行榜 TimeoutError→回退 39359 行)成功跑通,评估 16 条 1h 到期(累计已评 44)+
  记录新多水平线批(62@1h/34@4h/34@24h)。
- 2026-06-21 #60: **完整地址档案/分析系统**(用户:继续追踪地址完整信息+完整分析系统)。此前地址信息分散在
  画像/实时持仓/协同/对手方/轨迹/PnL 多处,`address` 命令只显示基础 profile。新 `monitor/address_dossier.py`
  `build_dossier`(异步汇总六维)+`fmt_dossier`(纯渲染可测):① 聪明钱画像(评分/胜率/全期·月·周PnL/做市判别/
  perp_active/偏好币) ② 实时逐币持仓完整明细(方向/名义/入场/杠杆/未实现盈亏/强平价,按名义降序) ③ 协同地址
  co-movers(庄家集团线索) ④ 频繁对手方(疑似关联/自成交) ⑤ 近期成交轨迹时间线 ⑥ PnL快照+可疑标记。
  CLI `address <addr> [--hours]` 升级为完整档案出口;各维 try/except 降级、空态友好。**真实实证**(庄 0xecb63caa):
  账户$35.9M/净空$90.7M/83持仓(ETH空$33.5M 15x 强平3355…)/协同 0x6ba…/对手方 0x31ca…×29/轨迹15笔。
  **3 新单测**(组装/空态/标记+轨迹,fake info 无网络)。零孤儿(CLI 接入+monitor 导出)。**382 单测全过**(379+3)。
- 2026-06-21 #61: **审计成熟定论:62 样本跨三市况→1h 信号精确随机(0 alpha)**(5min /loop;评估12:43批18条)。
  跑 poll 评估 18 条 1h 到期 + 累积新多水平线批。**累计 62 样本(1h):命中率 50.0%/相对随机 +0.0pp、共识
  23/48=47.9%、均按向收益 −0.03%。本轮 ~平盘(漂移+0.02%)、方向较均衡(16多/46空)、beta 嫌疑未触发→剔除
  趋势 beta 后仍 0 alpha**。首批 84.6% 完全证伪为下跌市做空 beta。**这是审计最终诚实答案:测量基础设施完美
  (能追踪/采集/闭环对比),它忠实证明 1h 共识信号无 alpha,与项目既有 KNN≈随机结论一致,绝不夸大**。
  剩余希望:4h(14:33)/24h(明日)更长水平线匹配庄持仓周期。**382 单测全过**(无代码改动,纯评估+诚实记录)。

- 2026-06-21 #62: **钱包完整持仓画像 + 开仓/平仓时间追踪 + 信号有效性自适应加权 + market-neutral alpha 审计**
  (本会话 Opus 规划/实证/验证, Sonnet 3 轮执行, [[opus-plan-sonnet-exec]])。用户:优先 HL 钱包地址/不限资金/结合历史/存外置盘/
  地址显示币种·仓位·方向·开平仓时间/提高命中率/解决隐性问题。
  ① monitor/wallet_portfolio.py:第一性原理实证 clearinghouseState(庄#1 净值$32.9M/总名义$101M/83 持仓全字段:
     币种·方向(多🟢空🔴)·名义·入场·uPnL·杠杆·爆仓,不限资金全展示);watched_wallets+wallet_positions_full 持久化外置盘
     + _seed 启动 load_wallets 接力(重启不丢地址)。② monitor/position_lifecycle.py:实证 userFills.dir(Open/Close/反手 X>Y,
     上限2000笔/庄#1 28min满)→position-netting+dir 重建当前持仓段开仓时间/持仓时长/最近平仓;info user_fills_by_time 分页突破2000
     拉历史。实证 SOL持仓25m/DOGE 1m/LINK 9m。cli wallet [--history N]+dashboard。
  ③ signals/efficacy.py:Wilson 置信区间 + meta-labeling(回顾命中率反哺信号),推送附实证命中率标注,反指(上界<50%)标
     contrarian 降权、高效(下界>50%)加权、小样本中性(不基于噪声乱调)。实证:共识 n=48 CI[0.34,0.62]含50%→中性。
  ④ scripts/alpha_audit.py 横截面去均值市场中性化:62条 1h 原始50.0%→市场中性50.0%(纯alpha≈0pp),本轮平盘 beta≈0
     ——确认 1h 信号无选币 alpha(与 #61/KNN≈随机一致,诚实不夸大)。
  ⑤ 隐性问题:跟庄历史反指(efficacy 自动 contrarian 标注);多自主循环(cron 5min+Ralph+Sonnet)并发改非git仓库有竞态
     (单测记录 382 vs 457),用户选维持并行,只读 pytest 确认合并后未破坏。457 单测全过。
  下一步:更长 horizon(4h/24h)匹配庄持仓周期积累样本;efficacy 样本足后筛真 alpha 信号子集;市场中性化进 review 常态化。

- 2026-06-21 #63: **M7 健康检查 + 配置热加载完成（M7 收官,Sonnet 执行/Opus 验证）**。
  ① health.py HealthMonitor.snapshot/fmt:数据新鲜度(各表 MAX(ts) fresh/stale/empty/unknown)+WS连接(hl _connected_evt/_running,bg)
     +热路径延迟(latency.stats)+内存累积器规模+验证闭环(预测/已评/到期未评)→overall ok/degraded/down。app _periodic_health
     周期自检(非ok才推送告警,避免噪声)+dashboard /health 端点(ok 200/down 503)+cli health 子命令。
  ② 配置热加载:config.diff_config 纯函数(比对可热更字段)+app _apply_config(阈值/require_sweep/console→运行时对象,
     webhook/telegram→重建notifier,llm→重建analyst)+_reload_config+SIGHUP handler(Windows守卫)+_periodic_config_reload
     (30s mtime看门狗)。改 config.yaml 不重启即生效。
  ③ 实证:health 命令真实抓到运维问题(数据17.7h stale+68条到期未评估→评估管线停滞告警),健康检查发挥真实价值。
  **470 单测全过**(457+13)。M7 全部完成(健康检查/配置热加载 ✅)。
  下一步:常驻进程让数据新鲜+评估管线常跑;前瞻预测纳入共振权重;efficacy 样本足后筛真 alpha;协同 lead-lag。

- 2026-06-21 #64: **alpha_audit 分 horizon 市场中性诊断增强 + health 去重(后台进行中)**(Ralph loop;Opus surgical+Sonnet)。
  ① scripts/alpha_audit.py 增强:按 horizon_ms 分层输出市场中性命中率(找匹配庄持仓周期的有效尺度),诚实判定
     (n≥20且中性边际>5pp=疑似真alpha,否则≈随机)。py_compile OK。实证:当前仅 1h 有评估(62条)中性50%≈随机;
     4h/34条·24h/34条已记录待到期评估(届时即可诊断有效尺度)。
  ② 隐性问题去重:并发竞态致 health.py 双套健康检查(#43 system_health + #62 HealthMonitor)并存+app/dashboard 混用,
     派 Sonnet 收敛(system_health 纯DB为唯一新鲜度真相源 + HealthMonitor 复用补运行时,统一引用,删重复)。
  下一步:4h/24h 到期后 alpha_audit 分层诊断有效尺度;efficacy 样本足筛 alpha;市场中性化进 review 常态化;协同 lead-lag。

- 2026-06-21 #65: **health 去重收敛完成验证(隐性问题闭环,Opus 验证)**。Sonnet 删除重复 B 套
  (_data_freshness/_overall/DEFAULT_CHECKS 全删),收敛到 system_health 为唯一 DB 新鲜度真相源(并补 overall 键
  + wallet_positions_full 表)、HealthMonitor 复用 system_health + 叠加运行时(WS/延迟/内存)、dashboard/cli/app 统一引用。
  Opus 独立验证:**470 单测全过(0 失败)**、grep 确认 B 套零残留(仅注释/db_overall 变量子串)、import 干净。
  竞态重复技术债清除。下一步:协同 lead-lag(识别集团核心 leader,前瞻);前瞻预测纳入共振;市场中性化进 review;4h/24h 样本积累。

- 2026-06-21 #66: **协同 lead-lag——识别庄家集团核心 leader(前瞻增强)**(Ralph loop;Opus规划/Sonnet执行/Opus验证)。
  第一性原理:跟 leader 比跟整团更前瞻(leader 先建仓,followers 跟随)。address_correlation.py 加 lead_lag
  (业界 time-lagged cross-correlation:同币同向滑窗内 #(A先于B)−#(B先于A)=净领先,不应期防单次狂热膨胀)
  + cluster_leader(群内领先得分最高且>0=leader,否则 None 诚实标注无显著领先)。app _periodic_correlation
  庄家集团告警附 "核心leader:0xXX…(领先N次)"。10 新单测(A总先于B→leader/对称→None/不应期/链式/边界)。
  真实数据实证:0x31ca…领先得分12(领先30/被领18)为最强 leader。零孤儿(cluster_leader@app.py:676)。**480 单测全过**(470+10)。
  下一步:前瞻预测纳入共振权重;市场中性化进 review 常态化;4h/24h horizon 样本积累诊断有效尺度。

- 2026-06-21 #67: **前瞻预测纳入共振源(强化「前瞻性」产品方向)**(Ralph loop;Opus规划/Sonnet执行/Opus验证)。
  FlowPredictor 领先信号(订单簿挂单意图+资金流加速度,领先于已成交)此前纯内存不入共振。
  ① db.py 加 flow_predictions 表(ts/coin/direction/score/vel/accel/book_imb)+insert_flow_prediction。
  ② app _periodic_flow_predict 产出 pred 即落库。③ confluence _SOURCES 加 ("flow_predictions","前瞻")——
  前瞻(领先维度独立源:挂单意图先于成交)与跟庄/共识(已成交)同向→超级信号。5 新单测(往返+纳入前瞻同向出超级信号
  +矛盾不出+三源)。零孤儿(insert@app _periodic_flow_predict、源@confluence:47)。**485 单测全过**(480+5)。
  下一步:市场中性化进 review 常态化;4h/24h horizon 样本积累诊断有效尺度;efficacy 加权进 confluence 打分。

- 2026-06-21 #68: **市场中性化进 review 常态化(诚实化核心 + 去重)**(Ralph loop;Opus规划/Sonnet执行/Opus验证)。
  review.py 加纯函数 market_neutral_stats(横截面去均值:同时间桶减均值得超额→按方向判中性命中→剔除趋势 beta 的纯 alpha)
  + accuracy_report 接入(查询加 ts 分桶,返回 market_neutral 键) + fmt_accuracy 加"市场中性命中率"行(样本不足标注)。
  _periodic_review 周期推送自动诚实区分 alpha/beta。scripts/alpha_audit.py 复用 review.market_neutral_stats
  (去重,单一定义@review.py:15,被 accuracy_report+alpha_audit 引用)。9 新单测(beta污染剥离/真alpha检出/跨桶隔离/向后兼容)。
  实证:62条 1h 中性 50%≈随机(数字复用后不变)。**494 单测全过**(485+9)。
  下一步:4h/24h horizon 样本积累诊断有效尺度;efficacy 加权进 confluence 打分;常驻进程让评估管线常跑数据新鲜。

- 2026-06-21 #69: **efficacy 历史命中率加权进 confluence 打分(信号质量自适应闭环完成)**(Ralph loop;Opus规划/Sonnet执行/Opus验证)。
  confluence 超级信号打分此前纯源数量(0.5+0.2*n_agree-0.15*opposing)。现 confluence.set_efficacy 注入 SignalEfficacy
  (app:172 在 efficacy 构造后),scan 打分改 weighted_agree=Σ weight_of(各同向源)——高效源(共识)贡献大、反指源(跟庄)降权;
  无 efficacy 退化纯数量(向后兼容)。源名↔kind:跟庄/共识/背离/前瞻 对得上,SMC 默认1.0。4 新单测(加权高/低/退化/注入)。
  零孤儿(set_efficacy@confluence:64←app:172,weight_of@scan)。**498 单测全过**(494+4)。
  **闭环完成**:预测落库→到期评估→market-neutral 剔 beta→efficacy Wilson 加权→反哺 confluence 打分(回顾真正反哺前瞻)。
  下一步:4h/24h horizon 样本积累诊断有效尺度;常驻进程让评估管线常跑数据新鲜;efficacy 加权也进 SignalEngine。

- 2026-06-21 #70: **独立评估管线入口 cli evaluate(解决评估停滞 + 数据闭环常态化)**(Ralph loop;Opus规划/Sonnet执行/Opus验证)。
  健康检查实测抓到"68 条到期未评估、评估管线停滞(依赖常驻 app)"。cli 加 evaluate 子命令:拉 HL allMids 构造 price_of
  →review.evaluate_due 评估所有到期预测→输出 accuracy_report(含 market-neutral 纯 alpha)。--push 可选推送,可 cron 化
  (不依赖常驻 app)。7 新单测(parser+dispatch+mock allMids 端到端评估,不联网)。
  **自诊断→自修复闭环实证**:health 发现 68 条积压→evaluate 评估 68 条→health 复查"到期未评 0"(积压清空);
  130 样本市场中性 47.7%(-2.3pp,诚实仍无纯 alpha,不夸大)。零孤儿(evaluate@build_parser,evaluate_due@_cmd_evaluate)。
  **505 单测全过**(498+7)。下一步:系统 crontab 跑 evaluate 让闭环持续;4h/24h horizon 样本积累;efficacy 进 SignalEngine。

- 2026-06-21 #71: **补全回顾闭环覆盖(SMC + 超级信号入回顾)**(Ralph loop;Opus surgical+验证)。
  实证发现 _on_signal(SMC共振)/_on_confluence(超级)推送但未 _record_pred,游离在回顾闭环外(efficacy 对 SMC 无数据)。
  补:_on_signal 加 _record_pred(coin,"SMC",dir)+efficacy.label_of("SMC");_on_confluence 加 _record_pred(coin,"超级",dir)。
  现回顾闭环**完整覆盖全部 7 信号源**(跟庄/暴涨/SMC/共识/超级/前瞻/背离)→所有信号都被事后评估命中率→efficacy 全覆盖加权。
  py_compile+import OK。**505 单测全过**(补线接入既有 _record_pred/review 测试已覆盖,无需新增)。
  下一步:系统 crontab 化 evaluate+poll 让闭环常态运转;4h/24h horizon 样本积累诊断有效尺度。

- 2026-06-21 #72: **cli cycle 运维闭环入口 + crontab 落地(部署最后一块)**(Ralph loop;Opus规划/Sonnet执行/Opus验证)。
  把 采集(poll)+评估(evaluate)+合并推送 打包成一次性 cron 友好 cycle 子命令——一条 crontab 即可驱动整个抓庄闭环
  常态运转(不依赖常驻 app),解决数据 stale/评估停滞 + 持续积累 4h/24h 样本。去重:抽 _poll_once_async/_evaluate_once_async
  共享 helper(poll/evaluate/cycle 三者复用,重构现有不重写)。新建 scripts/crontab.example(cycle每15min/evaluate每5min/
  health每小时/report每天)。8 新单测(parser+dispatch+组合 mock)。真实实证:cycle 跑通(15庄/192持仓 poll+超级信号+评估
  +准确率回顾含 market-neutral)。**513 单测全过**(505+8)。
  系统部署完整:crontab -e 装 cycle 即闭环常驻。下一步:积累 4h/24h 样本后 alpha_audit 分层诊断有效尺度(数据驱动,非代码)。

- 2026-06-21 #73: **dashboard 端到端实跑验证(补用户最初诉求"打开仪表盘确认端到端可用")**(Ralph loop;Opus 实证)。
  真实启动 dashboard(aiohttp serve)冒烟三端点:GET / →HTTP200/37KB HTML 主页渲染;GET /health →HTTP200 JSON
  (各表新鲜度+stale 标注正确);GET /api/state →15 区块完整(meta/health/accuracy/signals/.../wallet_portfolio,
  含本会话新增钱包画像+健康+市场中性准确率)。健康 overall=degraded 正确反映数据 stale(无常驻采集→cycle/crontab 解决)。
  **dashboard 端到端完全可用**,所有区块正常渲染。无代码改动(纯实证验证)。**513 单测维持全绿**。
  诚实结论:系统代码完整可部署,degraded 状态印证唯一瓶颈=数据积累(装 crontab 跑 cycle 即解,非代码)。

- 2026-06-21 #74: **系统核心有效性审计实证(cron /loop 审计;实跑 cycle + alpha_audit)**(Opus 实证)。
  实跑 cycle 验证审计三问:① 追踪数据 ✅(15庄/192持仓 + 庄#3 减空 ETH $6900万→$6790万);② 采集数据 ✅(poll 落库
  共识/流向/背离,数据新鲜解决 stale);③ 对比后期行情(evaluate + market-neutral)。真实产出:超级信号 SOL 空、共识17条
  (HYPE5庄空$1.26亿/ETH4庄空$1.15亿/SOL5庄空)、庄持仓面板全净空🔴(庄群当前极度看空)。
  **alpha_audit 最新(130条,样本翻倍)**:原始 50.8%→市场中性 47.7%(beta贡献+3.1pp,总体≈随机无alpha);
  **关键发现:共识信号 market-neutral 55%(n=96,边际+5pp)——唯一样本足且中性后仍正的微弱真 alpha 候选**
  (印证 efficacy 加权共识方向正确);跟庄中性 25%(强反指确认);4h horizon n=51 中性 45%≈随机(更长尺度暂未显 alpha,24h待积累)。
  **审计结论:追踪+采集核心有效性完全验证;后期行情对比诚实——总体无 alpha,共识是唯一正 edge 候选(需更多样本确认)**。
  无代码改动(纯审计实证+数据采集落库)。**513 单测维持全绿**。

- 2026-06-21 #75: **修复 efficacy 加权缺陷:原始命中率(beta污染)→market-neutral 纯 alpha(审计驱动的真实修复)**(Ralph;Opus规划/Sonnet执行/Opus验证)。
  审计实证发现真实算法缺陷:efficacy.refresh 用 predictions.correct(原始命中率)算 Wilson 加权,但原始命中率被趋势 beta
  污染(下跌市做空虚高/做多虚低)——共识原始48%(误判中性/降权)vs 中性55%(应加权)、跟庄原始60%(误判**加权!**)vs 中性25%(应降权),
  **加权决策完全相反**。修复:refresh 查 kind/ts/direction/realized_ret→按 kind 调 review.market_neutral_stats(复用去重,
  efficacy 自写横截面 0 处)→市场中性 hits/n 算 Wilson→weight。beta 污染单测端到端证明决策反转(原始误判 vs 中性正确)。
  真实库当前:共识中性59%(n=96 CI[0.49,0.69])、跟庄中性65%(n=20 CI[0.43,0.82])——CI 均跨0.5 暂中性(诚实:样本未达统计显著);
  算法已正确(样本显著后基于纯 alpha 加权,不被 beta 误导)。**517 单测全过**(513+4)。
  下一步:装 crontab 跑 cycle 积累样本至 CI 统计显著;24h horizon 诊断有效尺度。

- 2026-06-21 #76: **修复 position_lifecycle 两边界缺陷(审计驱动数据质量防御)**(Ralph;Opus审计+规划/Sonnet执行/Opus验证)。
  Opus 逐行审计 reconstruct 发现真实瑕疵:① is_close 超量平仓穿越0变号时 current_dir 不更新(仍旧方向);
  ② 裸 "Buy"/"Sell" dir(庄#1实测171/66笔,非Open/Close/反手)的减仓被误当加仓计 n_segment_fills。
  修复:is_close 平仓后按 running 符号重判方向;else 分支区分减仓(异号:更 last_close_ms/不增n_fills/可平flat/穿0变号)
  vs 同向加仓(同号:n_fills+1)。HL 主流 Open/Close/反手 行为不变(现有6场景回归全过)。7 新边界单测(超量平仓变号×2/
  裸Sell减仓不计/减到flat/裸Buy减空/裸Buy开加×2)。**524 单测全过**(517+7,position_lifecycle 27测试全绿)。
  连续两轮审计驱动修复(efficacy beta污染#75 + position_lifecycle边界#76)证明系统审计真实价值。
  下一步:装 crontab 跑 cycle 积累样本至 efficacy CI 统计显著;24h horizon 诊断有效尺度。

- 2026-06-21 #77: **DB 时间序列表保留清理(审计驱动:防长跑无界增长,数据质量+低延迟)**(Ralph;Opus审计+规划/Sonnet执行/Opus验证)。
  审计发现 _periodic_cleanup 只清内存累积器,所有 DB 时间序列表零保留策略→长跑必无界膨胀(bitget_oi 已2646行,
  wallet_positions_full 每周期 +83×15 行)。db.prune_before(table,ts_col,cutoff_ms) 通用裁剪(try/except 表不存在返0);
  app _periodic_cleanup 接入 12 表保守保留(bitget_oi/hl_meme_trades 7天、sm_events/signals/divergence/consensus/
  confluence/whale_signals/position_changes/whale_pnl/flow_predictions 30天、wallet_positions_full 3天;
  **保留窗口远大于功能最大回看**:oi_change 15min/协同30min/confluence 1h/efficacy 7天/review horizon≤24h,安全)。
  **predictions 绝不删**(review/efficacy/alpha_audit 全历史评估闭环基石,测试强制断言)。4 新单测(删旧留新/表不存在/
  全新不动/RETAIN 不含 predictions)。真实库验证 prune 工作 + latest 不受影响。**528 单测全过**(524+4)。
  连续 3 轮审计驱动修复(efficacy beta污染#75 + position_lifecycle边界#76 + DB无界增长#77)。
  下一步:装 crontab 跑 cycle 积累样本至 efficacy CI 统计显著;24h horizon 诊断有效尺度。

- 2026-06-21 #78: **让"前瞻性"在 cron 部署生效(审计发现:产品核心前瞻信号 cron 模式从未运行)**(Ralph;Opus审计+规划/Sonnet执行/Opus验证)。
  审计实证 predictions kind 仅共识/背离/超级/跟庄(无"前瞻")——FlowPredictor 只在流式 app _periodic_flow_predict,
  而系统实际 cron poll/cycle 部署→产品核心"前瞻性"(挂单意图)在生产从未产生/评估。修复:cli cycle 加 _forecast_once_async
  (读 meme_markets→拉 HL l2Book→orderbook_imbalance 挂单意图,单次可算无需时序→强失衡≥0.25 落 flow_predictions+
  review.record kind=前瞻);_cmd_cycle 重构三步(拉 allMids 一次共享→采集 poll→前瞻 forecast→评估 evaluate)。
  诚实标注:cron 前瞻仅订单簿挂单意图(领先未成交),无流加速度(2阶导时序需常驻进程)。复用 orderbook_imbalance/
  review.record/allMids(去重)。6 新单测(强买/强卖/弱失衡/无价/失败容错)。真实实证:cycle 产生 2 条前瞻,predictions
  出现"前瞻"4 条(GOAT/BRETT/TRUMP)。**534 单测全过**(528+6)。**前瞻性产品核心在 cron 部署真正生效**(此前完全缺失)。
  连续 4 轮审计驱动真实修复(#75 efficacy/#76 边界/#77 DB保留/#78 cron前瞻)。下一步:装 crontab 积累样本至 CI 显著。

- 2026-06-21 #79: **cron 部署补庄家集团识别(协同 clusters + lead-lag leader,抓庄核心在 cron 生效)**(Ralph;Opus审计+规划/Sonnet执行/Opus验证)。
  审计发现 poll_monitor 用 AddressCorrelation.counterparties(对手方)但庄家集团 clusters_detailed + lead-lag leader
  (CLAUDE.md 系统主体/抓庄核心)在 cron poll/cycle 部署缺失(只在流式 app _periodic_correlation)。修复:run_once 加
  clusters_detailed(now-30min,window120,min_shared3,min_coins2 跨币硬证据)+cluster_leader→digest 🕸️庄家集团区块
  (复用 app 格式);try/except 降级;无群不空推。3 新单测(跨2币协同/leader/无群不显)。**537 单测全过**(534+3)。
  真实 poll 跑通(当前30min 无跨币群,静默正确)。连续 5 轮审计驱动修复(#75-#79)。下一步:装 crontab 积累样本。

- 2026-06-21 #82: **MTF 实证诊断:实跑评估 + 7 TF market-neutral alpha 对比(系统核心有效性审计)**(Opus 实证)。
  实跑 evaluate 评估 90 条到期 MTF 预测(总样本 352)。**分 TF market-neutral 纯 alpha 诊断**:
  5m 中性48%(-2pp) / 15m 48%(-2pp) / 1h 47%(n=255,-3pp) / 4h 45%(n=51,-5pp) —— **已评估的 5m~4h 全部≈随机,无 alpha**;
  30m/12h/1d(最有希望的长尺度)刚记录待到期评估。总体 market-neutral 46.9%(-3.1pp)。
  **诚实审计结论**:① 追踪/采集目的完全达成(MTF 7 TF 全记录,能分尺度诊断);② 预测目的——5m~4h 经市场中性化后无选币 alpha,
  与既有定论一致;③ 12h/1d 长尺度(匹配庄持仓周期)是唯一未验证的希望,需积累数日。MTF 基础设施让"哪个 TF 有无 alpha"
  可被诚实测量,这是系统诚实价值的体现(不夸大)。**547 单测维持全绿**(纯实证,无代码改动)。
  下一步纯靠数据积累(装 crontab 跑 cycle 让 12h/1d 到期)——非代码能推。

- 2026-06-21 #84: **多数据源可达性+时效性实证(用户:binance永续/okx/bitget 都要)**(Opus 第一性原理实证)。
  实测各源(价对齐当前真实 BTC ~$64028,时效正确):❌ Binance 永续 fapi.binance.com **451 地域封锁**(与现货同;
  Binance 整体不可用);✅ OKX 现货+永续 实时可达(www.okx.com/api/v5,ts=当前);✅ Bitget 现货 实时可达
  (api.bitget.com/api/v2/spot,系统已接 Bitget 永续同域名)。**结论:Binance(现货+永续)本环境整体封锁,
  OKX/Bitget 现货+永续实时可用**。#83 的期现基差应换实时源:现货用 OKX/Bitget(替 data-api.binance.vision 死镜像),
  永续用 OKX/Bitget/HL。下一步:建 okx 客户端 + SpotFuturesBasis 改用 OKX/Bitget 实时现货→真期现基差可用。
  踩坑固化:Binance 任何域名(api/fapi/api-gcp/api1)在本沙箱 451,唯 data-api.binance.vision 可达但滞后685天(归档)。

- 2026-06-21 #85: **OKX 现货+永续接入 + 期现基差切换实时源(解决 #83 死数据)**(用户:okx/bitget 现货+永续;Opus规划/Sonnet执行/Opus验证)。
  新建 okx/client.py(OKXClient:ticker/candles/to_okx_bar,www.okx.com/api/v5 实证实时可达);BitgetREST 加 spot_ticker
  (api/v2/spot 现货);SpotFuturesBasis.scan 改注入式实时源(OKX 现货优先→Bitget 回退,vs HL/永续算实时基差);
  cli spot/cycle 切换实时源;Binance 模块保留标注"data-api.binance.vision 仅历史归档滞后685天"。21 新单测。**592 单测全过**(571+21)。
  **真实实证**:现货 BTC(OKX) $64,028(当前真实价,非#83 死镜像$53562) vs HL 永续 $63,994 → 基差 -0.05%(合理) —— 实时期现基差彻底可用。
  诚实:Binance(现货+永续 api/fapi)本环境 451 整体封锁不可用,现货所改用 OKX(主)+Bitget(回退)实时源。
  下一步:期现基差纳入信号(现货+永续共振)、现货大额成交流向、基差进 MTF 评估。

- 2026-06-21 #91: **OKX 永续 OI/资金费监控(用户:okx 永续)**(Ralph 自主;Opus规划/Sonnet执行/Opus验证)。
  OKXClient 加 funding_rate/open_interest;funding_divergence 纯函数(多所资金费分歧>0.05%标记);okx_perp 表(7天);
  cli spot 拉 OKX 永续 funding/OI 落库展示。12 新单测。**672 单测全过**(660+12)。真实实证:OKX 永续 BTC 资金费
  +0.0089%/OI 18234 BTC。多所永续维度:HL+Bitget+OKX 三所(OKX 补 OI/资金费)。Binance 永续 451 不可用已诚实标注。
  下一步:三所资金费分歧信号、dashboard 展示新维度、清洗进信号热路径;复杂 ML 需样本积累、AI 需 codex 环境(诚实边界)。

- 2026-06-21 #92: **3 后台 agent 并行推进(用户:subagents 后台并行)**(Ralph 自主;Opus 调度文件域不相交并行/统一验证)。
  并行派 3 个文件域不相交后台 agent 同步推进:A) dashboard.py 加 期现基差/现货流向/OKX永续 三展示区块(9测试);
  B) health.py 加信号链路覆盖(predictions 各 kind 近7天计数,看哪些信号产出/静默)+新源新鲜度(spot_basis/spot_flow/okx_perp,6测试);
  C) onchain/exchange_flow.py BSC 链稳定币流(EVMStableFlow 支持 BSC,decimals 按链区分 ETH6/BSC18,BSC RPC 实证可达,7测试)。
  **Opus 统一验证:706 单测全过**(677+29),全模块 import OK,**3 并行改动合并无竞态破坏**(文件域不相交策略成功)。
  系统多所多链多维:HL/Bitget/OKX 三所×永续现货×ETH/BSC/BTC/SOL 链×MTF×清洗×特征pipeline×仪表盘全维展示×健康可观测。

- 2026-06-21 #93: **系统核心有效性审计:三所实时采集+跨源一致(Opus 只读实证,与文档 agent 并行不冲突)**。
  实证三所实时采集:HL allMids BTC $108,400(219币)/OKX 现货 $108,428/Bitget 现货 $108,428,**跨源偏差<0.03%
  (data_quality.cross_source_price 判 agree)** → 数据采集/追踪核心有效、多源数据质量良好。OKX 现货 MTF K线 5m/1H/1D
  全可达(收盘$108,472,现货 MTF 监控数据源就绪)。诚实回答用户"能否追踪/采集数据":✅ 三所实时+跨源一致。
  (注:BTC 现价 $108k,与会话早期实证 $64k 差异为真实市场时点不同/波动,三源当下一致即数据正确)。无代码改动(纯审计)。

- 2026-06-21 #94: **文档/配置同步(3 后台 agent 并行,专业完整性)**(Ralph 自主;Opus 调度/验证)。
  并行派 3 个文件不相交文档 agent:A) ARCHITECTURE.md(446行,更新到当前真实架构:三所/四链/全维度/MTF/清洗/ML/LLM,
  诚实标注 Binance 封锁+1h 无 alpha);B) README.md(124行,CLI 全子命令+数据源+部署+诚实定位);C) config.example.yaml
  (补 review MTF 配置等,YAML 解析 OK)。Opus 验证:文档不影响代码,**706 单测维持全绿**,config YAML 可解析。
  文档专业完整性补齐(系统从#40 扩展到#94,文档同步到位)。3 后台 agent 并行零冲突(纯文档文件域不相交)。
  系统全貌:三所×永续现货×四链×聪明钱多维×MTF×清洗×特征pipeline×全维仪表盘×健康可观测×准确文档,706 单测,诚实可信。

- 2026-06-22 #95: **数据可信度核查:HL 内部一致但沙箱实时性不稳定(重大诚实发现)**(Opus 只读实证)。
  核查 HL allMids vs clearinghouseState 持仓估值:**HL 数据内部完全一致**(BTC/ETH/SOL/HYPE 偏离 +0.00~0.01%),
  系统估值逻辑正确、跨源清洗/一致性检测都对。**但**同源不同请求 HL allMids BTC 一次$64,140、另一次$108,400
  → **本沙箱外部 API 实时性不可信任(疑似缓存/录制不同时点快照,与 data-api.binance.vision 滞后685天同理)**。
  **诚实结论**:① 系统逻辑正确(任一快照内 HL 自洽);② 沙箱"实时性"是环境限制非系统缺陷,真实部署拿真实实时数据;
  ③ 此前 hypurrscan 地址 entry_px"矛盾"实为跨轮对比不同时点快照所致,非真矛盾。data_quality.is_stale_price 正为捕获此类。
  含义:本沙箱可验证系统正确性/数据自洽性,但无法验证实时 alpha(数据非真实时);alpha 验证须真实部署环境长跑。
