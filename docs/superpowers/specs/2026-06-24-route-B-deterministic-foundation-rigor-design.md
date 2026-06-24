# 路线 B — 确定性基石统计严谨化（实现级 spec）

> 状态: 草案(Opus 规划)。本 spec 只规划本地实现，**不部署**(部署须用户批准, 见 memory/harmonic-redesign-state)。
> 定位: 直接服务 CLAUDE.md 点名的「可验证基石」。核心是**提正确性/统计严谨, 非加新信号**。
> 三项改动彼此**文件解耦**, 可并行交付; 共用 helper 统一入 `util.py` / 复用 `signals/efficacy.wilson_interval`。
> 基线: `./.venv/bin/python -m pytest -q` 当前 357 passed, 改完必须全绿且新增测试通过。

---

## 0. 总览与设计取舍(多假设性)

三处确定性核心当前都有「统计不严谨」债务:

| 子项 | 现状根因 | 选定方案 | 被否方案 |
|---|---|---|---|
| B1 谐波 repaint | `find_pivots` 每次**全量重算** `swing_highs/lows` + `_clean_alternating` **贪心归并**, 同一历史段在下一根 K 线产出不同枢轴序列 → 已确认 XABCD 漂移 | 复用 `smc/structure.MarketStructure` 的**append-only 不可变 swing 流**驱动 `detect_xabcd` | (a)给 `_clean_alternating` 加「冻结已确认前缀」补丁——治标, 仍两套枢轴逻辑重复; (b)给枢轴打 hash 缓存——不解决归并漂移根因。均否。 |
| B2 协同共现无显著性 | `_pair_stats` 裸计数, `co_movers/lead_lag` 用绝对次数, 结构性偏向高频地址(交易越多越易"协同") | 对每对算**二项/超几何 null model** → lift + p-value, 只留显著对进 `_union_groups`; 计数**÷活跃度**做暴露归一 | (a)只按活跃度归一不做显著性——仍无"是否优于随机"判据; (b)permutation test——O(N·shuffles) 破坏低延迟/确定性。否, 用闭式二项尾概率(确定性+快)。 |
| B3 smart_money_score 魔数 | 权重/封顶/×0.85 全是无依据魔数; 胜率/ROI 裸比率无样本守卫(survivorship) | 魔数**外置 config + 文档化依据**; 胜率用 **Wilson 下界**(复用 `efficacy.wilson_interval`); 幸存者偏差**显式标注** | 直接拍新魔数——换汤不换药。否。 |

统一硬约束(CLAUDE.md): 无 API key/纯公开数据; 确定性可单测; numpy 向量化低延迟; 非阻塞 asyncio; 零孤儿(新模块同迭代接入运行时 + 从 `__init__.py` 导出 + grep 自查); 去重(公共 helper 入 `util`); 数据质量(`util.to_float` 拒 NaN/inf, `VALID_INTERVALS` 校验, 不裸下标); 指标类合成 golden/TA-Lib 平价; 中文注释 + 英文标识符 + 类型注解 + slots dataclass。

---

## B1. 消除谐波 repaint —— 复用 MarketStructure 不可变枢轴流

### B1.1 目标与现状(file:line)

- **现状根因**:
  - `src/smc_tracker/indicators/harmonic.py:94-120` `find_pivots`: 每次调用对**全量 candles** 跑 `swing_highs/swing_lows`(`patterns.py:11-30`)再 `sorted` 合并。
  - `src/smc_tracker/indicators/harmonic.py:123-144` `_clean_alternating`: 对**相邻同类型枢轴贪心保留更极端者**。这是 repaint 真源——新增一根 K 线若产生一个更极端的同类型枢轴, 会**改写历史段**已选枢轴, 导致同一历史窗口下一根产出不同 (X,A,B,C,D), 已"确认完成"的 XABCD 漂移/消失。
  - `harmonic.py:452` `analyze_candles` → `find_pivots` 是唯一入口; `monitor/harmonic_monitor.py:147` 是唯一运行时调用点。
