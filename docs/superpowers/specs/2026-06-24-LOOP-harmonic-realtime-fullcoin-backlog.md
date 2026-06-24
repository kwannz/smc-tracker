# LOOP 接力 backlog — 谐波全币种 + 实时动态 + 设计稿落地 + 前瞻

- 日期：2026-06-24（/loop cron 3c246182,每 5min 驱动）
- 愿景（用户 loop prompt）：结合 SMC 聪明钱追踪终端 html 设计稿,完善 HL + 谐波系统;
  谐波**监控全部 bitget 永续合约币种** + **实时追踪/最新实时动态** + **前瞻预测**(基于 bitget 永续数据);
  结合开源+模型知识;**Opus 规划 Sonnet 执行**;**本地无 bug + 全接入 + 网页排版 OK 再部署**(部署须批准)。
- 每次 loop：读本文件 → 取第一个未完成项 → Opus 规划/Sonnet 执行推进 → 勾选 → 追加迭代日志。

## 实证现状(第一性原理,已核)
- 谐波监控限 `top_n` 币(`harmonic_monitor.py:116` `coins[:top_n]`),**非全永续**。
- Bitget `rest.contracts()` 可拉全 USDT-FUTURES 永续(能力具备,~2747 币)。
- 谐波 = refresh 周期型 DB 缓存(`dashboard` 5s 轮询),**非 tick 级实时**。
- 设计稿 = React DSL(`support.js` 运行时 + window.React),与 smc「无 CDN/无依赖」约束冲突。
- SFG-KNN(WF3)已落地 + fail-closed 修复(1798 测试全绿,**未提交**);WF4 审计部分完成(msfvg/弱测试待修)。

## 接力清单(按依赖排序,逐 loop 推进)

### A. 谐波全币种监控(用户明确:全部 bitget 永续)
- [ ] A1 实证全永续完整谐波检测的性能/限流(2747 币 × 多周期 × XABCD 枚举耗时);定分批/优先级策略(按 vol/OI 分层 + 异步并发 Semaphore)。
- [ ] A2 `top_n` → 全永续(或分层:核心实时 + 长尾轮询);universe 用 `contracts()` 动态拉取,保留 vol 排序。
- [ ] A3 DB schema / dashboard 列表支持全币种(分页/过滤/搜索)。

### B. 实时动态(用户强调:谐波形态最新实时)
- [ ] B1 谐波从「周期 refresh DB 缓存」→ 增量实时:接 Bitget K线 WS(`bitget/ws_client.py` 已支持任意 channel),收线即增量更新谐波 pivot/XABCD(复用 B1 已修的 append-only swing,no-repaint)。
- [ ] B2 dashboard 谐波页从 5s 轮询 → 更实时(SSE/WS 推送或更短轮询 + LIVE 脉冲)。
- [ ] B3 forming 形态实时逼近 PRZ 告警(harmonic_forward / forming_approach 已有骨架,接全币种)。

### C. 前瞻预测(基于 bitget 永续数据,结合开源/模型)
- [ ] C1 SFG-KNN 收口:修 WF4 审计发现(msfvg warmup gate、弱测试加强、vap/ami 深审、lane A/B1/B23/C 审计)。
- [ ] C2 forward_confirm 接全币种(OI 速度/funding 极值/OFI 已实现,接谐波全币种宇宙)。
- [ ] C3 review 闭环按 asset_class/horizon 出谐波前瞻命中率(诚实度量,不夸大)。

