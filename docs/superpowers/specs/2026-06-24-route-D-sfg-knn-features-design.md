# 路线 D — SFG-KNN 特征强化 (实现级 spec 段)

## (1) 目标与现状

**目标**：让 KNN 从「11 维共线原始指标」升级到「11 原始 + 10 个 SFG 设计 alpha 因子 = 21 维」。本轮**只变一个变量**（特征空间），保留 z-score、保留固定 horizon 标签，不上 triple-barrier。

**现状 (file:line)**：
- `src/smc_tracker/indicators/knn.py:17-18` — `FEATURE_NAMES` 11 项；`:21-41` `feature_matrix(candles)->(n,11)`；`:34-40` 逐行 loop；`:55-69` `fit` 固定 horizon 标签 `y=1 if c[i+h]>c[i] else 0`，`:58-59` 整行有限性过滤，`:65-67` z-score。
- `src/smc_tracker/indicators/atr2_signals.py:28` — 唯一已移植 SFG 因子，**只返回末值** `magnified_mom[-1]`（`:115` 已有全数组 `magnified_mom`，但 `:118` 取 `[-1]`）。这是 series 化要补的关键缺口。
- `src/smc_tracker/indicators/technical.py:14-22` `ohlcv_arrays`；`:25-30` `sma`（cumsum）；`:33-47` `ema`（首观测种子）；`:50-65` `_wilder`；`:107-114` `bollinger`；`:114` `atr`。**无 `wma`/`hma`/`rolling_max`/`rolling_min`**（AMI/AI_ST/DMHA 需要）。
- `src/smc_tracker/indicators/__init__.py:16,25,40` — `feature_matrix`/`atr2_confirmation` 导出范式。
- `tests/test_indicators.py:288-292` — `test_feature_matrix_shape` 断言 `(150, 11)`，**本轮必改 21**。
- `src/smc_tracker/util.py:9` `to_float`（拒 NaN/inf）；无 `level_factor`。

**WF1 钉死的总约束（务必遵循）**：
- 因子顺序固定 `[lrsd,gpi,vap,pdbb,pivot,ami,atr2,msfvg,ai_st,dmha]`（8 反转 + 2 趋势）。
- **NaN 是显式「不可交易/缺失」哨兵，绝不 impute 0**（0 是真实中性读数）。warmup 行 NaN → 被 `knn.py:58` `np.all(np.isfinite)` 干净丢弃。
- 每因子已在内部 clamp 到 `[-1,1]`；series 函数 warmup 段 `=nan`。
- **诚实**：KNN 项目自承 ≈ 随机基线（`knn_validator.py:3`、`trade_setup.py:291`），富化特征**不预设提升 PnL**，靠 review 闭环实测，不夸大为 alpha。

---

## (2) 逐项改动

### 2.1 新建子包 `src/smc_tracker/indicators/sfg/`（组织决策）

**决策（多假设对比）**：
- 方案 A：9 个新因子各自单文件平铺进 `indicators/`（现 13 文件 → 22 文件，目录膨胀，共享 helper 难定位）。
- 方案 B（**选用**）：新建 `indicators/sfg/` 子包，每因子一模块 + `_common.py` 放共享 helper。理由：10 个 SFG 因子是同一来源/同一签名范式/共享 `level_factor` 内核，内聚成包符合 CLAUDE.md「去重 + 公共 helper 集中」；`atr2_signals.py` 保留原位（trade_setup 已引用 `..indicators.atr2_signals`，不动以免跨路线破坏），其 series 兄弟函数加在 `sfg/atr2.py` 里复用现有标量逻辑。

```
src/smc_tracker/indicators/sfg/
  __init__.py        # re-export 全部 *_series + level_factor_series
  _common.py         # level_factor_series, wma, hma, rolling_max/min, first_obs_ema, _min_bars 守卫
  lrsd.py  gpi.py  vap.py  pdbb.py  pivot.py  ami.py  atr2.py  msfvg.py  ai_st.py  dmha.py
```

### 2.2 公共 helper `sfg/_common.py`（去重核心）