- **已有可复用资产**: `src/smc_tracker/smc/structure.py:51-170` `MarketStructure`:
  - `update(candle) -> list[StructureEvent]` 逐根喂入, `swings: list[Swing]` 是 **append-only**(确认即追加, 永不回改; `structure.py:96-104`)。
  - `Swing(index, price, kind, time_ms)`(`structure.py:17-29`); `kind ∈ {"high","low"}`。
  - 滞后 `lookback` 根确认(与 `harmonic` `order` 语义一致)。
  - 关键: swing 一旦 append **不随后续 K 线改变** → 天然无 repaint。
  - `Candle` 同时具 `.h/.l/.c/.close_time_ms`(`models.py:71-82`), `analyze_candles` 已收 `list[Candle]`, **可直接 `ms.update(candle)`**。

### B1.2 逐项改动

**新增纯函数(harmonic.py 内, 不新建文件——避免孤儿)**:

`harmonic.py` 顶部 import 增:
```
from ..smc.structure import MarketStructure, Swing
```

新函数(放在 `find_pivots` 之后, 签名固定):
```python
def pivots_from_structure(
    candles: list[Any],
    order: int = 3,
) -> list[tuple[int, float, str]]:
    """用 smc.MarketStructure 的不可变 swing 流构造交替枢轴序列(根治 repaint)。

    与 find_pivots 同返回契约: [(idx, price, 'H'|'L'), ...] 升序, < 5 返回 []。
    差异: swing 由 append-only 引擎确认, 同一历史段不随新 K 线改变。
    交替性由 _alternate_immutable 强制, 但**不回改已选枢轴**(冻结语义)。
    """
```

实现要点(交 Sonnet):
1. `ms = MarketStructure(lookback=order)`; `for c in candles: ms.update(c)`。校验: `candles` 元素须有 `.h/.l/.c`(沿用现有 duck-typing; 合成测试用真 `Candle`)。空/不足返 `[]`。
2. 把 `ms.swings`(已升序, `index` 升序; 注: `_SWINGS_MAX=500` 裁剪只删最旧, 不影响近段)映射为 `(index, price, 'H' if kind=='high' else 'L')`。
3. 交替化用**新 helper `_alternate_immutable`**(下), 替代 `_clean_alternating` 的贪心改写。

**新增 `_alternate_immutable`**(harmonic.py, 替换 repaint 行为):
```python
def _alternate_immutable(
    swings: list[tuple[int, float, str]],
) -> list[tuple[int, float, str]]:
    """把已确认 swing 序列规整为严格交替 H/L, 但**保持因果不可变**:
    遇相邻同类型, 保留**先确认者**(index 更小者)、丢弃后者——绝不回改前缀。

    与 _clean_alternating 的本质差异: 后者贪心取更极端(改历史→repaint);
    本函数 first-wins(只追加不回改), 保证「给定前缀的输出不随后续 swing 变化」。
    """
```
关键不变量(决定 spec 成败): 对任意 `k`, `_alternate_immutable(swings[:k])` 是 `_alternate_immutable(swings[:k+1])` 的**前缀**。这是可单测的 repaint-free 断言。

> 取舍说明(写进 docstring): first-wins 会牺牲「同段更极端枢轴」的几何最优性, 但换来确定性/无 repaint。这是 CLAUDE.md「诚实/可验证」优先于「事后最优」的体现。

**接线(零孤儿)**:
- `harmonic.py:452` `analyze_candles` 内 `find_pivots(candles, order=order)` → 改为 `pivots_from_structure(candles, order=order)`。
- 保留 `find_pivots`/`_clean_alternating` 但标 `@deprecated`(docstring 注「repaint, 仅历史对照测试用, 运行时已弃用」), **或**直接删除并迁移 `tests/test_harmonic.py` 中依赖它们的用例到新函数。**选删除**(CLAUDE.md 零孤儿/去重): grep 确认 `find_pivots` 仅 `harmonic_monitor`(经 `analyze_candles`)与测试引用, 删后更新 `indicators/__init__.py:23,39` 的导出(移除 `find_pivots` 或替换为 `pivots_from_structure`)。
- `indicators/__init__.py`: `__all__` 增 `"pivots_from_structure"`(若保留 `find_pivots` 则二者并列)。
- grep 自查: `grep -rn "find_pivots\|_clean_alternating" src/ tests/` 改后无运行时孤儿。

