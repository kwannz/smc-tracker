# 谐波系统"前瞻性"全面优化 — 设计文档 v2（QA 修订 + 全量宇宙统一 + 每币分类）

- 日期：2026-06-24（v2 经四轮 QA 审计 + Bitget 数据源实证后重写）
- 方案：**A 接活前瞻骨架 + 全量宇宙统一**（增量，复用现成件，零外部依赖）
- 约束：本地实现 + TDD 验证；**部署须用户明确批准**（memory `harmonic-redesign-state`）。

## 0. QA 审计结论（v1 被证伪的前提 → v2 修正）
四轮 QA（file:line + Bitget API 实证）发现 v1 计划的致命隐患，v2 全部纳入修复：

| # | 隐患（QA 实证） | v2 修复 |
|---|---|---|
| **宇宙错配**(BLOCKER) | 谐波宇宙=Bitget vol top-12+TradFi；前瞻信号全建立在 `meme_markets`(~22 meme)，近乎不相交 → 前瞻分量对谐波币≈0 数据(`app.py:686` vs `:800`) | **R2 全量统一**：OI 监控 symbol 集从 meme 改复用谐波 `vol_c2s`；新接 Bitget trade/books WS（实证全 TradFi 可采） |
| **H1 forming 语义反转**(高) | `evaluate_due` 测记录时刻起价格方向；forming PRZ 可离现价任意远且 bullish=价先跌进 PRZ → 方向被符号反转、测随机漂移(`review.py:248-261`) | forming **不在投影时记**；反转预测推迟到**价格触达 PRZ(实时逼近)才记** |
| **H_price 幸存者偏差**(高) | `_record_pred` bg_px 走 meme-only `coin_to_symbol`、hl_px 走 HL 名 → 谐波币静默丢失(`review.py:171-176`) | 用谐波 `harm_c2s`({coin:symbol}) 取 bg_px；hl_px 经 `canon_to_hl`(app.py:163) 归一；落库前记 skip 计数 |
| **H6 热路径同步写**(高) | P1-3 在 `_on_all_mids`(WS 主循环)调 `_record_pred`=21 条同步 SQL(`review.py:213-225`) | 热路径只做 O(1) 命中判定；命中 `put_nowait` 入队，周期 worker 出队写库 |
| **H2 completed=反转后延续**(中高) | completed 的 D 已确认 pivot=价格已反转 3+ 根(`patterns.py:15-28`)→ 短 horizon 系统性偏高 | kind 标注**"反应式"**；优先看长 horizon + market-neutral |
| **H7 PRZ 缓存无失效**(中高) | 15min 整体覆盖、无 TTL、无穿越作废；±5% 宽带刷屏 | per-entry TTL(>2×interval)+ 穿越作废 + 稳定 round(prz_mid) 冷却 key |
| **H4 OI 非方向量**(高) | `oi_change` 返回持仓量标量无方向 | OI 信号 = **sign(oi_delta)×sign(price_delta)** 组合(OI升+价升=新多) |
| **H5 双重计数**(中) | `FlowPredictor.score` 已含 imbalance+OI，forward_mult 再单列=算两遍(`flow_predictor.py:82`) | **择一来源**：forward_mult 直接用 FlowPredictor.score 作合成前瞻分量，不再裸算 imbalance/OI |
| **H3-dedup 浮点去重失效**(中) | `round(prz_mid,4)` 每轮微抖 → 去重退化 | 去重 key 用结构指纹 `(coin,tf,pattern,direction,D_pivot_idx)` |
| **funding=0 纯股票**(中) | TSLA/AAPL/META… Bitget funding 恒 0（实测） | per-coin `has_funding` 守卫，funding 信号跳过这些币 |

## 1. 地基：每币信号画像（CoinSignalProfile）—— 用户核心要求"每币独立计算分类归类"
**新模块 `signals/coin_profile.py`**：每币计算一次、缓存，驱动"该币该算哪些前瞻信号 + 诚实标注"。
```python
@dataclass(slots=True)
class CoinSignalProfile:
    coin: str; symbol: str
    asset_class: str        # 'crypto' | 'tradfi_commodity' | 'tradfi_stock'（复用/扩 asset_class.py）
    has_oi: bool            # OI 可采（≈全部 True，实测）
    has_funding: bool       # funding != 0（纯股票代币 False）
    has_taker: bool         # trade WS 已订阅该币
    has_l2: bool            # books WS 已订阅该币
def build_profile(coin, symbol, ticker_row, subscribed_taker, subscribed_l2) -> CoinSignalProfile
```
- **分类来源**：`asset_class()`（crypto/tradfi）再按 tickers 的 funding!=0 细分 commodity/stock；`has_funding=ticker.fundingRate!=0`。
- **用途**：forward_confirm 按 profile 决定哪些分量参与；UI/review 按 profile 诚实标注（不对 funding=0 币claim funding 确认）；review 可按 asset_class 分桶看命中率。
- **单测**：crypto/commodity/stock 三类合成 ticker → profile 字段正确；funding=0 → has_funding False。

## 2. R2 全量信号宇宙统一（实证可行，复用现成件）
- **OI/funding（接现成）**：`bitget_oi_monitor` symbol 集从 `meme canon`(`app.py:627`) 改复用谐波 `vol_c2s`(`app.py:660`)。ticker WS 已实时推 OI/funding，只缺：
  - OI 速度/加速度（一/二阶导）计算层（新，纯计算）。
  - funding 极值（z-score/分位）+ 去重（8h 粒度，按 has_funding 守卫）。
