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
- [~] B1 谐波 K线 WS 增量实时:**核心完成(loop#5,Sonnet 执行)** —— 新增 `monitor/harmonic_candle_ws.py`
  (收盘线即 to_thread 落库+analyze,WS 回调非阻塞)+ config `realtime_ws` 开关(默认 false)+ 32 测试,**全量 1850
  passed**。**Opus 验证发现 2 gap → 派 Sonnet 收口(af3d9f99)**:① 热路径 O(n) 反向映射重建(全币种违纪)→
  预建 O(1);② on_update 未接 insert_harmonic_setups(实时算了不落库=空转)→ app.py 填回调端到端落库 +
  落库协调(不破坏 recent_harmonic_setups 全量快照)。
- [x] B1 **完成(loop#6,Opus 验证 + 固化 d4cbfca)**:2 gap 已收口(O(1) 预建映射 + on_update 端到端推送);
  落库协调=实时层只推送通知不写库(保 dashboard 全量快照),**全量 1859 passed**。诚实:页面实时读取(per-coin
  latest)是 B2 单独做。known limit:all_perp+harmonic_collected 动态加币需重新 attach(预存架构限制)。
- [ ] B2 dashboard 谐波页从 5s 轮询 → 更实时(SSE/WS 推送或更短轮询 + LIVE 脉冲)。
- [ ] B3 forming 形态实时逼近 PRZ 告警(harmonic_forward / forming_approach 已有骨架,接全币种)。

### C. 前瞻预测(基于 bitget 永续数据,结合开源/模型)

> **谐波前瞻性方法对标(loop#6,模型知识 + 开源/学术)** —— 用户核心要求「指标形态前瞻性预测性」。
> 谐波本质是回看(XABCD 的 D 已发生),让它「前瞻」的业界路径(由弱到强,诚实标注):
> 1. **forming 实时投影**(B3):XABC 完成、D 未到 → 从 A 投 d_xa + 从 C 投 cd_bc 双估 D(PRZ),
>    价格逼近 PRZ 实时告警 = 真提前量(对标 Pelletier/Carney PRZ 重叠区,已有 `harmonic_forward`/`forming_approach` 骨架)。
> 2. **多周期 PRZ 共振**:同币多 tf 谐波 PRZ 重叠 = 更强前瞻(对标 SMC 多 TF confluence;smc 已有多周期 S/R)。
> 3. **前瞻确认叠加**(C2,已实现 `forward_confirm`):PRZ + OI 方向化 + funding 极值 + OFI(Cont-Kukanov-Stoikov)
>    —— 订单流/资金流**先于价格**(领先信号),是谐波回看 → 前瞻的核心补强。接全币种。
> 3b. **订单流前置 PRZ**:PRZ 附近 OFI/挂单意图异常 = 反转前置确认(absorption 背离,C5 可选)。
> 4. **形态完成/反转成功率**:历史相似形态统计(KNN-SFG,C1 已做)—— **诚实:项目自承 ≈随机(EMH),
>    不预设 alpha,作展示 + review 闭环实测**,不夸大(PLAN.md「高 lift≠赚钱」纪律)。
> 落地优先级:C2 全币种 forward_confirm → B3 forming 逼近 → C3 review 闭环度量真实命中率。
- [x] C1 **SFG-KNN 审计完全闭合(loop#11,Sonnet 收口 + Opus 验证,全量 1874 passed)**:10 因子平价 10/10
  + no-lookahead 10/10;fail-closed + numpy warning 已修;**2 待修已修**:① msfvg per-row warmup mask 移除 →
  whole-batch guard(min_bars=2*swing+1,对齐 Rust,早期有 zone 就发射,no-repaint 保持);② gpi/msfvg 新增
  TestProductionPathGolden 直接驱动 `*_series`(原 golden 只测公式副本)。lane A 地基(loop#11)+ B3 协同(loop#8)已审。
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
- [x] D3 **完成(loop#7-8,Sonnet 执行 + Opus 验证/修复,commit 020fe77)**:dashboard.py
  `_HARMONIC_DETAIL_TEMPLATE` 原生重写 —— 浅色金融终端三栏(262/主/372)+ 16 设计 token + IBM Plex
  fallback(无 CDN)+ KPI strip + LIVE 脉冲 + 全币种列表(搜索/过滤/排序/分页 50)+ SVG 配色升级;
  数据接入不变;**全量 1859 passed**。**Opus 诚实修复**:KNN 卡原用 Math.random 伪造百分比/dots →
  改为只展真实 knn 标记(零伪造)。排版自查(curl 结构级:三栏/KPI/卡片/字体/列表完整,{{ 残留=0)。
  ⚠️ 视觉截图:浏览器扩展无法渲染 localhost,建议用户本地 http://127.0.0.1:8787/harmonic2 确认。

### H. HL 系统(聪明钱地址追踪)—— 用户「完善完整开发 hl 系统」(与谐波并列)
- [x] H1 **评分/协同确定性强化已实现**(WF B2/B3,commit 2e4575d):`smart_money_score` 魔数外置 config +
  最小样本守卫(Wilson 下界);`address_correlation` 协同加显著性(二项/超几何 null model → lift/p-value)+
  计数按活跃度归一(消高频偏向)。**B3 协同已审通过(loop#8)**:cooccur_stats null model 正确
  (expected=a·b/total,二项右尾 p-value log 空间防下溢 + 大 n 正态近似,n=0 守卫)+ 测试诚信
  (随机追涨人群被过滤、真协同保留,非走过场)。B2 评分配置化待审(测试全绿)。
- [ ] H2 **HL 前端落地**:设计稿聪明钱追踪部分(line 67-286)原生重写 —— 三栏:左 `coins`(币+净流向)/
  中 大图+`flowBars`(净主动流)+`coinPositions`(庄持仓)/右 `consensus`/`consTable`+`divergence`+`whales`+
  `events`+`onchain`+`breaks`(挂单墙)。复用 D3 设计 token(同终端)+ 现有 dashboard HL API(数据现成)。D3 谐波页后做。
- [ ] H3 **HL 前瞻**:资金流加速度(2阶导平滑)/OI 方向化速度/挂单意图(WF C 路线 OFI/oi_velocity/funding 已实现)
  接 HL 聪明钱信号 —— 从「回看庄做过什么」→「前瞻资金正往哪 positioning」(CLAUDE.md 产品方向)。
- [ ] H4 系统 tab 统一:设计稿 header 的 [HL 系统 | 谐波系统] 切换 → 统一 SPA(D2 合并,原生)。

### E. 固化与门禁
- [x] E1 **固化全绿工作**(4 commit 在 feat/sfg-knn-harmonic-loop:2e4575d/98456c1/d4cbfca/020fe77,全程 1859 全绿)。
- [ ] E2 **部署门禁检查清单**(用户条件:本地无 bug + 全接入 + 排版 OK 再部署 → 须批准):
  1. ☐ 全量 `pytest -q` 全绿(当前 1859 passed;C1 收口后复跑)。
  2. ☐ 零孤儿:新模块全接入运行时(`harmonic_candle_ws`/`sfg/*`/`supervisor`/`microprice`/`cooccur_stats`
     已接;grep 自查 `realtime_ws`/`universe_mode` 等开关可达)。
  3. ☐ 网页排版无问题:**用户本地浏览器**确认(`-m smc_tracker dashboard` → :8787/harmonic2,
     三栏对齐/KPI/蜡烛图配色/全币种列表/窄屏堆叠;Claude 浏览器扩展无法渲染 localhost,须人工)。
  4. ☐ `py_compile` 全通过。
  5. ☐ 无伪造数据(Math.random 等已清;诚实标注 ≈随机/缺数据)。
  6. ☐ 审计待办闭合:**lane A 地基已核验通过(loop#11)**(busy_timeout=5000+cache/temp_store、supervise
     包裹每 periodic 任务+return_exceptions、推送队列 maxsize=2000+QueueFull、harmonic_setups 双索引);
     **B3 协同已审(loop#8)**;C1 收口中;B2 评分配置化待审(测试全绿,可选)。
  7. ☐ 全永续实时压测:`universe_mode=all_perp` + `realtime_ws=true` 本地跑,观察 Bitget 限流/WS 稳定/内存。
  8. ☐ **用户明确批准** → 合并 main → 推 GitHub → 部署服务器(memory:部署须批准)。

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
- 2026-06-24 loop#4(Opus 规划,与 B1 后台执行并行零冲突):固化 A2(commit 98456c1);派 Sonnet
  后台执行 **B1 谐波实时化**(adce12f7,candle WS 增量驱动,K线级实时);**D 设计系统提取**完成 →
  写 `D3-harmonic-page-design-system.md`(设计 token + 谐波页三栏蓝本 + 数据映射 + 无依赖落地要点,
  为 D3 原生重写铺路)。下一步:B1 完成核验 → D3 谐波页重写(Sonnet)或 C1 SFG 收口。
- 2026-06-24 loop#5(Opus 只读审计,与 B1 后台执行并行零冲突):**vap/ami 深审完成 → 10 因子审计闭环**
  (平价 10/10 正确、no-lookahead 10/10 安全;vap 严格因果 rolling、ami trailing channel + 短序列退化已标注)。
  C1 待修项明确(msfvg warmup gate + gpi/msfvg 弱测试驱动生产路径)。下一步:B1 核验 → C1 收口或 D3 重写。
- 2026-06-24 loop#6(Opus 规划,与 B1 收口 af3d9f99 后台并行零冲突):B1 独立验证(1850 全绿)→ 抓 2 gap
  (热路径 O(n) + on_update 空转不落库)→ 派 Sonnet 收口;**C 前瞻方法对标**(模型知识+开源:forming 实时投影/
  多周期 PRZ 共振/forward_confirm 订单流领先/完成率诚实≈随机)写入 C 段,指导 C2/B3/C3。下一步:B1 收口核验
  → C2 全币种 forward_confirm 或 D3 谐波页重写。
- 2026-06-24 loop#7(Opus 规划,与 D3 后台执行并行零冲突):B1 完成验证(1859 全绿)+ 固化(d4cbfca);
  派 Sonnet 执行 **D3 谐波页原生重写**(a10efa1c,设计系统三栏 + 全币种列表);**补 HL 系统段 H1-H4**
  (回应用户「完善 hl 系统」并列要求:H1 评分/协同强化已实现待审,H2 HL 前端落地,H3 HL 前瞻 positioning,
  H4 系统 tab 统一)。下一步:D3 核验 + 截图自查排版 → C1 收口 / C2 全币种 / H2 HL 前端。