**数据流**: `harmonic_monitor.refresh` → `get_candles`/`bg.klines` → `list[Candle]` → `analyze_candles` → `pivots_from_structure`(内部 `MarketStructure.update` 逐根) → `_alternate_immutable` → `detect_xabcd`/`project_prz`(下游**完全不变**, 契约同 `find_pivots`)。

> 低延迟注: `MarketStructure._is_swing_high/low`(`structure.py:144-170`)是 O(lookback) 纯 Python 小循环, 每根 O(order); 全程 O(N·order), 与 `swing_highs/lows` 同阶, 无回归。如需进一步向量化可后续, 本 spec 不要求。

### B1.3 TDD 测试计划(`tests/test_harmonic_no_repaint.py`, 新文件)

合成数据(确定性, 用真 `Candle`, 无网络):
1. **`test_pivots_match_legacy_contract`**: 构造一段含清晰交替 H/L 的 K 线, 断言 `pivots_from_structure` 返回 `[(idx,price,'H'/'L')]` 升序、交替、长度 ≥5。
2. **`test_immutable_prefix_invariant`**(核心): 给定 K 线序列 `cs`, 对每个切点 `k` in 范围内: `p_k = pivots_from_structure(cs[:k])`, `p_k1 = pivots_from_structure(cs[:k+1])`, 断言 `p_k == p_k1[:len(p_k)]`(已确认枢轴**永不改变**)。
3. **`test_confirmed_xabcd_does_not_repaint`**(端到端): 构造一段产出 ≥1 个 completed XABCD 的 K 线; 逐根增量喂入 `analyze_candles(cs[:k])`; 一旦某 `D_idx` 出现在 completed, 断言后续所有 `k' > k` 的输出里**同 D_idx 的 (X,A,B,C,D) 点位与 pattern 不变**(允许因 `_COMPLETED_MAX_DIST`/recent 过滤而从列表"消失", 但**不允许同 D_idx 点位改变**)。
4. **`test_alternate_first_wins`**: 直接喂 `_alternate_immutable` 一个 `[H,H,L]` 序列(两相邻 H), 断言保留**index 更小**的 H(first-wins), 与旧 `_clean_alternating` 取更极端区分。
5. **回归**: 跑现有 `tests/test_harmonic*.py`; 若旧用例断言依赖 `_clean_alternating` 贪心语义, 迁移/更新并在 commit 说明。

### B1.4 风险与回滚

- 风险: 下游 `detect_xabcd` 命中率可能下降(first-wins 比 greedy-extreme 少抓个别形态)。缓解: 这是**预期**的诚实代价(repaint 形态本就不可交易); 用 #3 端到端测试量化"completed 数量"变化, commit 记录。
- 风险: `MarketStructure` 裁剪(`_SWINGS_MAX=500`/`_MAX=512`)在超长 bars(2500)下删最旧 swing。影响: 仅丢极远古枢轴, `analyze_candles` 本就 `recent_cutoff` 只取近段(`harmonic.py:461`), 无实质影响。测试覆盖 bars≤512 即可, 另加一条 bars=2500 不崩断言。
- 回滚: 单文件改动 + 一处接线。回滚 = `analyze_candles` 改回 `find_pivots`(若保留)或 git revert 本提交。

---

## B2. 协同共现加显著性 —— 二项 null model + 活跃度归一

### B2.1 目标与现状(file:line)

