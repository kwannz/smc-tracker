# CLAUDE.md — SMC 聪明钱抓庄系统 开发规范

> 本文件是项目强制开发规范，Claude Code 自动加载。任何开发/迭代必须遵循。
> 详细架构见 [ARCHITECTURE.md](ARCHITECTURE.md)；路线图/进度见 [PLAN.md](PLAN.md)。

## 一、开发前必须思考（最高优先级）
**写任何代码之前先思考，禁止上来就写：**
1. **多假设性**：对每个功能先列 ≥2 个候选方案/假设，对比权衡再选，不锁死单一思路。
2. **模型知识库 + 开源案例结合**：先调用模型知识 + 参考成熟开源做法（指标/SMC/订单流/ML 的业界标准实现），
   再结合本项目实际落地，不闭门造车、不重复造轮子。
3. **第一性原理**：所有 API / 数据通路**先实证真实行为再写**（跑最小脚本验证格式/字段/限流），严禁假设。
   已知踩坑记录在 PLAN.md「关键经验」，先查再问。

## 二、产品方向（贯穿所有迭代）
- **预测性 + 前瞻性**：从「回看庄做过什么」转向「前瞻资金正在往哪 positioning」。
  历史回测仅作辅助验证（已知 KNN≈随机、高 lift≠赚钱，诚实标注，不夸大）。