**`level_factor_series(close: np.ndarray, support: np.ndarray, resistance: np.ndarray) -> np.ndarray`**
- 6 个反转因子共享内核（LRSD/PDBB/PIVOT/MSFVG 及 GPI/VAP/AMI/ATR2 的归一化变体的同型 clamp）。WF1 `continuous_factors.rs:129-138`。
- 公式：`half=(R-S)/2; mid=(S+R)/2; raw=(mid-c)/half`；`half<=0 或任一非有限 → nan`；`clamp(raw,-1,1)`。向量化全 `np.where`。复杂度 O(n)。
- **放置决策**：放 `sfg/_common.py` **不放 util.py** —— 这是指标域 clamp 逻辑（依赖 NaN-哨兵语义），util.py 是跨域通用（`to_float`/时间）。`util.to_float` 只用于摄入校验，不在此热路径。spec 备选：若未来 review 发现别处复用可再上提 util，本轮不做。

辅助（WF1 要求、technical.py 缺）：
- `wma(x, n)` — 线性加权 MA，权重 `1..n`，warmup nan（AMI/AI_ST/DMHA）。golden: `wma([1,2,3,4],3)=[nan,nan,14/6,20/6]`。
- `hma(x, n)` — Hull MA = `wma(2*wma(x,n//2)-wma(x,n), round(sqrt(n)))`（DMHA smoothlen=6→half=3,root=2）。
- `rolling_max(x, n, min_periods=1)` / `rolling_min` — AMI 通道（2000 窗，`min_periods=1`，前缀渐增）。用 `np.maximum.accumulate` 分段或 `sliding_window_view` + 头部渐增补丁。
- `first_obs_ema(x, span_float)` — GPI 用：`alpha=2/(max(span,1)+1)`，首个有限值播种，NaN 上 carry-forward（串行 scan，O(n) 单遍）。

### 2.3 九个新 `*_series` 函数（签名 + 公式引用 + 复杂度）

所有签名统一：`def <name>_series(candles: list[Any], **params) -> np.ndarray`，长度 `n`，warmup `=nan`，内部 `arrs=ohlcv_arrays(candles)`，全 numpy 向量化（除 GPI/AMI 必需的串行 scan）。