- `monitor/address_correlation.py:25-59` `_pair_stats`: 滑窗+不应期裸计数 `counts[(A,B)]`, **无 null model**——高频地址天然与更多人"同窗", 计数膨胀。
- `:61-66` `co_movers` / `:146-214` `lead_lag`: 用**绝对次数**排序/聚合, 结构性偏向高频。
- `:90-117` `clusters` / `_union_groups`: 直接用 `counts ≥ min_shared` 进并查集, 无"是否优于随机"判据 → 单币追涨人群易混入。
- 调用点(零孤儿需全覆盖): `app.py:883,940`(`clusters_detailed`)、`app.py:904`(`cluster_leader`)、`dashboard.py:94`、`monitor/address_dossier.py:62,78`、`monitor/poll_monitor.py:171-181,240`。

### B2.2 逐项改动

**新建纯统计模块 `src/smc_tracker/monitor/cooccur_stats.py`**(确定性纯函数, 无 I/O, 易单测):

```python
def pair_lift(
    pair_count: int,        # 该对协同事件数(_pair_stats 不应期去重后)
    a_activity: int,        # A 的总协同事件参与数(暴露度)
    b_activity: int,        # B 的总协同事件参与数
    total_events: int,      # 全体协同事件总数(归一基)
) -> tuple[float, float]:
    """返回 (lift, p_value)。

    null model: 在"随机配对"零假设下, A、B 共现期望次数
      expected = a_activity * b_activity / total_events  (独立性下的期望共现)
    lift = pair_count / max(expected, eps)  (>1 = 强于随机)
    p_value: 二项右尾 P(X >= pair_count | n=a_activity, p=b_activity/total_events)
             用闭式/对数空间求和(确定性, 无随机), n 大时正态近似兜底。
    """

def is_significant(lift: float, p_value: float,
                   min_lift: float, max_p: float) -> bool:
    """显著 ⟺ lift >= min_lift 且 p_value <= max_p。纯阈值判据, 确定性。"""
```

实现要点:
- `expected` 用独立性期望(超几何/二项的一阶矩); `lift` 是标准 association 度量(等价 PMI 的指数形式)。
- p-value 用**二项右尾**: `p = sum_{k>=pair_count} C(n,k) p^k (1-p)^{n-k}`, 在**对数空间**累加防溢出; `n > 1000` 用正态近似(`mean=np`, `var=np(1-p)`, 连续性校正)保持低延迟。numpy 向量化求和。
- 所有除法用 `eps=1e-12` 守卫; 入参经 `util.to_float`/`int` 守卫(不裸用)。
- **去重**: `pair_lift` 不放 `util.py`(它是 util 的通用数值器, 不含领域语义); 但其依赖的「二项右尾」若已在 `efficacy.py` 缺失, **新建私有 helper 留在 `cooccur_stats.py`**, 不污染 util。Wilson 在 B3 复用 `efficacy.wilson_interval`, 此处不需要。

**改 `_pair_stats`(`address_correlation.py:25-59`)**: 额外返回每地址活跃度与总事件数, 供 lift 计算。签名扩展(保持向后兼容——新增第三返回元素, 旧调用解包前两个):
```python
def _pair_stats(self, since_ms, window_sec=60
    ) -> tuple[Counter, dict[tuple[str,str], set[str]], dict[str,int], int]:
    # 返回 (pair_counts, pair_coins, activity, total_events)
    # activity[addr] = addr 参与的协同事件总数; total_events = Σ pair_counts.values()
```
> 注: 现有 4 个内部调用(`co_movers:64`、`clusters:116`、`clusters_detailed:125`)需同步更新解包。grep `_pair_stats` 自查全覆盖。

**改 `_union_groups`(`:90-108`)**: 入参增 `activity/total_events/min_lift/max_p`, 在 `c >= min_shared` 之外**追加 `is_significant(...)` 门**, 不显著的对不进并查集:
```python
def _union_groups(self, counts, coins, activity, total_events,
                  min_shared, min_coins, min_lift, max_p) -> list[list[str]]:
    for (a, b), c in counts.items():
        lift, p = pair_lift(c, activity[a], activity[b], total_events)
        if c >= min_shared and len(coins[(a,b)]) >= min_coins \
           and is_significant(lift, p, min_lift, max_p):
            union(a, b)
```

