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
  优先用领先信号：订单簿挂单意图(l2Book，未成交)、资金流加速度(2阶导)、OI 速度 —— 都先于价格。
  历史回测仅作辅助验证（已知 KNN≈随机、高 lift≠赚钱，诚实标注，不夸大）。
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
3. **数据质量高**：摄入数据加校验（数值用 `util.to_float` 拒 NaN/inf；周期用 VALID_INTERVALS 校验；
   避免裸下标 `r["k"]`/`lst[0]` 导致 KeyError/IndexError；空串 `int()` 加守卫）。
4. **低延迟**：热路径数值计算用 numpy 向量化（已把 indicators 关键循环向量化，compute_indicators ~1ms）；
   全程非阻塞 asyncio；SQLite WAL + 批量 executemany。
5. **异步并行**：数据收集用 asyncio 并发(Semaphore 限流)；大开发任务用 **workflow 多 agent 并行**(文件零冲突)。
6. **风格**：中文注释 + 英文标识符 + 类型注解；slots dataclass；与现有代码一致。

## 四、验证规范（声称完成前必须做）
1. 改动后跑全量 `./.venv/bin/python -m pytest -q` 必须全绿（当前基线 357 passed）。
2. 新功能配单测（合成数据，确定性）；关键功能再用**真实数据**实证（非投资建议，仅验证）。
   指标类用 **TA-Lib 基准交叉验证**数值正确性（test_talib_parity，importorskip 零硬依赖）。
3. 编译检查 `python -m py_compile`。诚实报告结果，失败就说失败。

## 五、环境 / 入口
- venv：`./.venv/bin/python`（websockets/aiohttp/orjson/numpy/pyyaml/pytest/telethon；TA-Lib 仅用于平价校验）。
- **统一 CLI**（推荐入口）：`PYTHONPATH=src ./.venv/bin/python -m smc_tracker <cmd>`，子命令：
  `run`(流式实时) / `poll [--loop --interval N]`(轮询) / `report [--hours]` / `address <addr>` /
  `discover [--top]` / `bench` / `llm` / `dashboard [--port]`(Web 仪表盘)。
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