| 因子 | 签名（默认参数）| 公式来源（WF1）| 复杂度 | 关键算法 |
|---|---|---|---|---|
| `lrsd_series` | `(candles, length=100, vol_ma_len=6)` | factor_formula `clamp((S+R-2c)/(R-S))`；`S=last down-fractal low[i-3]`,`R=last up-fractal high[i-3]` | **低** | 5-bar Williams 分型 + 体积闸 `vol[i-3]>SMA(vol,6)[i-3]`；前向填充 4 条 zone 边界 → `level_factor_series(c,S,R)`。`indicators/sfg/lrsd.py` |
| `gpi_series` | `(candles, span0=1960.0, span1=1973.0, tfm=1.0)` | `clip(-2*(close-band_mid)/(band_upper-band_lower))`；band=min/max of 3 `first_obs_ema(close, span/tfm)` | **低** | span_mid=`sqrt(span0*span1)`；3 个 first_obs_ema → band → `level_factor` 同型（注意 **GPI 符号已含负号**：close 在带下=+1）。`sfg/gpi.py` |
| `vap_series` | `(candles, length=150, rows=150, value_area_pct=0.70)` | `clamp(-2*(close-poc)/abs(vah-val))` | **中** | 每 bar 滚动窗 `linspace(lo,hi,rows+1)` 边界 + 差分数组分桶体积 + cumsum；last-tie argmax POC；从 POC 外扩到 `value_area_pct*总量` → vah/val。逐 bar O(rows)，总 O(n·rows)。`sfg/vap.py` |
| `pivot_series` | `(candles, left_bars=10, right_bars=10)` | `clamp((top+bot-2c)/(top-bot))`；top/bot=前向填充的已确认 pivot high/low | **低** | pivothigh/low（中心 bar 在 `c+right` 确认发射，**绝不用未确认尾 pivot**——会注入未来）；ffill → `level_factor_series`。`sfg/pivot.py` |
| `ami_series` | `(candles, momentum_window=20, channel_lookback=2000)` | `clamp((lower+upper-2*pred)/(upper-lower))`（NEGATED 通道位）| **中** | MLMI：在每个 fast/slow WMA(5/20) 交叉事件存 ±1 标签（`close>=prev_event_close`），running KNN 累积 sum=`pred`（warmup=0 非 nan）；`rolling_max/min(pred,2000,min_periods=1)` 通道。串行 scan。`sfg/ami.py` |
| `atr2_series` | `(candles, trend_length=8, smoothness=20, magnify=3.0)` | `clamp(-confirmation/volatility)`；`confirmation=SMA(mom/std(mom,s),s)*magnify`,`volatility=std(mom,s,ddof=0)` | **低** | **复用 `atr2_signals.py` 现有数组**：把 `:71-115` 的 `mom/volatility/magnified_mom` 提为内部 `_atr2_arrays(candles,...)`，`atr2_confirmation` 取 `[-1]`，`atr2_series` 返回 `clamp(-magnified_mom/volatility)` 全数组。**注意符号是 reversal-NEGATED**，与 smc 自有 ×0.80 反向罚的 atr2 是**不同物**（WF1 PORTING CAVEAT），勿混。`sfg/atr2.py` |
| `msfvg_series` | `(candles, swing_size=20, fvg_history=7)` | 三 case：both→`level_factor(c, bull_top, bear_bot)`；bull-only→`clamp((bull_top-c+half)/(2*half))`；bear-only→`clamp(-(c-bear_bot+half)/(2*half))`；neither→nan | **中** | FVG 状态机（FIFO ring=8）严格前向：事件用 `h[i-3],l[i-1],l[i-3],h[i-1]`，填充/收缩用当前 bar low/high；最近 bull/bear FVG 上下界。`sfg/msfvg.py` |
| `ai_st_series` | `(candles, length=20, factor=1.5, price_len=2, st_len=90, k=1)` | k=1：`ai_st=(price_wma>st_wma)?+1:-1`；fallback `clamp(tanh(100*(close-st)/max(abs(close),1e-12)))` | **中** | 体积加权 base MA + ATR(Wilder) SuperTrend ratchet（仅读 base/atr/close/close[-1]/prev state）；`price_wma=wma(close,2)`,`st_wma=wma(st,90)`；**趋势组 +1=上涨**（与反转组反号）。`sfg/ai_st.py` |
| `dmha_series` | `(candles, fast=12, slow=25, smooth_len=6)` | `dmha_state∈{-1,0,+1}` = sign(ha_close−ha_open) of HA-candle **on MACD series** | **中** | `gf` Ehlers IIR 滤波(out[i]依赖src[i]+out[i-1]) 替代 EMA；`raw_macd=HMA((gf(HLCC4,12)-gf(HLCC4,25))/gf(h-l,25)*100, 6)`；对 MACD 序列再做 HA（`open_macd[i]=macd[i-1]`），取方向。**趋势组**。`sfg/dmha.py` |

**HLCC4** = `(h+l+c+c)/4`。每模块文件顶部中文 docstring 引 WF1 `factor_formula` + rust_anchors 行号 + lookahead 诚实标注。

### 2.4 PDBB 单独评估（高复杂度）

**决策（诚实标注）**：PDBB 全量需 ZigZag ring(50) + MSS 5-swing 门 + breaker block 形成（rust `pd_array_breaker_block.rs:139-663`，~500 行状态机），**本轮不全量移植**。

**本轮替代（简化 premium/discount 带，明确标注）**：`pdbb_series` 只移植 factor 实际消费的两列 `pd_premium_top / pd_discount_bottom` = **「活跃 block 期间最近确认的 ZigZag HH/LL」**。简化为：复用 `pivot_series` 已有的 confirmed pivot high/low ffill 作为 HH/LL 代理（WF1: factor 仅需这两条，不需 breaker box / TP / MSS），`pdbb_series = level_factor_series(c, pd_discount_bottom=last_LL, pd_premium_top=last_HH)`。