**改 `co_movers`(`:61-66`)**: 排序键从绝对 `c` 改为 **lift 优先**(归一暴露度后), 或返回值附 lift。最小侵入方案: 返回 `[(a, b, count, lift)]` 4 元组? — **否**(破坏 `correlated_with:82` 解包契约)。**选**: 内部按 `lift` 降序、仍只过滤 `c >= min_shared`, 返回签名不变 `[(a,b,c)]`, 但**排序由 lift 决定**, 并在 docstring 注明"排序已用活跃度归一(消高频偏向), c 仍为原始协同次数"。`correlated_with`/`app`/`dashboard` 解包不变, 零破坏。

**改 `lead_lag`(`:146-214`)**: `net[(i,j)]` 计数**÷参与活跃度**消高频偏向。最小方案: `score[a] = Σ_b (net[(a,b)] - net[(b,a)]) / max(activity[a], 1)`(归一净领先)。保持返回签名 `[(addr, score, leads, lags)]`(score 改为归一后四舍五入或保留 float; `cluster_leader:230` 只判 `score > 0`, 归一不改符号, 零破坏)。

**config 外置阈值** —— `config.py` `CorrelationCfg`(若无则在 `DetectionCfg` 增字段):
```python
@dataclass(slots=True)
class CorrelationCfg:
    min_lift: float = 2.0      # 共现强度 ≥2× 随机期望才算显著(依据: lift>2 业界常用强关联阈)
    max_p: float = 0.01        # 二项右尾 p ≤1% (99% 置信非随机)
    min_shared: int = 3
    min_coins: int = 2
```
在 `Config`(`config.py:244-260`)注册 + `load`(`:280-296`)透传(对照 `harmonic` 写法); `config.example.yaml` 补 `correlation:` 块 + 注释依据。`AddressCorrelation.__init__` 接收 cfg(或默认值), 调用点 `app.py`/`poll_monitor`/`dashboard`/`dossier` 传入 `cfg.correlation`(默认值保证旧测试不破)。

**接线(零孤儿)**: 新模块 `cooccur_stats.py` 必须被 `address_correlation` import + 从 `monitor/__init__.py` 导出 `pair_lift, is_significant`。grep `pair_lift` 确认运行时可达。

### B2.3 TDD 测试计划(`tests/test_cooccur_stats.py` + 扩 `tests/test_correlation.py`)

合成 `hl_meme_trades`(沿用 `test_correlation.py` 的 `_trade` 工厂, 无网络):
1. **`test_pair_lift_random_pair_low`**: 两高频地址**各自独立**与众多人共现(无真协同), 构造使 `pair_count ≈ expected` → 断言 `lift ≈ 1.0`、`p_value > max_p`(不显著)。
2. **`test_pair_lift_true_collusion_high`**: 两地址几乎只彼此共现(低活跃但高 pair_count) → 断言 `lift >> 2`、`p_value < 0.01`。
3. **`test_binom_tail_monotone`**: 固定 n,p, `pair_count` 越大 p_value 越小(单调)；`pair_count=0` → p≈1; 边界 `pair_count=n` → p = p^n。对照 `math.comb` 暴力小样本(n≤20)逐项校验闭式正确(golden)。
4. **`test_normal_approx_matches_exact`**: n=500 时正态近似与对数空间精确值相对误差 < 5%(平价校验)。
5. **`test_clusters_filters_random_crowd`**(端到端): 构造**单币 50 人同时追涨**(高频但随机) + **2 地址跨 3 币真协同**; 断言 `clusters(min_lift=2,max_p=0.01)` **只返回真协同对**, 追涨人群被显著性门过滤。
6. **`test_co_movers_sorts_by_lift`**: 高频对绝对 count 高但 lift 低、低频真协同对 lift 高 → 断言后者排在前。
7. **`test_lead_lag_activity_normalized`**: 一个超高频地址"凑巧"领先很多 → 归一后 score 不再机械居首。
8. **回归**: `test_correlation.py` 现有用例(`test_co_movers_finds_pair` 等)在新默认阈值下仍通过(必要时调测试数据使其满足显著性, 或测试显式传低阈值)。

