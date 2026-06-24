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
- [~] C1 SFG-KNN 收口:**10 因子审计闭环完成(loop#5)** —— 平价 10/10 正确、no-lookahead 10/10 安全
  (lrsd/atr2/gpi/pivot/ai_st/dmha/pdbb/vap/ami ✅ + msfvg 公式对但 warmup gate 分歧);fail-closed + numpy
  warning 已修(loop 前)。**剩余待修**:① msfvg `if i<warmup:continue` per-row mask 去除(对齐 Rust whole-batch);
  ② gpi/msfvg golden 测试改为驱动生产 `*_series`(现仅测公式副本);③(可选)lane A/B1/B23/C 代码审(测试已全绿)。
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