- **诚实标注（写入模块 docstring + spec）**：这是 PDBB 的 **premium/discount 带近似**，非完整 breaker-block 复现；与 PIVOT 因子高度相关（同源 pivot），KNN z-score 后两列近共线 —— 本轮接受（一次只变一个变量），review 闭环若显示该列零增益则后续移除或上全量 ZigZag。`drop_one` 式消融留待路线后续。
- **不预设**：不声称 PDBB 带来独立 alpha。

### 2.5 `knn.py` 拼接 11→21（不替换）

`src/smc_tracker/indicators/knn.py`：
- `:17-18` `FEATURE_NAMES` 追加 8 反转 + 2 趋势：`[..., "lrsd","gpi","vap","pdbb","pivot","ami","atr2","msfvg","ai_st","dmha"]`（顺序 = WF1 `factor_order`）。
- `feature_matrix` 重构（关键约束：**不能逐行调标量版**，必须 series）：在 `:23` 后一次性算 10 个 series 数组：
  ```
  from .sfg import (lrsd_series, gpi_series, vap_series, pdbb_series,
                    pivot_series, ami_series, atr2_series, msfvg_series,
                    ai_st_series, dmha_series)
  sfg = np.column_stack([lrsd_series(candles), ..., dmha_series(candles)])  # (n,10)
  ```
  逐行 loop `:34-40` 末尾把 `sfg[i]` 拼到 `feats[i]`（11+10=21）。**series 不足 n（warmup nan）天然填 nan，被 fit 行过滤丢弃**。
- z-score 保留（`fit:65-67` 不动）—— SFG 因子尺度异质（如 ami pred ±large vs body∈[0,1]），z-score 是必需的，否则大尺度因子主导欧氏距离。
- 标签不动（`fit:61` 固定 horizon sign）。

**warmup 影响**：21 维 = 更长 warmup = 更少训练行。`ai_st`(st_len=90)、`ami/pdbb`(pivot right=10 + 通道) 是最长 warmup 驱动。**须验证** `len(rows)>=k`（`fit:62`）在典型 150-bar 仍成立；spec 要求测试用 ≥250 bar 合成数据以保证训练行充足（见 3.4）。

### 2.6 零孤儿接入

- `sfg/__init__.py` 导出 10 个 `*_series` + `level_factor_series` + `wma/hma`。
- `indicators/__init__.py`：新增 `from .sfg import (lrsd_series, ..., dmha_series)` 并加入 `__all__`。
- **运行时可达**：`feature_matrix`（已被 `engine.analyze`→`ta_signal`→`trade_setup` 消费，`knn_validator.fit`）即消费点，10 个 series 全部经 `feature_matrix` 进入 KNN 热路径 → 满足「全部代码用上」。
- grep 自查：`grep -rn "_series" src/smc_tracker/ | grep -v __pycache__` 确认每个 `*_series` 至少有 1 个非测试引用。

---

## (3) TDD 测试计划（合成数据，确定性）

**总范式**：每因子克隆 `tests/test_atr2_signals.py` 形状 —— `_Candle` slots 类(.o/.h/.l/.c/.v) + `_make_trending_up/down/sideways` 生成器 + 断言类。新建 `tests/test_sfg_<name>_series.py`（9 个）+ 改 `tests/test_indicators.py`。

### 3.1 共享 helper 测试 `tests/test_sfg_common.py`
- `level_factor_series`：用 WF1 LRSD golden 4 数值 oracle —— `S=95,R=110,c=95→+1`；`c=110→-1`；`c=102.5→0`；`c=200→clamp -1`（rust `continuous_factors.rs:933-976`）。断言 `np.allclose(rtol=1e-9)`。`half<=0→nan`。
- `wma([1,2,3,4],3)=[nan,nan,14/6,20/6]`（WF1 ai_st parity）。
- `ema` 同型 golden `ema([10,20,30],2)=[nan,15,25]`（已有 technical 可交叉，但 sfg 不复制 ema；wma/hma 独测）。