### B2.4 风险与回滚

- 风险: 阈值过严 → 真集团被过滤(漏报)。缓解: 默认 `min_lift=2/max_p=0.01` 偏宽松; 阈值全 config 可调; 端到端测试锚定行为。
- 风险: `_pair_stats` 签名扩展漏改某调用点 → 解包错。缓解: grep `_pair_stats` 全覆盖(4 处), 测试覆盖每个 public 方法。
- 风险: lift 在 `total_events` 极小时不稳。缓解: `total_events < min_events`(如 <30) 时 `pair_lift` 返回中性 `(1.0, 1.0)`(样本不足不冒进, 同 efficacy 哲学)。
- 回滚: 新阈值默认值设极宽(`min_lift=0/max_p=1`)即等价旧行为; 或 git revert。

---

## B3. smart_money_score 魔数外置 + 样本守卫 + 幸存者偏差标注

### B3.1 目标与现状(file:line)

- `monitor/address_analyzer.py:57-92` `smart_money_score`: 权重(28/18/16/14/8/8/8)、封顶(5000万/1000万/0.5/0.7)、`×0.85` churn 折扣**全是无依据魔数, 硬编码在函数体**。
- `:88` `win_rate` 用裸 `wins/len(closed)`(`:24-44` `analyze_fills:37`), **无最小样本守卫**——3 单 2 胜=67% 与 300 单 200 胜=67% 同权, 幸存者偏差。
- `:82-84` ROI(`mo/av`)同样无守卫。
- 调用点: `address_analyzer.py:133`(`analyze` 内)是唯一运行时调用; `tests/test_address_analyzer.py` 测高分场景。

### B3.2 逐项改动

**(1) 魔数外置 config** —— `config.py` 新 `SmartScoreCfg`(slots dataclass):
```python
@dataclass(slots=True)
class SmartScoreCfg:
    """smart_money_score 权重/封顶。每项注释依据(非拍脑袋)。"""
    w_alltime: float = 28.0;  cap_alltime: float = 50_000_000   # 全期 PnL 主导权重
    w_month: float = 18.0;    cap_month: float = 10_000_000
    w_consistency_all: float = 16.0; w_consistency_part: float = 7.0
    w_roi: float = 14.0;      cap_roi_monthly: float = 0.5      # 月化 50% 封顶
    w_realized: float = 8.0
    w_account: float = 8.0;   cap_account: float = 10_000_000
    w_winrate: float = 8.0;   cap_winrate: float = 0.7
    churn_vol_floor: float = 1_000_000; churn_eff_max: float = 0.001
    churn_penalty: float = 0.85
    min_trades_winrate: int = 20   # 胜率最小样本(Wilson 守卫触发阈)
```
依据写进 docstring/`config.example.yaml` 注释(交 Sonnet 落字, 引用 CLAUDE.md「聪明钱低胜率高盈亏比」「跨窗一致性=持续 edge」「churn=非方向 alpha」)。在 `Config` 注册 + `load` 透传(对照 `harmonic`)。

**(2) `smart_money_score` 签名扩展**(向后兼容——cfg 默认):
```python
def smart_money_score(profile: dict[str, Any],
                      cfg: SmartScoreCfg | None = None) -> float:
    # cfg=None → 用 SmartScoreCfg() 默认值(等价当前魔数), 旧测试零破坏
```
函数体所有字面量替换为 `cfg.*`; 逻辑结构**完全不变**(只去魔数, 不改算法), 保证旧高分用例仍通过。

**(3) 胜率 Wilson 下界守卫** —— 复用 `signals/efficacy.wilson_interval`(`efficacy.py:24-42`, **不重写**, 去重):
- `analyze_fills`(`:24-44`)已算 `wins`/`n_closed`; 让其额外输出 `win_rate_lower`(Wilson 下界):
  ```python
  from ..signals.efficacy import wilson_interval
  lo, _ = wilson_interval(wins, len(closed))  # n_closed=0 → (0.0, 1.0)
  beh["win_rate_lower"] = lo
  ```
