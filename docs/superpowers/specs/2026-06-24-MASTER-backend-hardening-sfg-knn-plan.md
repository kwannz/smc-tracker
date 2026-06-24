# 后端确定性核心强化 + SFG-KNN — 主计划（WF 编排锚点）

- 日期：2026-06-24
- 来源：5 份后端只读分析 + WF1(SFG 10 指标实证) + WF2(4 路线 spec 草稿 + Opus 对抗审查)
- 分工：**Opus 计划/审计,Sonnet 开发**(用户指定)。全程 workflow 编排。
- 约束：**本地实现 + TDD,部署/合并须用户批准**(CLAUDE.md / memory `harmonic-redesign-state`)。
- 详细 lane spec(本目录):
  - `2026-06-24-route-A-infra-hardening-design.md`
  - `2026-06-24-route-B-deterministic-foundation-rigor-design.md`
  - `2026-06-24-route-C-forward-signals-design.md`
  - `2026-06-24-route-D-sfg-knn-features-design.md`

---

## 0. 诚实定位（贯穿全计划）

本系统已实测并自承:1h 回看共识信号 = 精确随机(50%/0 alpha)。核心价值 = **诚实可验证的测量基础设施**。
因此强化排序的第一性原理:**先保证测量正确(不丢数据/不静默死/无 repaint/统计严谨),再谈 alpha**。
SFG-KNN 让 KNN 特征空间从「共线原始指标」升级到「设计好的 alpha 因子」,**有理论依据但不预设赚钱**,靠 review 闭环实测。

---

## 1. 四路线摘要

### A — 地基加固(确认的真隐患,非推测)
1. `db.py` 加 `PRAGMA busy_timeout=5000`(根治多进程 `database is locked`)+ `cache_size`/`temp_store`。
2. **热路径异步化(与 busy_timeout 成对,关键)**:`_on_sm_event` 同步 insert → 入缓冲由 `_periodic_flush` 批量落(`asyncio.to_thread`);`_on_structure` 的 `oi_change` 子查询 → 读 OI monitor 内存窗口。否则加了 busy_timeout 反而阻塞 event loop 5s。
3. 顶层 `gather` → per-task supervisor(捕获→log→指数退避重启),WS 保留自重连。不静默死。
4. 推送 `Queue(maxsize=2000)` 背压,防长跑 OOM。
5. `harmonic_setups` 加 `INDEX(ts)`/`INDEX(coin,ts)` 防全表扫。
- 明确排除(过度工程):连接池、时序 DB、mmap_size。

### B — 确定性基石统计严谨化
- **B1 消除谐波 repaint**:`find_pivots`+`_clean_alternating` 全量重算+贪心改写已选 pivot = 真 repaint(confirmed XABCD 会被下一根 K 线重写)。改为复用 `smc/structure.py` 的 **append-only 不可变 swing 流**。
- **B2 评分魔数外置 config + 样本守卫**:`smart_money_score` 权重/封顶/×0.85 → config;胜率 `wins/closed` 加最小样本守卫(复用 `efficacy.wilson_interval`)。
- **B3 协同显著性 + 归一化**:`_pair_stats` 裸计数 → 加 null model(二项/超几何)得 lift/p-value;`co_movers`/`lead_lag` 计数 ÷ 活跃度(消高频偏向)。

### C — 前瞻信号 alpha 强化(对标 Cont-Kukanov-Stoikov 等学术标准)
1. `orderbook_imbalance` 静态档位深度比 → **OFI 盘口逐帧增量** + queue imbalance + micro-price。
2. `flow_acceleration` 裸 2 阶导 → **先 EMA 平滑再求导**(抗噪),样本不足降权。
3. HL 路径 `app.py:851` 裸 `oi_vel` → `oi_directional_velocity`(函数已存在,一行级)。
4. `funding_extreme` 全历史等权 z-score → **滚动窗口经验分位**(厚尾稳健)。
5. 挂单墙抗 spoof:加挂单存活时间过滤 + build/pull 横跳计数。
- **前置 gate**:先跑最小脚本实证 HL l2Book 逐档语义(px 升序/sz 单位),再写 OFI(第一性原理)。

### D — SFG-KNN 特征强化(用户头号要求)
- 把 SFG 因子作 KNN 特征(从 11 维原始 → 拼接 SFG 因子,**不替换**)。
- 每因子暴露 `*_series(candles)→ndarray`(向量化,warmup=nan);抽公共 `level_factor` helper(6 个反转因子共享内核,去重)。
- 保留 z-score(量纲异构);**保留固定 horizon 标签**(本轮一次只变一个变量,不上 triple-barrier)。
- 克隆 `atr2_signals.py` + `test_atr2_signals.py` 范式;用 SFG 仓库 golden 值做跨语言平价。
- 零孤儿:`indicators/__init__.py` 导出 + `feature_matrix` 消费 + 更新 `test_feature_matrix_shape`。
- **诚实**:KNN 项目自承≈随机,富化特征不预设提升 PnL,不夸大为 alpha。

---

## 2. SFG 10 因子(WF1 实证) → KNN 特征