- **★ 信号可信度地图（#150-183 样本外实证审计，单一真相源，禁止再建在已证伪前提上）**：
  - **🏛️ 基石(系统根本命题,#185 首次实证)：聪明钱技巧真实持续，非运气**：地址早半→晚半(fills 按时间切非重叠)
    **胜率持续 corr +0.515(null −0.004)、每笔效率 +0.523、Spearman +0.552**(43 地址,样本外·size-independent)——
    与方向(≈鞅)/regime(#177伪影)相反,过去盈利**预测**未来盈利⇒`smart_money_score` 的"跨窗一致性/ROI"筛选前提成立、
    "发现庄→跟庄"命题有据。⚠陷阱:**原始 PnL 持续 −0.295**(被账户规模+均值回归污染),必须用胜率/效率度量(#149纪律)。
    脚本 scripts/audit_smart_money_persistence.py 可复现。局限:n=43(HL 限流)、验证"技巧持续"非"跟单即盈利"(后者见 #186)。
    **#186→#192 第二道坎(入场可跟性)——OVERTURNED:照入场跟单不赚(庄聪明≠入场可复制)**:
    #186 初测似"庄入场领先 +0.46%/4h(扣漂移)",但 #192 用 **#189 retry 改善覆盖 + 币内配对(#191同法)**严格复核**推翻**:
    池化 alpha **+0.46%↔−0.53% 符号翻转**(coin-selection 伪影,同 #187→#188);币内配对 0延迟 4h **5/15币为正、中位 −0.16%**(24h 4/15正)⇒入场领先 edge **≈0~负**;
    **扣 0.10% 来回成本后净利全负(−0.4~−0.9%)、净胜率 38~50%**。**关键 reconcile**:**庄确聪明(技巧持续 #185 真)但盈利非来自可复制的入场 timing**(大概率出场/仓位/持有)→
    "发现庄→跟庄"中 smart-money 信号价值=**注意力/语境**("聪明钱在此活跃"),**非"照入场点复制即盈利"**。#190 的告警标注已撤回。脚本 scripts/audit_copytrade_latency.py。
    🎓教训:aggregate edge(#186)对币种选择极敏感,必须币内配对照妖(#191同型陷阱);我曾轻信并操作化(#190),#192 严格复核后撤回——撤自己的 overclaim 比发现新 edge 更必要。
    **#187→#188→#191 第二基石(共识/address_correlation)——已了结:共识放大≈0(多庄不比单庄预测更大移动)**:
    #187 初测似"双庄 +7.1%峰值/≥3反转"非单调,#188 复核翻成 −6%(跨运行符号翻转,疑币种选择伪影、无法定论)。
    #191 用 **#189 retry 改善覆盖(n 上万,#187/#188 的 +7.1%↔−6% 狂摆消失,效应全收缩 ±1.4pp 内=证实当初是覆盖伪影)+ 币内配对(消除币种选择混淆)**定论:
    **币内 cons(≥2)−solo(=1) 24h 中位差 −0.47pp、6/12 币为正(≈掷硬币)⇒共识放大≈0**,归入"≈0 噪声"(同方向/funding 拥挤)。
    **精确**:非"共识无用"——每庄入场仍带 #186 领先性,共识只是**不额外放大**;故 `WhaleConsensus` 勿按庄数加权当更强(score∝n_agree 无据),min_consensus=3 是去噪阈值非"越多越强"。脚本 scripts/audit_consensus_strength.py。
    🎓教训:#188"测不准"→先修根因(#189限流)+换干净估计量(币内配对)→#191"测准了=≈0";幸亏 #187 守"薄证据不改算法",全程零伤害。
  - **有 edge（系统真本事=测幅度/极端/形态/逼空，非测温和方向）**：
    · 波动**水平预测**有**扎实技巧**,主前瞻量=**GARCH(1,1)**(均值回归;EWMA #178 是其 α+β=1 退化特例,假设随机游走漏掉回归);
      胜 EWMA **周期依赖(#180 跨周期泛化)**:15m+0.078最强(corr 0.38/0.49/0.53@1/5/10bar)、4H/1D+0.02、1H≈中性+0.002(短周期获益最大,数据多→回归估计准);
      逐 bar |收益|记忆快衰减(#149 ARCH 自相关 lag-1≈0.28→lag-10≈0.05,null≈0)。生产实测工具:`vol --skill`(#182)在自己币集核对、dashboard σ→GA 呈现(#183)。
      ⚠️#177 null 对照纠 #153 偏差:原"扩张90%续/rv自相关0.73/lift7.6×"是**滚动窗重叠机械伪影**(null 0.711≈observed 0.725,真实增益+0.014≈0,非高持续);
      #178 修矫枉过正:水平预测 corr~0.4 真实(别贬成噪声)、逐bar记忆才快衰减——两个对象别混。脚本 scripts/audit_expansion_persistence.py 可复现。
    · pump/dump 规则(极端动量)样本外 lift 12-71×(#162-164，已无偏重校准)。
    · 谐波 setup 经验盈利 +0.5R/笔(#165，confidence 真校准:0.9桶 +1.5R)。
    · **逼空背离**(空头拥挤 funding<0 + 聪明钱净买)超基线 +0.83pp(#170，方向类唯一幸存 edge；小样本)。
      **生产 alpha 验证(#176)**:落 predictions 表的 kind 已按方向拆「逼空背离/分销背离」(两路径共用 `pred_kind`),
      让实盘 accuracy_report/efficacy 分桶独立审判——逼空累积前向样本验真、分销持续证实 ~0,不再混记稀释。
  - **≈0（噪声，勿当强预测、勿加码、勿主动推送）**：K线**方向**(velocity/PD/压缩/MTF 全反转 #150-158)、
    聪明钱**裸净流向**及其速度/**加速度**(#167-168，"加速度领先"已证伪)、OI velocity、funding 拥挤反转、分销背离、
    **波动变化方向**(GARCH/EW vs σ 升降 #157/#181：本质仅"极端正常化"均值回归 corr~0.5，朴素回归均值即天花板、GARCH 不加分、5bar 方向命中 50.7%≈掷硬币——GARCH 价值限于水平预测 #179 不延伸方向)。
  - **结论**：**方向几乎不可预测(收益≈鞅)、幅度可预测(波动有记忆)**——这是结构性事实，非实现缺陷。
    新信号默认按此先验:押幅度/极端/逼空可信，押温和方向需先样本外自证 edge 再上(否则当弱上下文，不推送)。
  - **减噪纪律(#169)**：主动**推送**只发样本外有 edge 的(如谐波 render min_conf≥0.75)；边际/噪声信号退到 dashboard 按需查(push严格 pull全显)。
- **抓庄核心**：发现庄(排行榜)→ 实时监控成交/持仓 → 跟庄/换仓/共识/可疑地址/关联(庄家集团) → 信号。
- **硬编码算法是核心，LLM 只做分析（用户 #36 明确）**：确定性算法(筛选地址 `smart_money_score`、
  协同/庄家集团 `address_correlation`)是系统主体与可验证基石，必须正确、可测、可解释；LLM 是上层解读，
  不可喧宾夺主、不可成为信号产生的必经路径。筛选用盈利+跨窗一致性+ROI+做市商判别；协同用滑窗+不应期+
  **跨币数(min_coins≥2 为同一实体硬证据，隔离单币追涨人群)**。
- **LLM 前瞻研判层（已建 `llm/`，#33）**：系统提示词(抓庄研判员/第一性原理/前瞻) + 用户提示词(实时态势摘要
  + 硬编码核心产出 `_hardcoded_context`)用 **Codex OAuth GPT-5.4**(`codex exec` 子进程，无 apikey)。
  默认 `llm.enabled=false`，需本机 `codex login`；失败优雅降级，绝不阻塞监控热路径。
  app `_periodic_llm` 周期推送 🧠抓庄研判；`scripts/llm_analyze.py` 独立验证。

## 三、代码库规范（全栈/agentic 工程标准）
1. **零孤儿**：每个源文件/模块必须接入运行时（app.py 或 poll_monitor.py 可达），**全部代码都要用上**。
   新建模块同迭代内必须接入 + 从所在目录 `__init__.py` 导出。改完用 grep 自查孤儿。
2. **去重**：公共 helper 集中到 `util.py`（`to_float` 安全数值解析、`fmt_hms` 时间格式），不重复定义。
   **统一多周期（单一真相源）**：全系统多周期 K 线**只用** `config.CANONICAL_TIMEFRAMES`
   = **`15m / 1H / 4H / 6H / 12H / 1D / 1W`**（7 周期；用户#明确，其余周期如 30m 一律删除）。
   谐波/BB/监控清单/采集器/dashboard 周期 tab 全部引用此常量，**禁止再各自硬编码周期列表**；
   `bitget.GRANULARITY_MS` 是交易所**能力表**（含 30m/6H 等），非系统选择，勿混淆。
   **每周期 K 线滚动保留 3000 根**（历史+实时统一上限，`Store.prune_candles_to(3000)`，超额删最旧）。
3. **数据质量高**：摄入数据加校验（数值用 `util.to_float` 拒 NaN/inf；周期用 VALID_INTERVALS 校验；
   避免裸下标 `r["k"]`/`lst[0]` 导致 KeyError/IndexError；空串 `int()` 加守卫）。
4. **低延迟**：热路径数值计算用 numpy 向量化（已把 indicators 关键循环向量化，compute_indicators ~1ms）；
   全程非阻塞 asyncio；SQLite WAL + 批量 executemany。
5. **异步并行 + 模型分工**：数据收集用 asyncio 并发(Semaphore 限流)；大开发任务用 **workflow 多 agent 并行**(文件零冲突)。
   **模型分工规范（Opus 规划/审计，Sonnet 执行）**：
   - **Opus（本模型）= 规划 + 审计**：拆 spec/plan、设计架构、定任务边界与依赖、最终把关合并；
     **亲自复核**每个执行单元产出（零孤儿 / TDD / §三-四规范 / 对抗式验证），不亲自写大批量样板代码。
   - **Sonnet = 执行**：按 plan 实现代码 + 写测试；workflow 里 `agent(prompt, {model:'sonnet'})` 或 builder-agent。
     不做架构决策、不改任务边界（有疑问回报 Opus）。
   - **闭合门**：每个 Sonnet 执行单元完成后**必须经 Opus 复核通过**才算闭合；复核不过则回退重做，不假闭合。
6. **风格**：中文注释 + 英文标识符 + 类型注解；slots dataclass；与现有代码一致。

## 四、验证规范（声称完成前必须做）
1. 改动后跑全量 `./.venv/bin/python -m pytest -q` 必须全绿（当前基线 2313 passed, 2 skipped；2026-06-26 实测）。
2. 新功能配单测（合成数据，确定性）；关键功能再用**真实数据**实证（非投资建议，仅验证）。
   指标类用 **TA-Lib 基准交叉验证**数值正确性（test_talib_parity，importorskip 零硬依赖）。
3. 编译检查 `python -m py_compile`。诚实报告结果，失败就说失败。

## 五、环境 / 入口
- venv：`./.venv/bin/python`（websockets/aiohttp/orjson/numpy/pyyaml/pytest/telethon；TA-Lib 仅用于平价校验）。
- **统一 CLI**（推荐入口）：`PYTHONPATH=src ./.venv/bin/python -m smc_tracker <cmd>`，子命令：
  `run`(流式实时) / `poll [--loop --interval N]`(轮询) / `report [--hours]` / `address <addr>` /
  `discover [--top]` / `bench` / `llm` / `dashboard [--port]`(Web 仪表盘) /
  `watch <add|rm|list>`(监控币种清单，config.monitored_coins.enabled 时驱动多周期采集，热载入)。
- 流式（等价）：`-m smc_tracker.app`；轮询（等价）：`scripts/poll_monitor.py`。
- **仪表盘**：`-m smc_tracker dashboard` → http://127.0.0.1:8787（aiohttp 实时单页，5s 自刷新；无 CDN/依赖）。
- 推送：config.telegram(bot_token+chat_id) → Telegram；config.output.webhook_url → Discord/Slack。
  推送告警带**实时价格+24h涨幅**(Bitget lastPr/change24h，BWE 风格) + **完整时间戳**(util.fmt_ts:日期+时间+时区)。
- **正确性回顾层**（review.py）：前瞻推送落 predictions 表(两源价交叉验证)，到期核对真实价 → 命中率/校准报告
  (诚实复盘纠正)。周期 `_periodic_review` 推送 📊准确率回顾。
- **行情监控板**（_periodic_ticker_board）：周期推送 📊 币种/价格/涨跌幅/资金费率/OI(Bitget，按涨跌幅排序)。
- **交易所资金流**（onchain/exchange_flow.py，keyless）：监控 Binance/OKX/Bitget 钱包资金动向，注册表
  config/exchange_wallets.yaml。**BTC**(blockstream.info 分页) 24h 净流入/流出 + **EVM 稳定币**(eth_getLogs，
  USDT/USDC 流入交易所=买盘弹药) 。`_periodic_exchange_flow` 大额推送 🏦，单位/语义感知(BTC 净流入🔴=抛压；
  稳定币净流入🟢=买盘弹药)。局限:公开地址为种子可能不全、对极端热钱包仍低估、Bitget BTC 地址待补。
- 约束：**无 API key**（纯公开数据：HL/Bitget 公开接口、公开 RPC、排行榜）。

## 六、接力（/loop 每小时驱动）
每次 loop：读 PLAN.md → 找第一个未完成项 → 按上述规范推进 → 勾选 → 追加迭代日志。
纲领见 memory（loop-directives）。