### 3.2 每因子 series 测试（断言要点，引 WF1 parity golden）
统一断言：(a) 返回 `len==n` ndarray；(b) warmup 段 nan；(c) 有效段 ∈[-1,1] 或 nan；(d) 方向语义 + golden 标量。
- **lrsd**：合成「在 i-3 放高峰 + `vol[i-3]>>SMA(vol,6)`」up-fractal，断言 confirm bar zone 触发、`res_top=high[i-3]`；factor golden 4 数值（同 3.1）。
- **gpi**：ramp close(base=30000,slope=5,n=200)，稳定上涨→price>band_upper→clamp **-1**（premium）；factor golden `{price_to_mid=-0.03,width=0.04}→+1`；`{+0.03,0.04}→-1`；`{0,0.04}→0`；`{-0.01,0.04}→+0.5`（WF1 `continuous_factors.rs:982-1010`）。**无 Pine oracle（确认 MANIFEST 缺）—— 纯算术 golden 足够**。
- **vap**：(1) POC golden：150-bar 合成窗，断言 last-bar POC = `mids[argmax_last]`；(2) factor golden `vap_val=99,vah=101,dist=-0.02,close=98→正`；`+0.02,close=102→负`（WF1 `:1012-1033`）。真实 BTC 可选：`vap_poc=73717.399`/`vap_poc_volume=29959.5700`（vapvol_5m.csv，importorskip fixture，非硬依赖）。
- **pivot**：手算 1 个 confirmed pivot-high + 1 pivot-low 的小合成序列，断言 `c==bot→+1`,`c==top→-1`,`c==mid→0`（rtol 1e-9）。**绝不读未确认尾 pivot**（专测一条：尾部新极值不应改变已发射值）。
- **ami**：上升趋势 pred 单调↑→接近通道上界→factor 趋负（NEGATED）；横盘→factor≈0；断言 pred warmup=0（非 nan）。store 须从 inception 播种（确定性 seeding 测试）。
- **atr2**：上升趋势 confirmation>0 → factor<0（reversal NEGATED，与标量 `atr2_confirmation` bias=long **反号**，专测断言 `sign(atr2_series[-1]) == -sign(confirmation)`）。复用 `atr2_signals` 现有测试不破坏（标量函数行为不变）。
- **msfvg**：factor golden bull-only `top=98,bot=95,close=100→(98-100+3)/6=+0.16667`；bear-only `top=105,bot=102,close=100→-0.16667`（WF1 `:1185-1206`，rtol 1e-5）。
- **ai_st**：k=1 → 输出严格 `±1`，且 `==sign(price_wma-st_wma)`；primitives golden `atr=[nan,3.5,4.25]`、`rma=[nan,15,22.5]`。**趋势组 +1=上涨**（上升趋势→+1）。
- **dmha**：输出严格 `∈{-1,0,+1}`；上升趋势→+1，下降→-1；warmup ≥220 bar 丢弃（gf/HMA 收敛）；容忍 idx-8 doji 1-ULP（测试避开该 bar 或 rtol 放宽）。

### 3.3 NaN 哨兵语义测试（横切）
- 退化窗（flat 价/zero range）→ 对应因子 nan，**不得为 0**（专断言 `np.isnan` 而非 `==0`）。
- candles 不足 → 全 nan ndarray（不 crash，不 None —— series 返回 nan 数组与 `feature_matrix` 契约一致）。

### 3.4 集成测试 `tests/test_indicators.py`
- `test_feature_matrix_shape`：`(150,11)` → **`(150,21)`**（WF1 强制）。
- 新增 `test_feature_matrix_sfg_columns`：21 列名 == `FEATURE_NAMES`；SFG 列在足够 bar 后非全 nan。
- 新增 `test_knn_fit_with_21_features`：≥250 bar 合成 → `fit` 返回 True（`len(rows)>=k=15`）；`predict_latest` 返回 dict（验证 warmup 增长未饿死训练集）。
- 回归：`engine.analyze(knn=...)`、`ta_signal`（0.15 knn 权重）、`knn_validator.validate_direction` 只读 `p_up/confidence`，特征数无关 —— 跑现有测试确认不破。