- **taker CVD（中等，新 WS handler）**：Bitget WS 加 `trade` channel（client 已支持任意 channel，`ws_client.py:30`）；累积 per-coin taker buy−sell delta → 喂 FlowPredictor.push（替/补 meme-only 源）；REST 补 `fills()` 兜底；新落库表/内存序列。
- **l2 挂单意图（中等，新 WS handler）**：Bitget WS 加 `books5/books15` channel；算盘口失衡（复用 `flow_predictor.orderbook_imbalance`）；REST 补 `merge_depth()`。
- **统一后**：FlowPredictor/OI/funding/l2 对谐波 top-12（含 TradFi 微观结构）真有数据。TradFi 仅 funding 按币跳过。

## 3. 前瞻置信（forward_confirm，按 profile 门控，forming+completed 都生效）
**新模块 `signals/forward_confirm.py`**：纯函数
```python
def forward_mult(direction, profile, *, flow_score=None, oi_dir=None, funding_extreme=None,
                 l2_imbalance=None) -> tuple[float, str]   # (乘子, 诚实 note)
```
- **分量（按 profile 门控，缺数据=中性，不佯装）**：
  - flow_score（FlowPredictor.score∈[-1,1]，**已含 accel+imbalance+OI 三合一**，作主前瞻分量，避免双计）。
  - oi_dir = sign(oi_delta)×sign(price_delta)（方向化，非裸 OI 量）。
  - funding_extreme（仅 has_funding；拥挤反向确认）。
  - l2_imbalance（仅 has_l2；与 flow_score 的 imbalance 二选一，避免双计）。
- 合成 `mult = clamp(1 + Σ wᵢ·分量ᵢ, 0.80, 1.30)`，权重初值保守、上线后用 review 闭环回校。
- **接线**：`harmonic_monitor.refresh` 解除 `if s.completed`，对 completed+forming 都应用；`forward_note` 存入 setup。
- **单测**：profile 门控（funding=0 币不含 funding 分量）；缺数据→1.0；double-count 防护（不同时用 flow_score 与裸 imbalance/OI）。

## 4. review 闭环（R1，诚实度量，QA 修复版）
- **completed**：`_periodic_harmonic_board` 调 `_record_pred(coin,"谐波-反应式",direction)`；价格用 harm_c2s/canon_to_hl 修覆盖；去重用结构指纹（D_pivot_idx）。明确标注"反应式/反转后延续"，优先长 horizon + market-neutral。
- **forming**：**不在投影时记**；在 §5 实时逼近触达 PRZ 时记 `"谐波-逼近"`。
- **产出**：`accuracy_report` by_kind/by_asset_class 出谐波 forward 命中率 → 一切优化的实证地基。

## 5. forming 实时逼近 PRZ（真提前量，QA 修复版）
- 缓存 forming PRZ：`self._forming_prz: {coin: [(lo,hi,dir,tf,pattern,D_idx,ts)]}`，**per-entry TTL**(>2×interval)。
- 价格源：**HL allMids 仅覆盖 HL 子集** → 对 Bitget-only/TradFi 用 `oi_monitor.ticker(sym)` 的 Bitget 价（`app.py:226` 已示范）；逼近检测挂在合适的高频价更新点。
- 触达 PRZ 带 → 校验未穿越失效 → `put_nowait` 入队（**不在热路径同步写库**）→ worker 出队推 🎯逼近告警 + `_record_pred("谐波-逼近",dir)`。冷却 key=round(prz_mid)，时长>>interval。
- **单测**：价穿入带触发一次；穿越作废；TTL 失效；热路径无同步写。

## 6. 清理
- KNN 降级为展示（`trade_setup.py:289-292` 停止乘性调权，保留 knn_note）。

## 阶段顺序（QA 驱动的诚实排序）
1. **P0 地基**：CoinSignalProfile（§1）+ R1 completed 诚实度量（§4 completed 部分，含价格修复/去重）。先量出基线。
2. **P1 统一**：R2 全量信号宇宙（§2）—— OI 速度/funding 极值（接现成）先，taker/l2（新 WS）后。
3. **P2 接活**：forward_confirm 按 profile 进置信（§3，forming+completed）+ forming 实时逼近（§5）。
4. **P3 清理/校准**：KNN 降级（§6）；用 P0 闭环数据回校 forward 权重。

## 验证
- `./.venv/bin/python -m pytest -q` 全绿（每模块合成单测，确定性；指标类 TA-Lib 平价校验）。
- `py_compile`；本地跑 → `accuracy_report` by_kind/by_asset_class 出谐波命中率；dashboard 按 profile 展示前瞻确认。
- 诚实：缺数据优雅降级不崩、不阻塞热路径；不对无数据币佯装确认。

## 非目标 / YAGNI
- 不重写 swing/检测核心（后续谨慎试点）。
- 不引入新外部依赖/API key（全 Bitget keyless 公开）。
- 不部署（待用户批准）。

## 风险
- 热路径：逼近检测 O(1) 守卫 + 入队，实测 `latency.record` 无回退。
- l2/trade 新 WS 负载：限谐波 top-12，监控连接稳定。
- forward 权重初值凭经验 → P0 闭环数据回校，先窄区间。
