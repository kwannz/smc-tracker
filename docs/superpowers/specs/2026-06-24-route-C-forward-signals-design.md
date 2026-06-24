# 路线 C — 前瞻信号 alpha 强化 (实现级 spec)

## C.0 设计前提与多假设权衡

**现状诚实定位**：回看信号(KNN/历史 lift)已塌回随机 (MEMORY 记录 HL 方向 ~50%)。本路线是系统**唯一 alpha 希望**，必须扎实抗噪、抗 spoof、防双计。对标业界标准 Cont-Kukanov-Stoikov (2014) *Order Flow Imbalance* 与 micro-price (Stoikov 2018)。

**逐项的候选方案与选型**(对每改动列 ≥2 假设)：

| 改动 | 候选 A | 候选 B | 选型 |
|---|---|---|---|
| 盘口失衡 | 静态档位深度比(现状) | 逐帧 OFI 增量 | **OFI + queue-imbalance(L1) + micro-price**(A 含 spoof 全深度；OFI 只算最优档增量、对深处虚挂不敏感) |
| 加速度平滑 | EMA 预平滑序列 | Savitzky-Golay 速度 | **EMA 预平滑**(SG 需固定步长窗口、样本不规则时退化；EMA 在线 O(1) 抗噪足够，低延迟) |
| funding 极值 | 全历史等权 z(现状) | 滚动窗口经验分位 | **滚动经验分位**(z 假设高斯，funding 厚尾;分位无分布假设、稳健) |
| 抗 spoof | 静态深度阈值 | 存活时间 + flap 计数 | **存活时间 + build/pull flap 计数**(spoof 特征=瞬现瞬撤) |

**防双计总账**(贯穿全路线，硬约束)：
- `flow_score`(FlowPredictor.predict) **已含** accel 分量 (权重 0.45) + book_imbalance (0.35) + oi (0.20)。
- OFI/queue/micro-price **替换** predict 内的 `book_imbalance` 入参，不新增并列项。
- `oi_directional_velocity` **替换** predict 的 `oi_velocity` 入参 (现 app.py:851 裸比率)，不并列。
- `funding_extreme_signal` 是 predict **不含**的独立维度，仅在上层 ConfluenceAggregator / harmonic_forward 单列消费，**不进** predict。
- 抗 spoof 是 book_imbalance/wall 的**质量门控** (过滤+降权)，非新增分数项。

---

## C.1 OFI + queue imbalance + micro-price (替换静态 orderbook_imbalance)

**目标**：把 `orderbook_imbalance`(flow_predictor.py:18-24，全深度名义比、对深处 spoof 敏感、无逐帧增量) 升级为业界标准前瞻微观结构三件套。

**现状 (file:line)**：
- `src/smc_tracker/signals/flow_predictor.py:18-24` — 静态 depth=15 档名义深度比，裸 `float(b["px"])` 下标(数据质量违规)，无防 spoof。
- `src/smc_tracker/monitor/orderbook_monitor.py:113-153` `_on_l2book` — 逐帧入口，持 `self._walls`(prev 墙) 但**不持**上一帧最优 N 档原始 sz，无法做 OFI 差分。

**新文件**：`src/smc_tracker/signals/microprice.py`

纯函数 (确定性、numpy 向量化、`util.to_float` 解析、不裸下标)：

```
def queue_imbalance(bids, asks, depth=5) -> float
    # ∈[-1,1]：Σbid_sz[:depth] vs Σask_sz[:depth]，按 size(非名义)，正=买盘排队厚
    # 名义易被远档大额虚挂污染；queue 用 size 聚焦真实排队意图

def micro_price(bids, asks) -> dict[str,float]
    # Stoikov micro-price = (bid_px*ask_sz + ask_px*bid_sz)/(bid_sz+ask_sz)
    # 返回 {"micro": float, "mid": float, "tilt": (micro-mid)/mid}
    # tilt>0 = micro 偏向 ask = 买压(前瞻看涨)；空盘/零量→tilt=0

def ofi_delta(prev_bid, prev_ask, cur_bid, cur_ask) -> float
    # Cont-Kukanov-Stoikov L1 OFI 单帧增量(只看最优档 px+sz 变化):
    #   e_b = (cur_bid_px > prev_bid_px) ? cur_bid_sz
    #       : (cur_bid_px == prev_bid_px) ? (cur_bid_sz - prev_bid_sz)
    #       : -prev_bid_sz
    #   e_a = (cur_ask_px < prev_ask_px) ? cur_ask_sz       # ask 方向相反
    #       : (cur_ask_px == prev_ask_px) ? (cur_ask_sz - prev_ask_sz)
    #       : -prev_ask_sz
    #   return e_b - e_a        # 正=买方订单流净增(前瞻看涨)
    # prev/cur 各为 (px:float, sz:float)；任一无效→0.0
```