- `smart_money_score` 胜率项(`:88`)从 `min(wr, cap)` 改用 **`win_rate_lower`**(小样本自动塌向 0, 大样本逼近裸胜率):
  ```python
  wr_lb = profile.get("win_rate_lower", 0.0)
  s += min(wr_lb, cfg.cap_winrate) / cfg.cap_winrate * cfg.w_winrate
  ```
  > 效果: 3 单 2 胜的 Wilson 下界 ≈0.2(守卫生效), 300 单 200 胜下界 ≈0.61。幸存者/小样本偏差被结构性压制, 无需硬 `min_trades` 截断(Wilson 平滑优于硬阈)。
- ROI 项(`:82-84`)同理加最小样本守卫: `av > 0 and n_trades >= min_trades`(或对极小 `av` 守卫), 不足则 ROI 项不计入(诚实)。

**(4) 幸存者偏差显式标注**:
- `profile` 增字段 `survivorship_note`/`score_caveats: list[str]`, 当样本不足(`n_closed < cfg.min_trades_winrate`)或纯排行榜入选(`perp_active=False`)时填诚实说明。
- `AddressAnalyzer.fmt`(`:138-146`)推送文案在评分后附 `⚠样本N单(胜率下界估计)` 标注(CLAUDE.md 诚实标注), 让用户知道分数的统计可靠度。

**接线(零孤儿)**: `SmartScoreCfg` 从 `config.__init__`/导出可达; `smart_money_score` 调用点 `address_analyzer.py:133` 传 `cfg`(由 `AddressAnalyzer.__init__` 持有, 或调用方注入)。`win_rate_lower` 字段进 `address_profiles` 落库(若 schema 固定, 仅内存用不落库亦可——避免改 schema; 由 Sonnet 按 `db.py` upsert 字段决定, 优先**不改 schema**)。

### B3.3 TDD 测试计划(扩 `tests/test_address_analyzer.py`)

合成 profile/fills(确定性, 沿用 `_mk` Fill 工厂):
1. **`test_score_cfg_default_equals_legacy`**: 同一 profile, `smart_money_score(p)` == 旧硬编码基线分(保护重构不改数值)。用现有高分用例的期望值锚定。
2. **`test_winrate_small_sample_guarded`**: 两 profile 裸胜率同为 67%, 一个 `n_closed=3`、一个 `n_closed=300` → 断言后者得分**明显高于**前者(Wilson 下界守卫生效)。
3. **`test_winrate_lower_monotone`**: `analyze_fills` 输出的 `win_rate_lower` ≤ 裸 `win_rate`, 且 n 越大越逼近(平价)。
4. **`test_cfg_override_changes_weight`**: 传 `cfg.w_alltime=0` → 全期 PnL 项归零, 分数下降可预测。
5. **`test_zero_closed_safe`**: `n_closed=0` → `win_rate_lower=0`, 不崩, 胜率项=0(守卫 `wilson_interval(0,0)=(0,1)` 取下界 0)。
6. **`test_survivorship_caveat_present`**: 小样本/纯现货 profile → `score_caveats` 非空且含诚实文案; `fmt` 输出含 ⚠ 标注。
7. **回归**: 现有 `test_address_analyzer.py` 全绿(默认 cfg 等价旧行为)。

### B3.4 风险与回滚

- 风险: Wilson 下界使小样本地址普遍降分 → 新发现的"潜力地址"被低估。缓解: 这是**统计诚实**的预期代价(小样本本就不可信); `survivorship_note` 让用户知情; cap/阈值可调。
- 风险: 改 `analyze_fills` 返回结构可能影响其他消费者。缓解: grep `analyze_fills`/`win_rate` 调用点; 仅**新增**字段不删旧字段, 零破坏。
- 回滚: cfg 默认值 == 旧魔数; 胜率守卫可由 `min_trades_winrate=0`(退回裸比率)关闭; 或 git revert。