### D. 设计稿落地(SMC 聪明钱终端 + 量析终端 → 真实前端)
- [x] **数据契约提取(loop#3)**:设计稿数据绑定 ≈ smc 现有 API **1:1 对应**(设计稿基于 smc 数据能力设计)。
  映射:HL 系统 `whales/coinPositions/consensus/consTable/divergence/events/onchain/flowBars/breaks/hlStats`
  → smc 聪明钱/共识/背离/链上/净流向 API(dashboard 已有);谐波系统 `coinsHarm/harmList/harmStats/candles/
  xabcd/fibs/fibRows/fvgs/sigFactors/indRows/KNN` → smc `build_coin_detail`(candles/setups含XABCD/sr)已产出;
  量析终端 `watchlist/candles/volBars/positions/signals/stats/indRows` 同源。**落地=表现层,数据层现成。**
- [x] **D1 决定(工程约束,loop#3)**:采用**方案3 = 设计 token 提取 + 原生重写**。理由:CLAUDE.md
  「无 CDN/无依赖」是 checked-in 硬约束(部署稳定),**排除 React DSL+CDN 方案**;数据契约已对齐 smc API,
  现有 aiohttp dashboard.py 内联基座可用。提取设计语言(IBM Plex 字体、浅色金融终端配色 `--bg:#eef3fa
  --blue:#2563eb --long:#16a34a --short:#e23744`、卡片、三栏)→ 原生重写。(如用户坚持 React 版可改。)
- [ ] D2 合并两终端为统一前端(系统 tab:HL / 谐波 / 量析),原生 HTML/CSS/JS,接 dashboard 真实 API。
- [ ] D3 **谐波页优先**用新设计重写(浅色金融终端风 + IBM Plex + 三栏:harmList 侧栏 / 大蜡烛图含 XABCD-PRZ-FVG /
  右栏 Setup+S-R+KNN),接 `build_coin_detail` + 实时刷新(配合 B1)。**与 A2/B1 协同后做(避免 dashboard 竞态)**。

### E. 固化与门禁
- [ ] E1 固化已完成全绿工作(SFG-KNN + fail-closed,本地 git 提交,非部署)。
- [ ] E2 部署门禁:本地全绿 + 全接入 + 网页排版无问题 → **用户批准** → 部署服务器。

## 迭代日志
- 2026-06-24 loop#1(Opus 主循环规划):实证谐波现状 gap(top_n 非全币种 / DB 缓存非实时);
  建本 backlog 作接力锚点。SFG fail-closed 修复完成(1798 全绿)。会话限制(重置 01:40)中,
  Sonnet workflow 执行留待后续 loop。下一步:A1(全永续性能实证)或 E1(固化全绿工作)。
- 2026-06-24 loop#1.5(固化):全绿工作提交特性分支 feat/sfg-knn-harmonic-loop(commit 2e4575d,
  65 文件 +14088,SFG-KNN+fail-closed+specs),未合并 main/未推 GitHub/未部署。
- 2026-06-24 loop#2(Opus 规划 + Sonnet 执行):**A1 实证完成**(analyze_candles 1.42ms,全永续 CPU
  仅 23s 不是瓶颈,瓶颈是 I/O 冷启动 ~10min;**全永续可行无需砍 top-N**)→ 勾选 A1;定 **A2+B1 统一
  架构**(WS 增量驱动全永续谐波引擎,同满足全币种+实时动态)。派 Sonnet builder 后台执行 **A2**
  (universe top_n→all_perp 可配置 + 冷启动回填,向后兼容,TDD)。下一步:A2 结果核验 → B1 实时化。
- 2026-06-24 loop#3(Opus 规划/验证 + Sonnet 执行):**A2 完成**(Sonnet 后台 agent + Opus 独立验证:
  全量 **1818 passed 零回归**,resolve_universe 契约正确)→ 勾选 A2;**D 数据契约提取**(设计稿数据绑定
  ≈ smc 现有 API 1:1,落地=表现层)+ **D1 决定方案3**(设计 token 提取 + 原生重写,无依赖约束排除 React CDN);
  **B1 现状实证**(bitget ws_client 已支持 candle1m channel;现状 candle_collector REST 轮询 + periodic refresh
  非实时)。下一步:固化 A2 → 派 Sonnet 执行 **B1 谐波实时化**(candle WS 增量驱动)。