**有状态聚合器** (同文件，slots dataclass)：

```
@dataclass(slots=True)
class OFITracker:
    _prev: dict[str, tuple[float,float,float,float]]   # coin->(bid_px,bid_sz,ask_px,ask_sz)
    _cum: dict[str, deque]                              # coin->deque[(ts, ofi)] maxlen
    window_ms: int = 60_000

    def update(self, coin, bids, asks, ts) -> float
        # 取最优档(bids[0]/asks[0] 经 to_float)，与 _prev 算 ofi_delta，
        # 入 _cum，更新 _prev；首帧 prev 缺→记录并返回 0.0
    def normalized(self, coin, now_ms) -> float
        # 窗口内 Σofi / (Σ|ofi|+eps) ∈[-1,1]，作前瞻盘口意图分数
```

**数据流改动**：
1. `OFITracker` 实例挂在 `HLOrderbookMonitor.__init__` (orderbook_monitor.py:77-104)，`self._ofi = OFITracker()`。
2. `_on_l2book` (orderbook_monitor.py:130-134) 内调 `self._ofi.update(coin, bids, asks, ts)`；并存 micro-price/queue 到 `self._imbalance[coin]`，扩展该 dict 为 `{imbalance, queue_imb, micro_tilt, ofi_norm, bid_usd, ask_usd}` (**向后兼容**：`imbalance` 键保留)。
3. `book_imbalance()` (orderbook_monitor.py:173-175) 返回值扩展，新增键默认 0.0。
4. `predict` 的 `book_imbalance` 入参由调用方改传**复合盘口意图** = `0.5*ofi_norm + 0.3*queue_imb + 0.2*micro_tilt` (在 orderbook_monitor 新方法 `book_intent(coin, now_ms)` 计算，单一出口防双计)；predict 内部权重不动。
5. app.py:843-845 路径 (REST l2Book，无逐帧 prev) **保留** `orderbook_imbalance` 作降级 (无状态时只有静态比可用)，但 `orderbook_monitor.book_intent` 在有 WS 帧时优先 (app.py:841 `book_imb = self.orderbook_monitor.book_intent(coin, now) or orderbook_imbalance(...)`).

**flow_predictor.py:18-24 自身整改**：`orderbook_imbalance` 裸下标改 `_f(b.get("px"))*_f(b.get("sz"))` (数据质量)，保留函数(REST 降级 + 既有 import 零孤儿)。

---

## C.2 flow_acceleration 前置平滑

**目标**：`flow_acceleration` (flow_predictor.py:67-72) 现为裸两半窗速度差(2 阶导)，无平滑，单笔大单即抖动。前置 EMA 平滑净流向速度序列再求差。

**现状 (file:line)**：`flow_predictor.py:58-72` `_vel`/`flow_velocity`/`flow_acceleration`，`_flow` deque 存原始 `(ts, delta)`。

**改动 (file:line)**：
- `FlowPredictor.__init__` (flow_predictor.py:47-52) 新增 `ema_alpha: float = 0.3`、`min_accel_samples: int = 8`。
- 新方法 `flow_acceleration(coin, now_ms) -> float | None`：
  1. 把窗口 `[now-window, now]` 按固定 bin (如 `window_ms/10`) 聚合成净流向速度序列 (numpy 向量化分箱)。
  2. 非空 bin 数 `< min_accel_samples` → 返回 `None`(样本不足降权/不预测，诚实)。
  3. 序列过在线 EMA (`alpha`) 平滑 → 取后半 EMA 均值 − 前半 EMA 均值 = 平滑加速度。
