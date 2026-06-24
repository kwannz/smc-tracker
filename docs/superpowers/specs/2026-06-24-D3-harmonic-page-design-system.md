# D3 谐波页设计系统 + 原生重写规范(从 SMC 终端设计稿提取)

- 日期：2026-06-24（loop#4,Opus 规划）
- 来源：`/Users/zhaoleon/Downloads/网页设计方案讨论/SMC聪明钱追踪终端.dc.html` 谐波系统区块(line 287-585)
- 方案：D1 决定的**方案3 = 设计 token 提取 + 原生重写**(无 CDN/无依赖,符合 CLAUDE.md)。
- 用途：后续 D3 Sonnet 执行,把 `dashboard.py` 谐波页(`_HARMONIC_DETAIL_TEMPLATE`)重写到设计稿视觉水准。

## 1. 设计 token(浅色金融终端)

### 配色(CSS 变量,直接搬设计稿 root)
```
--bg:#eef3fa    --panel:#ffffff   --line:#e4eaf3   --line2:#eff3f9
--t1:#0f1c33    --t2:#5b6b85      --t3:#9aa7bd
--blue:#2563eb  --blue2:#1d4ed8   --bluebg:#eaf1ff
--long:#16a34a  --short:#e23744   --longbg:#e8f6ee  --shortbg:#fdecee
--amber:#e6a23c --violet:#a855f7
```
语义:long=绿(看涨/支撑)、short=红(看跌/压力)、blue=主色/PRZ、amber/violet=目标/扩展。

### 字体(无 CDN:系统 fallback,视觉接近)
```
正文: 'IBM Plex Sans',system-ui,-apple-system,sans-serif
等宽(数字): 'IBM Plex Mono',ui-monospace,monospace; font-variant-numeric:tabular-nums; letter-spacing:-.2px
```
> CLAUDE.md「无 CDN」:**不引 Google Fonts**,用系统 fallback(IBM Plex 若本机已装则生效,否则 system-ui)。
> 数字一律 `.mono` + tabular-nums(金融终端对齐关键)。

### 尺寸/间距(密集终端)
- 字号:KPI 18px / 标题 13-13.5px / 正文 11-12.5px / 标注 9-10.5px
- 字重:标题 700 / 次级 600 / 正文 400
- 圆角:卡片 12px / 中元素 8-9px / 小标签 3-5px
- 间距:gap 2/5/6/7/8/10/14px(按层级)
- 阴影:`box-shadow:0 1px 3px rgba(0,0,0,.1)`(轻)
- header 高 54px;三栏 grid `262px minmax(0,1fr) 372px`(窄屏堆叠)

### 通用动画
- LIVE 脉冲:`@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}`(实时点)
- 数据刷新闪烁:`@keyframes flashin{0%{background:#eaf1ff}100%{background:transparent}}`

## 2. 谐波页三栏结构(D3 蓝本)

```
┌ header: SMC logo · [HL系统|谐波系统] tab · 数据源脉冲 · clock ┐
├ KPI strip(6列): 监控币数/活跃形态/completed/forming/命中率/数据年龄 ┤
├─ 左 262px ──┬─ 中 主区(flex) ──────────┬─ 右 372px ──────────┤
│ coinsHarm   │ 大蜡烛图(核心焦点):     │ harmStats(形态详情)  │
│ harmList    │  candles + XABCD polyline│ sigFactors(指标gauge │
│ (币+形态+   │  + PRZ 区带 + fib levels │  + 共振信号)         │
│  置信+方向) │  + FVG/OB + BOS lines    │ fibRows(斐波位)      │
│             │  + OTE band              │ KNN prediction(诚实  │
│             │ indicator subchart       │  标注≈随机)          │
└─────────────┴──────────────────────────┴──────────────────────┘
```

## 3. 数据映射(→ 现有 API,数据层现成)
| 设计稿绑定 | smc 数据源 |
|---|---|
| `coinsHarm`/`harmList` | `build_harmonic_list`(每币汇总:best_conf/direction/has_completed) |
| `candles` | `build_coin_detail.candles`(200 根 OHLCV) |
| `xabcd` | `setups[].{x,a,b,c,d}_idx/_px`(已有 29 列) |
| `fibs`/`fibRows` | setups 的 fib_note + PRZ/entry/stop/target |
| `fvgs` | (新)需 zones.py FVG/OB 接入 detail —— C/后续 |
| `harmStats` | `setups[0]`(pattern/direction/entry/prz/rr/confidence) |
| `sigFactors`/`indRows` | 指标引擎 + forward_confirm(OI/funding/OFI) |
| `KNN` | `setups[].knn`(诚实标注≈随机) |
| S/R | `build_coin_detail.sr`(多周期布林带) |

## 4. 原生落地要点(无依赖)
- 复用现有 `render_harmonic_detail_html` 的自包含 HTML + `__INITIAL_STATE__` 注入 + 5s 轮询模式。
- SVG 蜡烛图保留现有 `renderSvgCandles`(已含 XABCD/PRZ/S-R),按新 token 调色 + 三栏布局重排。
- 配合 B1 实时:币头部 ● LIVE 脉冲 + 现价 + 数据时间;就地重绘保留滚动/展开态(现有已实现)。
- 谐波全币种(A2):左列表支持分页/过滤/搜索 + vol 排序(A3)。

## 5. 不做(YAGNI)
- 不引 React/CDN/Google Fonts(无依赖硬约束)。
- 量析终端(D2)合并暂缓,先做谐波页(用户优先)。
- 下单面板(order panel,设计稿有)—— smc 无 API key 不下单,跳过。