| 因子 | 簇 | 难度 | 因子内核 | 前视 |
|---|---|---|---|---|
| LRSD | 反转 | 低 | 分形供需带 `level_factor` | 因果,3根滞后 |
| GPI | 反转 | 低 | EMA 网格带(1960/1973)带内位置 | 完全因果 |
| VAP | 反转 | 中 | 成交量 POC/VAH/VAL 带内位置 | 因果(滚动) |
| **PDBB** | 反转 | **高** | ZigZag HH/LL 溢价折价 + breaker | 因果(须保留确认滞后) |
| Pivot | 反转 | 低 | pivot 高低 ffill 带内位置 | 低-中等 |
| AMI | 反转 | 中 | MLMI k-NN 动量振荡,通道归一+取负 | 因果(短序列 2000 窗退化) |
| ATR2 | 反转 | 低 | 归一化动量确认,取负(**smc 已有标量版**) | 因果 |
| MSFVG | 反转 | 中 | 市场结构+FVG 带内位置 | 因果 |
| AI_ST | 趋势 | 中 | AI 超级趋势 k-NN `pred*2−1` | 因果 |
| DMHA | 趋势 | 中 | 动态 MACD+双重平均K state{+1,0,−1} | 完全因果 |

聚合:反转 `α=clamp(Σf/8)`、趋势 `β=clamp((ai_st+dmha)/2)` —— 两条独立通道,作 KNN 不同特征,不符号合并。

---

## 3. 执行计划(对抗审查修正版)

### 3.1 排序(审查修正了原 A→B→C→D)
**`[B1 ∥ D] → A → C → [B2/B3]`**
理由:
- **D 不该垫底**:用户头号要求 + 风险隔离最好(零 app.py 触碰)+ 代码量最大宜早启动。
- **B1 该提前**:repaint 是确定性真 bug(confirmed 信号被改写),优先级 > 不确定的 C alpha。
- **A 第二批**:对已运行系统非阻塞,但必须在 C 之前(A 重构 app.py 结构,C 在其上局部改)。
- **C 第三**:依赖 HL l2Book 实证 gate,不确定性最高,建立在 A 稳定基线上更稳。
- **B2/B3 可推迟**:把已工作算法「统计严谨化」属质量改善非功能缺口。

### 3.2 Worktree 拓扑(零文件冲突)
```
worktree-D    : D 全部(独立,首批可合)        — 碰 indicators/{knn,__init__,atr2}+indicators/sfg/*+test_indicators
worktree-B1   : B1 谐波(独立,首批可合)        — 碰 indicators/harmonic.py(只读 smc/structure.py)
worktree-AC   : A 全部 → 然后 C 全部(同 worktree 串行,A先C后) — app.py 排他锁
worktree-B23  : B2 → B3(同 worktree 串行,共享 config)        — AC 合并后 rebase(B3 有 app.py call-site)
```

### 3.3 冲突要点
- **app.py 是最大序列化约束**:A + C + B3-callsite 三处交汇 → 必须单线程穿过 `A→C→(B3 rebase)`。
- **D / B1 完全隔离** → 可任意并行、任意顺序、首批合并。
- **oi_change 跨路线去重**:A 新增 `oi_window`(内存),C.3 应复用它(而非保留 851 磁盘查询)。

---

## 4. no-lookahead 硬护栏(审查强制)
- **D**:repaint 测试须覆盖 **pivot/lrsd/msfvg/pdbb 全 4 个**(非仅 pivot)——「尾部新极值不改已发射值」各一条。所有 wma/hma 须 **trailing 尾对齐**(非居中)。
- **B1**:`MarketStructure(lookback=order)` 须与谐波**同一 order 实例化**(否则 pivot 定义漂移);prefix-invariance 测试须覆盖 **swings 裁剪(500)边界**。
- **C**:`flow_acceleration` 前/后半段须 **trailing 时序**(非居中 bin)。

## 5. 测试基线与缺口
- ⚠️ **基线 357 passed 已过时**:实测 **1330 passed**(我已跑)。所有 TDD「必须全绿」锚点改用实测值。
- **A**:locked 竞速测试 flaky → 改确定性(断言 `pragma("busy_timeout")==5000` + monkeypatch);索引测试用 `"INDEX"` 子串宽松匹配。
- **B**:B2/B3 改权重须留**回归 golden snapshot**(证明重构不静默改分);wilson 须有 n=0 守卫。
- **C**:OFI/micro-price 须给**手算数值 golden oracle**(非仅方向)。
- **D**:最严谨(每因子 golden + parity rtol + 真实 fixture importorskip)。

## 6. scope 决策(需用户拍板)
1. **PDBB**:审查建议**首版砍至 9 因子**(PDBB 与 Pivot 同源 pivot、z-score 后近共线,砍掉零损失,用 review 闭环证明是否需补)。→ 推荐砍。
2. **B2/B3**:审查建议**可推迟到下一轮**(质量改善非缺口)。用户要求本轮含 B —— 是否本轮做 B2/B3,或仅做 B1?

## 7. 风险
- D 生产取数根数须 ≥ 最长 warmup(ai_st 90 + ami 2000 窗)否则 KNN 静默退化(0.15 权重 drop,不崩但失效)——须实证生产 K 线根数,AMI 短序列退化须诚实标注。
- C 依赖 HL l2Book 逐档语义实证 gate(前置必跑)。
- app.py 串行约束:A/C/B3 不可并行改 app.py。
- 部署 gate:本计划全本地,**合并/部署须用户批准**。

## 8. 下一步(WF3/WF4)
- **WF3 实现(Sonnet,worktree 隔离,TDD)**:按 3.2 拓扑 fan-out,首批 `D ∥ B1`,再 `A→C`,(可选)`B2/B3`。
- **WF4 审计(Opus 对抗)**:逐改动验证 no-lookahead/parity/测试覆盖/CLAUDE.md 合规 → 全量 pytest → 诚实报告 → 交用户决定合并。