- `predict` (flow_predictor.py:74-97)：`accel = self.flow_acceleration(...)`；`accel is None` → accel_sig 视为 0 且 score 仅由 book+oi 构成，并在 `reason` 标注「流加速样本不足」。**签名兼容**：返回类型 `float|None` 是放宽，旧调用方 app.py:836 `abs(flow_acceleration(...))` 需改 `abs(... or 0.0)`。

**防双计**：accel 仍只在 predict 内权重 0.45，平滑不改权重结构。

---

## C.3 HL 路径接方向化 OI (一行级)

**目标**：app.py:848-853 HL 前瞻路径用裸 `oi_change` 比率 `(chg[0]-chg[1])/chg[1]`，**未方向化** (OI↑不分多空建仓，语义错)。`oi_directional_velocity` 已存在已测 (oi_velocity.py:14-30)。

**改动 (file:line)**：`src/smc_tracker/app.py:848-853`。

替换为：取 `oi_now, oi_past = chg[0], chg[1]` + `price_now, price_past`(同窗口价，复用 `self._mids[coin]` / `store` 近窗收盘或 `self._last_close`)，调 `oi_vel = oi_directional_velocity(oi_now, oi_past, price_now, price_past)`。需在 `predict` 前确保 price_past 取得 (无价史→沿用 0.0，predict 内 oi_sig=0)。导入：app.py 顶部加 `from .signals import oi_directional_velocity` (signals/__init__.py:18 已导出)。

**防双计**：oi_vel 仅作 predict 的 `oi_velocity` 入参(权重 0.20)，与 funding/独立 OI 列不重叠 (与 oi_velocity.py:8-9 docstring 一致)。

---

## C.4 funding_extreme z-score → 滚动窗口经验分位

**目标**：`funding_extreme_signal` (funding_extreme.py:16-37) 用全历史等权 z-score，假设高斯；funding 厚尾 + 长历史稀释近期极值。改滚动窗口经验分位(稳健、无分布假设)。

**现状 (file:line)**：`funding_extreme.py:26-37` 全样本 mean/std → `-tanh(z/2)`。两处消费：harmonic_forward.py:101 (kwargs `min_samples`)，signals/__init__.py:17 导出。

**改动 (file:line)**：`src/smc_tracker/signals/funding_extreme.py:16-37`，签名**向后兼容扩展**：

```
def funding_extreme_signal(
    funding_now, funding_history, *, min_samples=20,
    window=240, method="quantile"      # 新增 kw，默认仍可被旧调用方无参命中
) -> float
```

逻辑：取 `funding_history[-window:]`；样本 `< min_samples` → 0.0。经验分位 `p = (#hist <= funding_now)/n ∈[0,1]` (numpy `np.searchsorted` 向量化)；映射 `sig = -tanh(k*(2p-1))` (p→1 即极高 funding→看跌负；k≈1.5 调灵敏度)。常量历史 (全相等) → 退化分位 0.5 → sig=0 (诚实)。`method="zscore"` 保留旧路径供平价对照测试。

**防双计**：funding 维度仍只在上层(harmonic_forward `__call__` 返回 funding_extreme、ConfluenceAggregator)单列，**不进** FlowPredictor.predict。harmonic_forward.py:101 调用无需改 (新 kw 有默认)。

---

## C.5 挂单墙抗 spoof (存活时间 + build/pull flap 计数)

**目标**：`detect_walls`/`_on_l2book` (orderbook_monitor.py:37-153) 把任何瞬现大额都当墙；spoof(虚挂诱导) 特征是**瞬现瞬撤**。加挂单存活时间过滤 + build/pull 横跳计数，过滤/降权可疑墙。

**现状 (file:line)**：`orderbook_monitor.py:142-153` 每帧用 `cur` 覆盖 `self._walls[coin][side]`，build/pull 即时 emit，**不记**墙首现 ts、不计同价位反复 build/pull。

**改动 (file:line/新增结构)**：
- `__init__` (orderbook_monitor.py:93-101) 新增：
  - `self._wall_born: dict[str, dict[str, dict[float, int]]]` — (coin,side,px)→首现 ts。
  - `self._wall_flap: dict[tuple[str,str,float], deque]` — (coin,side,px)→近 build/pull 事件 ts deque(maxlen=8)。
  - 参数 `min_lifetime_ms: int = 3000`、`max_flap: int = 4`、`flap_window_ms: int = 30_000`。
