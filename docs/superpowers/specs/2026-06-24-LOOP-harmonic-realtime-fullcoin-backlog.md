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
- [ ] D1 决策:React DSL 保留(引 CDN,违约) vs 原生重写(符合无依赖,工作量大) vs 设计 token 提取重构现 dashboard。**须用户拍板**(brainstorming 被 /loop 打断,待补)。
- [ ] D2 合并两终端(聪明钱追踪 + 量析)为统一前端,接 dashboard 真实 API。
- [ ] D3 谐波页用新设计(浅色金融终端风,IBM Plex 字体,三栏布局)。

### E. 固化与门禁
- [ ] E1 固化已完成全绿工作(SFG-KNN + fail-closed,本地 git 提交,非部署)。
- [ ] E2 部署门禁:本地全绿 + 全接入 + 网页排版无问题 → **用户批准** → 部署服务器。

## 迭代日志
- 2026-06-24 loop#1(Opus 主循环规划):实证谐波现状 gap(top_n 非全币种 / DB 缓存非实时);
  建本 backlog 作接力锚点。SFG fail-closed 修复完成(1798 全绿)。会话限制(重置 01:40)中,
  Sonnet workflow 执行留待后续 loop。下一步:A1(全永续性能实证)或 E1(固化全绿工作)。