### 3.5 全量基线
`./.venv/bin/python -m pytest -q` 必须全绿（基线 357 passed → 加新文件后 >357）。`python -m py_compile` 每个新文件。

---

## (4) 风险与回滚

| 风险 | 缓解 / 回滚 |
|---|---|
| warmup 变长饿死训练集（21 维 → `fit` 返回 False）| 测试用 ≥250 bar；生产若 candle 不足，`fit:62` 已 return False 优雅降级（KNN 项 0.15 权重在 `ta_signal:126` 自动 drop，不阻塞）。回滚：`FEATURE_NAMES` 截回 11 + `feature_matrix` 去掉 column_stack。 |
| SFG series 移植 bug 注入 NaN/inf 污染整行 → 训练行全被过滤 | series 内部 fail-closed 用 nan（非 inf）；`util.to_float` 校验摄入；测试断言有效段 finite。回滚同上。 |
| PDBB 简化带与 PIVOT 近共线，零增益 | 已诚实标注；z-score 后冗余不会害（只稀释距离），不阻塞。后续 drop-one 消融决定去留。 |
| AMI/GPI 串行 scan 拖慢热路径 | 单遍 O(n)，与现有 `ema` 串行同量级；`feature_matrix` 已非每行调标量（一次算 series）。基线 compute ~1ms 不受冲击（feature_matrix 本就 O(n)）。 |
| ai_st/dmha 趋势组符号与反转组反号被误用 | FEATURE_NAMES 顺序固定 = WF1 factor_order；KNN 把 21 维当独立特征（不 sum），符号差异由 z-score 吸收。测试断言趋势组上升→+1。 |
| 跨语言数值漂移 | 用 WF1 纯算术 golden（rtol 1e-9 factor / 1e-4~1e-5 复合）；真实 fixture importorskip（零硬依赖）。 |

**整体回滚**：本路线全部新增在 `indicators/sfg/` 子包 + `knn.py`/`__init__.py`/2 测试文件的可逆增量；revert 三处编辑 + 删子包即回到 11 维基线。**部署须用户批准**（本 spec 仅本地实现）。

---

## (5) 本段涉及/修改文件清单（供跨路线冲突检测）

**新建**：
- `src/smc_tracker/indicators/sfg/__init__.py`
- `src/smc_tracker/indicators/sfg/_common.py`（level_factor_series, wma, hma, rolling_max/min, first_obs_ema）
- `src/smc_tracker/indicators/sfg/{lrsd,gpi,vap,pdbb,pivot,ami,atr2,msfvg,ai_st,dmha}.py`（10 文件）
- `tests/test_sfg_common.py`
- `tests/test_sfg_{lrsd,gpi,vap,pdbb,pivot,ami,atr2,msfvg,ai_st,dmha}_series.py`（10 文件）

**修改**：
- `src/smc_tracker/indicators/knn.py`（FEATURE_NAMES 11→21；feature_matrix column_stack 拼接；标签/z-score 不动）
- `src/smc_tracker/indicators/__init__.py`（import + `__all__` 加 10 个 `*_series`）
- `tests/test_indicators.py`（`test_feature_matrix_shape` 11→21；新增 sfg 列 / 21 维 fit 测试）

**跨路线冲突热点（需 orchestrator 串行化或文件分区）**：
- `src/smc_tracker/indicators/knn.py` 与 `indicators/__init__.py` —— 若其他路线也改 KNN/导出会冲突。
- `src/smc_tracker/indicators/atr2_signals.py` —— 本路线建议**仅提取 `_atr2_arrays` 内部 helper、不改 `atr2_confirmation` 公共签名/行为**（trade_setup `:269` 依赖）；若其他路线动 atr2 需协调。
- `tests/test_indicators.py` —— 共享测试文件，多路线追加测试有合并冲突风险。

**只读引用（不改，仅依赖契约）**：`indicators/technical.py`（ohlcv_arrays/sma/ema/atr）、`indicators/price_action.py`（pa_features）、`signals/{ta_signal,trade_setup,knn_validator}.py`、`indicators/engine.py`、`util.py`。