- `_on_l2book` build 分支 (orderbook_monitor.py:144-148)：墙新现先记 `_wall_born`，**不立即** emit；`_emit` 仅在该 px 存活 `>= min_lifetime_ms` (下一帧仍在且首现至今超阈) 时触发 → 过滤瞬现 spoof。
- pull 分支 (orderbook_monitor.py:149-151)：记 flap 事件 ts；近 `flap_window_ms` 内 build+pull 次数 `>= max_flap` → 标记该 px 为 spoof，emit 时 `event["spoof"]=True` 且**不计入** `confirming_wall`/`book_intent` 的看涨/看跌权重 (质量门控，非新增分数)。
- `confirming_wall` (orderbook_monitor.py:184-211)：跳过 spoof 标记墙 + 要求 `now - born >= min_lifetime_ms` (存活墙才算确认意图)。
- 新只读 `wall_quality(coin, side, px) -> dict` 供 dashboard/测试。

**(可选) CVD⟂price absorption 背离** — 列为子任务 C.5b，本迭代**不强制**：新纯函数 `absorption_divergence(cvd_series, price_series)`(放 microprice.py)，价创新高但 CVD 不跟 = 吸收背离(前瞻反转)。若实现，作 ConfluenceAggregator 独立列，**不进** predict (防双计)。建议先验证 CVD 数据源可得性(第一性原理实证)再排期。

---

## C.6 TDD 测试计划 (合成数据、确定性、断言要点)

全部用合成确定性数据，`./.venv/bin/python -m pytest -q` 须全绿 (基线 357 passed)。指标类加 golden / 可选 TA-Lib 平价 (`importorskip`)。

**`tests/test_microprice.py`** (C.1)：
- `queue_imbalance`：买盘 size 远厚 → +接近1；对称 → 0；空盘 → 0；远档大额虚挂不应翻转符号(只取 depth 档)。
- `micro_price`：bid_sz≫ask_sz → micro 趋近 ask_px、tilt>0(买压看涨)；对称 → tilt=0；零量 → tilt=0 不除零。
- `ofi_delta`：bid px 上移 → e_b=+cur_bid_sz；bid 同价 sz 增 → 正；bid 撤离(px 下移) → -prev_bid_sz；ask 镜像断言；混合帧手算 golden。
- `OFITracker`：首帧返 0 且记 prev；多帧累积净买 → normalized→+1；买卖抵消 → ≈0；窗口滚出旧帧后衰减。

**`tests/test_flow_accel_smooth.py`** (C.2)：
- 样本 `< min_accel_samples` → `flow_acceleration` 返 None，且 `predict` 不因 accel 崩(score 仅 book+oi)。
- 注入单笔尖峰：平滑后 |accel| 显著 < 未平滑裸 2 阶导 (断言降抖)。
- 稳定加速流入序列 → accel>0、predict direction='long'。
- 兼容：app 调用 `abs(flow_acceleration() or 0.0)` 不抛。

**`tests/test_oi_directional_wiring.py`** (C.3)：
- 单测已存在 `oi_directional_velocity`；新增**接线断言**：构造 mock store/mids，OI↑价↑ → predict 收正 oi 贡献(direction 偏 long)；OI↑价↓ → 偏 short；无价史 → oi_sig=0 不崩。

**`tests/test_funding_extreme.py`** (C.4，扩展现有)：
- 厚尾历史(多数近 0 + 少数极值)：经验分位法对当前极高 funding 给强负 sig，且 |sig| > 同数据 z 法 (稳健性断言)。
- `method="zscore"` 回退路径数值 == 旧实现 (回归保护)。
- 常量历史 → 0.0；`window` 截断只用近窗(注入远古极值不应再触发)。
- harmonic_forward 旧调用(无新 kw)签名兼容 smoke。

**`tests/test_orderbook_spoof.py`** (C.5)：
- 瞬现瞬撤(下一帧即消、存活 < min_lifetime_ms)→ **不** emit build 信号(spoof 过滤)。
- 同 px 在 flap_window 内 build/pull ≥ max_flap → event["spoof"]=True 且 `confirming_wall` 跳过。
- 真实墙(持续 ≥ min_lifetime_ms 多帧)→ 正常 emit、confirming_wall 命中。
- `book_intent` 不把 spoof 墙计入看涨权重。

