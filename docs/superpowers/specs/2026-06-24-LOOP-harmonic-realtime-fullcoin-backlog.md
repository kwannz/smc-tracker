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
- [x] A1 **完成(loop#21,Opus 规划 + Sonnet 并行执行,全量 2001 passed)**:实证 Bitget K线接口
  `_SEMA=4` 多轮 **0 次 429**(系统所有 429 来自 HL wallet_portfolio,与谐波无关)→ `_SEMA_LIMIT` 4→8
  (实测 8 无 429,10 偶发,保守取 8,冷启动回填提速 ~2x);分批/优先级策略见 A2 分层。**前置真 bug 修复**:
  全永续 WS 重连风暴(单条巨型 subscribe 被 Bitget 断链,实测 29 次/83 行)→ ws_client `_send` 分批(≤50/批),
  风暴 29→0,数据流恢复(DB 36→179 持续回填)。
- [x] A2 **完成(loop#21,Sonnet 执行)**:分层调度(业界 hot/warm/cold tier 标准)—— 核心层(高 vol 前
  `core_n=60`)每轮全量 refresh(实时性);长尾层 round-robin 分 `tail_shards=8` 片每轮处理 1 片(降单轮请求量 +
  保核心实时);`core_n>=总数`退化为全量每轮(向后兼容)。config.py 加 core_n/tail_shards;refresh 内 `_round`
  计数驱动分片;20 确定性单测(核心每轮必含/长尾 N 轮全覆盖不重不漏/退化/665 币极端)。universe 既有
  resolve_universe 全永续 + vol 降序。
- [x] A3 **完成(loop#21,Sonnet 执行)**:`/api/harmonic/list` 加分页(offset/limit,默认 50 最大 500)+
  搜索(q 关键词)+ 过滤(asset_class),响应改 envelope `{items,total,offset,limit}`;前端 refreshList 用
  `Array.isArray(d)?d:(d.items||[])` 兼容新旧格式;31 单测 + 端到端 curl 验证(分页/搜索 q=BT→BTC/页面 200 47KB/
  内联 JS `node --check` OK)。

### B. 实时动态(用户强调:谐波形态最新实时)
- [~] B1 谐波 K线 WS 增量实时:**核心完成(loop#5,Sonnet 执行)** —— 新增 `monitor/harmonic_candle_ws.py`
  (收盘线即 to_thread 落库+analyze,WS 回调非阻塞)+ config `realtime_ws` 开关(默认 false)+ 32 测试,**全量 1850
  passed**。**Opus 验证发现 2 gap → 派 Sonnet 收口(af3d9f99)**:① 热路径 O(n) 反向映射重建(全币种违纪)→
  预建 O(1);② on_update 未接 insert_harmonic_setups(实时算了不落库=空转)→ app.py 填回调端到端落库 +
  落库协调(不破坏 recent_harmonic_setups 全量快照)。
- [x] B1 **完成(loop#6,Opus 验证 + 固化 d4cbfca)**:2 gap 已收口(O(1) 预建映射 + on_update 端到端推送);
  落库协调=实时层只推送通知不写库(保 dashboard 全量快照),**全量 1859 passed**。诚实:页面实时读取(per-coin
  latest)是 B2 单独做。known limit:all_perp+harmonic_collected 动态加币需重新 attach(预存架构限制)。
- [x] B2 **完成(loop#18,Sonnet 执行 + Opus 验证,全量 1891 passed)**:`recent_harmonic_setups` 改 per-coin
  latest(`WHERE (coin,tf,ts) IN (SELECT coin,tf,MAX(ts) GROUP BY coin,tf)`,走 ix_harmonic_coin_ts);B1 实时层
  `_on_harmonic_ws_update` 恢复按币落库(delete_harmonic_coin_tf 删旧 + insert 新);batch_ts 修正;消费方
  (build_harmonic_list/build_coin_detail)兼容验证;7 测试。**谐波形态最新实时动态端到端达成**(WS 收盘→按币落库→
  页面读最新)。**诚实权衡**:开 realtime_ws(默认 false)时 delete 删该币历史 → harmonic_history 退化为最新
  (默认 false 无影响;方案 b 独立 realtime 表可两全,后续按需)。
  --- 原方案(已执行): 核心 = 解 B1 留的 gap
  (实时层只推送不落库,因 `recent_harmonic_setups` 用 `WHERE ts=MAX(ts)` 全量快照,单币落库会塌列表)。
  **方案 a(选)**:`recent_harmonic_setups` 改 **per-coin latest** —— `WHERE (coin,tf,ts) IN (SELECT coin,tf,
  MAX(ts) GROUP BY coin,tf)`(或窗口函数),每币各自最新而非全局 MAX(ts);则 B1 实时层可恢复**按币落库**
  harmonic_setups(on_update → insert),页面读每币最新 = 真页面级实时。**风险**:① 验证 dashboard 列表/详情
  对「不同币不同 ts」兼容(现假设同 ts 快照);② periodic 全量落库与实时单币落库共存不重复(harmonic_setups
  已加 ix_harmonic_coin_ts 索引,A5);③ 全永续下 per-coin GROUP BY 性能(索引覆盖)。**方案 b 备选**:独立
  `harmonic_realtime_setups` 表,dashboard 合并读(realtime 优先)。TDD:per-coin latest 读取 + 实时落库不塌列表 +
  性能。**改 db.py(读取)+ harmonic_candle_ws on_update(恢复落库)+ dashboard 调用**,与 H2 串行(都碰 dashboard)。
- [x] B3 **forming 实时逼近 PRZ 完成(loop#12,核验已实现 + 补测试,全量 1877 passed)**:
  `FormingApproachTracker`(forming_approach.py:per-entry TTL 缓存 + 结构指纹冷却去重 + 穿越作废 + 纯内存
  热路径)+ app.py `_periodic_prz_approach`(15s worker,非 WS 回调)+ `_seed` 建 + PRZ 缓存 update,**已接入运行时**。
  补 3 测试(热路径无同步写库 + band_pct 0.8% 提前触发 + bear 作废)。**诚实纪律**:forming 投影时不记预测,
  价实时触达 PRZ 才记 `_record_pred("谐波-逼近")`(QA H1,避免方向随机漂移)。**真前瞻提前量达成**。

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
- [x] C2 **已随 A2 + WF3 自动达成(loop#13 核验)**:`apply_forward` 接入 harmonic_monitor(line 183),
  **completed+forming 都施加** forward_mult(解除 completed 门控,QA §3);forward provider(HarmonicForwardSignals
  + build_profile)接 harmonic_monitor 的 coin_to_symbol=harm_c2s,**随 A2 universe_mode 全永续**。诚实降级:
  长尾币若 oi_monitor 未覆盖 → 缺 OI/funding → forward_mult 中性(1.0,不佯装)。可选增强:扩 oi_monitor 全永续。
- [~] C3 **核心已达成(loop#14 核验)**:谐波预测落 predictions 表(`build_harmonic_predictions` completed
  「谐波-反应式」+ B3 forming「谐波-逼近」,app.py `_record_pred` 接入);`accuracy_report` 有 `by_kind`
  (区分谐波-反应式/逼近的命中率/edge/avg_ret)+ `by_horizon`(时间尺度)+ `market_neutral_stats`(去 beta,
  诚实剔趋势)+ 离群守卫。**小 gap**:`by_asset_class`(crypto/tradfi 分桶)未实现 —— 可选增强(by_kind 已够区分)。
- [x] C4 **OI 加速度/背离领先信号(loop#21,Sonnet 执行)**:harmonic_forward.py 强化前瞻领先因子(CLAUDE.md §二
  「优先领先信号:OI 速度先于价格」)—— 每币定长 OI/price deque(maxlen=20,内存有界)+ `_oi_acceleration`
  (2 阶导,tanh 归一封顶 [-1,1],样本不足→0 中性)+ `_oi_price_divergence`(OI 涨>3% 价滞<1% 或 OI 涨而价反 setup
  方向→负信号)+ `_composite_oi_signal`(速度 0.4/加速度 0.3/背离 0.3 加权 tanh 封顶);`__call__` 4-tuple 接口不变
  (向后兼容 apply_forward);缺数据始终中性(首帧 0.0,无 OI→None,诚实铁律)。27 确定性单测(方向/封顶/缺数据中性/
  背离/向后兼容)。诚实:领先信号辅助,非投资建议;长尾币 oi_monitor 未覆盖则中性降级。

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
- [ ] D2 **量析终端规范(loop#18,次要,谐波+HL 完成后做)**:新增 `/quant` 页(同 /hl2 模式,复用 D3 token)——
  三栏:左 `watchlist`(币+价+涨跌)/ 中 symbol header + price chart(SVG K线)+ volume bars + indicator subchart(MACD/RSI/KDJ tab)
  + positions / 右 signals + indicator gauge。数据映射:`candles/volBars`→build_coin_detail；`indRows`→指标引擎(technical.py)；
  `signals`→信号；`positions`→whale_positions。**order panel 跳过**(smc 无 API key 不下单,诚实)。系统 tab 三页统一
  (/hl2↔/harmonic2↔/quant)= H4 完整。零 Math.random,无 CDN。**与 B2 串行**(都碰 dashboard)。
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
- [x] H2 **完成(loop#16,Sonnet 执行 + Opus 验证,全量 1884 passed)**:新增 `/hl2` 路由 + `_HL_TEMPLATE`
  (735 行)+ `render_hl_html`,设计稿聪明钱追踪三栏(左 whale_flows 币+净流向 / 中 净流向 SVG bars + 地址排行 +
  庄家集团 / 右 鲸鱼动作+共识+背离+挂单墙+链上),复用 D3 设计 token + IBM Plex,**数据全真实**(whale_flows/
  top_addresses/clusters/whale_signals/signals/divergence/okx_walls/onchain),per-coin 详情标「暂无数据」**零
  Math.random 伪造**;系统 tab 链 /harmonic2;不动现有主页 /(保守降风险);7 新测试。curl 自查:三栏/token/{{残留=0}。
- [x] H4 系统 tab **双向互链完成(loop#21,Sonnet 执行)**:谐波页 header「HL 系统」tab href `/`→`/hl2`,
  与 HL 页「谐波系统」tab(→`/harmonic2`)形成双向切换;端到端验证 `/hl2` 含 `href="/harmonic2"`、`/harmonic`
  含 `/hl2`。完整单页 SPA 合并(line 140 / D2)为可选增强,当前 tab 跳转已满足 [HL|谐波] 切换诉求。
- [~] H3 **HL 前瞻已基本达成(loop#15 核验)**:`flow_predictor`(前瞻资金流:挂单意图+流加速度,app.py:140)
  + `_periodic_flow_predict`(30s,oi_directional_velocity 方向化 OI,C.3)+ `_periodic_divergence`(60s,资金费⟂
  净流向背离)全接入 HL periodic = 「前瞻资金正往哪 positioning」。可选增强:flow_acceleration EMA 平滑(C 路线已规划)。

> **loop#15 真实剩余工作盘点**(连续核验后收敛):★绝大多数路线核心已实现+审计★。真未做的仅:
> - **B2**(谐波页 per-coin latest 实时读取):`recent_harmonic_setups` 仍 `WHERE ts=MAX(ts)` 全量快照(B1 留)→
>   改 db+dashboard 让实时层可按币落库+页面读最新(等 H2 完成避竞态)。
> - **D2**(量析/行情分析终端):无路由/页面(次要,用户优先谐波+HL)。
> - **H4**(系统 tab 统一 /hl2↔/harmonic2):H2 落地时带上。
> 其余(A/B1/B3/C1/C2/C3核心/D3/H1/H3 + 全部审计)均已完成。**系统功能完成度远高于初始 backlog 勾选数**。
- [ ] H4 系统 tab 统一:设计稿 header 的 [HL 系统 | 谐波系统] 切换 → 统一 SPA(D2 合并,原生)。

### N. 用户新需求(loop#18 本地截图后追加)
- [~] N1 **谐波多周期(15m/30m/1h/4h/12h/1d/1w 每币)**(agent aa01d2506 执行中):根因 = `handle_harmonic_discover`
  硬编码 `["4H"]` 单周期 → 改全 7 周期;config `harmonic.timeframes` 的 `6H`→`30m`(对齐用户精确 7 周期)。
  前端 tf tab 数据驱动,7 周期 setup 生成后自动显示。Bitget 全 7 周期支持。
- [~] N2 **HL 页 /hl2 数据加载修复**(同 agent):截图 KPI 全 `—`/「加载中」,但 `/api/state` 有 top_addresses(3)
  等真实数据 → JS 加载/解析问题,修到能显示现有真实数据(空区块诚实「暂无数据」不伪造)。
- [ ] N3 **补充实时数据(运行架构)**:dashboard 是**只读 DB 独立进程**,数据靠 app.py 监控进程实时填。
  现状谐波页有数据=手动 discover;HL 页空=没跑 HL 监控。**方案**:① 文档化「同跑 app.py 监控 + dashboard 展示」
  (标准架构,实时数据持续);② dashboard 内嵌轻量周期刷新(谐波 discover + ticker,单进程即有实时,适合演示/本地)。
  待 N1/N2 完成后定方案。**诚实**:代码改不出凭空数据,实时数据=监控进程跑。
  **方案① 标准架构(服务器部署用)** —— 同跑两进程:
  ```
  # 终端1: 监控进程(实时填 DB,全永续+谐波多周期+HL 聪明钱)
  PYTHONPATH=src ./.venv/bin/python -m smc_tracker.app --config config/config.yaml
  # 终端2: dashboard(只读 DB 展示,5s 轮询)
  PYTHONPATH=src ./.venv/bin/python -m smc_tracker dashboard --port 8787
  ```
  服务器用 systemd/supervisor/nohup 守护两进程;config 设 harmonic.universe_mode=all_perp + realtime_ws=true 启全永续实时。
  **方案② 演示便捷(本地看效果)**:dashboard 启动时内嵌轻量周期任务(谐波 discover + ticker 刷新),单进程即有数据
  (碰 dashboard,待多周期固化后做,避免与 N1 竞态)。本地快速看:先 `dashboard` 起页面 → 点「🔍发现搜集币种」手动触发谐波多周期。

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
- 2026-06-25 loop#21(Opus 规划/审计 + Sonnet 并行执行,workflow 文件零冲突):**先修全永续 WS 重连风暴真 bug**
  (systematic-debugging:单条巨型 subscribe 被 Bitget 断链,实测 29 次/83 行 → ws_client `_send` 分批≤50/批,
  风暴 29→0,DB 回填 36→179 恢复)+ 日志标签 bug(top_12 误显→真实 665 币)+ 异常 %s→%r 可诊断。**再 workflow
  3 agent 并行**(文件零冲突 {harmonic_monitor+config}/{harmonic_forward}/{dashboard}):A1 实证 Bitget 429=0→
  `_SEMA` 4→8;A2 分层调度(核心 60 每轮 + 长尾 round-robin 8 片);A3 列表分页/搜索/过滤 envelope;C4 OI 加速度/
  背离领先信号(2 阶导,缺数据中性)。**权威全量回归 2001 passed**(+78 新测)+ 编译 OK + dashboard 端到端
  (分页/搜索/页面 200/内联 JS node --check)+ 监控重启全 665 币纳入 WS 0。勾选 A1/A2/A3/H4/C4。**未部署(须批准)**。
  下一步:全永续冷启动回填完成度核验(_SEMA=8 后 ~40min)→ D2 量析终端 / E2 部署门禁清单。