---

## 4. 跨路线冲突检测 —— 本 spec 涉及/修改文件清单

> 供 workflow 多 agent 并行时文件级零冲突核对。**[改]**=修改既有, **[新]**=新建。

### B1(谐波 repaint)
- **[改]** `src/smc_tracker/indicators/harmonic.py`(加 `pivots_from_structure`/`_alternate_immutable`; 改 `analyze_candles` 接线; 删/弃用 `find_pivots`/`_clean_alternating`)
- **[改]** `src/smc_tracker/indicators/__init__.py`(导出更新: 第 23、39 行)
- **[新]** `tests/test_harmonic_no_repaint.py`
- 只读依赖(不改): `src/smc_tracker/smc/structure.py`、`src/smc_tracker/models.py`、`src/smc_tracker/monitor/harmonic_monitor.py`(经 `analyze_candles` 间接, 无需改)
- 可能需更新: `tests/test_harmonic.py`(若用例依赖被删函数)

### B2(协同显著性)
- **[新]** `src/smc_tracker/monitor/cooccur_stats.py`
- **[改]** `src/smc_tracker/monitor/address_correlation.py`(`_pair_stats`/`_union_groups`/`co_movers`/`lead_lag`/`clusters*`/`__init__` 接 cfg)
- **[改]** `src/smc_tracker/monitor/__init__.py`(导出 `pair_lift, is_significant`)
- **[改]** `src/smc_tracker/config.py`(`CorrelationCfg` + `Config` 注册 + `load` 透传)
- **[改]** `config/config.example.yaml`(`correlation:` 块)
- **[改]** 调用点传 cfg: `src/smc_tracker/app.py`(883/904/940)、`src/smc_tracker/dashboard.py`(94)、`src/smc_tracker/monitor/address_dossier.py`(62/78)、`src/smc_tracker/monitor/poll_monitor.py`(171-181/240)
- **[新]** `tests/test_cooccur_stats.py`; **[改]** `tests/test_correlation.py`

### B3(评分严谨化)
- **[改]** `src/smc_tracker/monitor/address_analyzer.py`(`smart_money_score`/`analyze_fills`/`fmt`/`analyze` 传 cfg)
- **[改]** `src/smc_tracker/config.py`(`SmartScoreCfg` + 注册 + 透传) — **与 B2 同改 `config.py`, 并行时需协调(同文件冲突点)**
- **[改]** `config/config.example.yaml`(`smart_score:` 块) — **与 B2 同改, 冲突点**
- 只读依赖(去重复用, 不改): `src/smc_tracker/signals/efficacy.py`(`wilson_interval`)
- **[改]** `tests/test_address_analyzer.py`

### 跨路线冲突要点
- **`config.py` 与 `config/config.example.yaml` 被 B2+B3 同改** → 若并行, 让一个 agent 先落 config 骨架, 或合并到同一子任务串行。
- B1 与 B2/B3 **文件零交集**, 可完全并行。
- 与仓库现有未提交改动(`harmonic_monitor.py`、`signals/*`、`storage/db.py` 等已 M/??)无新增交集(本 spec 不碰这些, 除 B2 调用点 `app/dashboard/poll_monitor/dossier` 为加 cfg 参数的最小改动)。

---

## 5. 验证清单(声称完成前, CLAUDE.md 四)
1. `./.venv/bin/python -m pytest -q` 全绿(基线 357 + 新增, 无回归)。
2. 新增确定性单测: `test_harmonic_no_repaint.py`(prefix 不变量)、`test_cooccur_stats.py`(随机人群被过滤 + 二项尾 golden)、`test_address_analyzer.py` 扩(Wilson 守卫)。
3. `python -m py_compile` 全过; grep 自查零孤儿(`pivots_from_structure`/`pair_lift`/`SmartScoreCfg` 均运行时可达 + 导出)。
4. 诚实报告: B1 预期 completed 数下降、B3 小样本降分, 均在 commit 说明(非 bug, 是统计诚实)。
5. 不部署(用户批准前)。