**防双计回归** `tests/test_no_double_count.py` (新增护栏)：
- 断言 `FlowPredictor.predict` 分数仅由 accel+book_intent+oi 构成，funding 不影响 predict 输出 (传不同 funding 不改 predict 分)。
- 断言 OFI/queue/micro 仅经单一 `book_intent` 出口进 predict(不并列)。

**编译**：所有新/改文件 `python -m py_compile`。

---

## C.7 风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| OFI 对 HL l2Book 逐档语义假设错(最优档 px/sz 含义) | **第一性原理先实证**：跑最小脚本打印连续 l2Book 帧确认 px 升序/sz 单位再写(CLAUDE.md 三.3) | OFITracker 不挂载，`book_intent` 退回 `orderbook_imbalance` |
| EMA/分箱引入延迟使 accel 滞后 | bin 数与 alpha 可调；保留窗口短 | `flow_acceleration` 切回裸 2 阶导(保留旧逻辑分支) |
| 经验分位改变 harmonic_forward 既有 funding 行为 | `method="zscore"` 默认对照、单测回归锁旧值；可配置默认 method | 默认 `method` 切 `"zscore"` 即恢复 |
| spoof 过滤误杀真实快墙(冰山) | `min_lifetime_ms` 保守(3s)、仅过滤+标记不删数据 | 阈值设 0 / max_flap 设 ∞ 关闭门控 |
| 签名变更破坏调用方 | 全部新增为带默认 kw / 放宽返回类型；保留旧函数零孤儿 | git revert 单文件 |
| 性能(逐帧 OFI 在热路径) | numpy 向量化 + O(1) 在线更新 + deque maxlen 限内存 | — |

**接入自查 (零孤儿)**：实现后 `grep -rn "OFITracker\|micro_price\|queue_imbalance\|book_intent\|wall_quality" src/` 确认每个新符号有运行时调用方 + 从 `signals/__init__.py` 导出 (microprice 三函数 + OFITracker)。`detect_walls`/`orderbook_imbalance` 保留(REST 降级)非孤儿。

---

## C.8 本段涉及/修改的文件清单 (跨路线冲突检测)

**新建**：
- `src/smc_tracker/signals/microprice.py` (C.1，可选 C.5b absorption)
- `tests/test_microprice.py`、`tests/test_flow_accel_smooth.py`、`tests/test_oi_directional_wiring.py`、`tests/test_orderbook_spoof.py`、`tests/test_no_double_count.py`

**修改**：
- `src/smc_tracker/signals/flow_predictor.py` (C.1 整改 orderbook_imbalance 裸下标；C.2 flow_acceleration 平滑 + __init__ 新参 + predict None 处理)
- `src/smc_tracker/signals/funding_extreme.py` (C.4 经验分位 + 新 kw)
- `src/smc_tracker/signals/oi_velocity.py` (无逻辑改；若加 docstring 链接 app 接线则微调)
- `src/smc_tracker/monitor/orderbook_monitor.py` (C.1 挂 OFITracker/book_intent；C.5 spoof 存活+flap)
- `src/smc_tracker/signals/__init__.py` (导出 microprice 符号 + OFITracker)
- `src/smc_tracker/app.py` (C.1 book_intent 优先；C.2 `or 0.0` 兼容 line 836;C.3 接 oi_directional_velocity line 848-853 + 顶部 import)
- `tests/test_funding_extreme.py` (C.4 扩展)

**冲突高风险点 (供 orchestrator 排他锁)**：
- `src/smc_tracker/app.py` — 多路线常改的热点，C 仅触及 line 31(import)/836/841-845/848-853，应与其他路线对 app.py 的改动**串行或文件级锁**。
- `src/smc_tracker/signals/flow_predictor.py` 与 `signals/__init__.py` — C.1+C.2 双改，须同一 agent 串行处理避免半完成。
- `src/smc_tracker/monitor/orderbook_monitor.py` — C.1+C.5 双改同文件，同 agent 串行。

**与其他路线的契约不变量**：`book_imbalance()` 返回 dict 仅**新增键**(imbalance 保留)；`funding_extreme_signal`/`oi_directional_velocity` 签名仅**新增带默认 kw**；`confirming_wall` 返回 dict 形状不变(仅多过滤)。任何路线消费这些接口无需改动。