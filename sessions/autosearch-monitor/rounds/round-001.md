# autosearch round-001 — 数据源真实可行性实证（keep/discard）

## Task Contract
- **目标**: 实证 4 大数据源(OKX/Binance/Coinglass/钱包)真实可行性 → 产出 keep/discard，
  为 streaming 监控系统确定可落地的数据源清单。metric: 每源给出 可用接口+字段+限流 或 不可用证据。
- **已知事实(带出处)**:
  - OKX 可用(www.okx.com REST ticker/candles) — `RESUME_OKX.md:8-10`
  - Binance api/fapi 451 封锁、data-api.binance.vision 滞后685天 — `RESUME_OKX.md:11`（**本轮复验：451 成立，但"滞后685天"已证伪**）
  - okx/client.py 曾写已提交但**已丢失**(竞态吞噬) — `ls src/smc_tracker/` 无 okx 目录
  - 现有 streaming: `app.py` TradingSystem(HL WS + Bitget WS) — `app.py:80-120`
- **执行边界**: 本轮**只读实证**(curl/最小 python)，不改任何生产代码、不写模块。
- **结果格式**: 每源 接口/状态/字段样本/限流/keep|discard/理由。

---

## 1) OKX 永续 — ✅ KEEP（streaming 一手数据源）
实证方式: 本机 `./.venv/bin/python` + curl，keyless 公开访问，全部 HTTP 200 / WS 连通。

### WS streaming（wss://ws.okx.com:8443/ws/v5/public）— 全可用
| channel | 字段样本 | 频率 |
|---|---|---|
| `trades` | tradeId, px, sz, **side(buy/sell)**, ts, count, seqId | 实时每笔 |
| `tickers` | last, ask/bid, open24h/high24h/low24h, vol24h, ts | 高频 |
| `open-interest` | oi, oiCcy, **oiUsd**, ts | ~3-4s |
| `mark-price` | markPx, ts | ~200ms |
| `funding-rate` | fundingRate, nextFundingTime, premium, ... | 8h 变一次 |
- 单帧批量订阅多 instId；全市场 **372 个 USDT 永续**（387 含币本位）。

### REST 补充
- `open-interest?instType=SWAP` / `mark-price?instType=SWAP` → **一次拉全市场**（冷启动/快照）。
- `funding-rate?instId=` → **不支持批量**(code=50014)，须逐币 + Semaphore 限流。
- `candles?bar=5m/15m/1H/...&limit=` → 倒序，**需 reverse**。

### 限流/稳定性
- WS 须 **25-30s 字面 `"ping"`** keepalive（服务端回字面 `"pong"`），否则断连。
- REST 公开行情 ~20 req/2s/IP，响应头不返计数，须客户端自控。无封锁。

### 接入结论（关键）
- **OKX WS 协议与现有 `BitgetWSClient`(`bitget/ws_client.py`) 几乎同构**：同样 `{"op":"subscribe","args":[...]}`、
  文本 ping/pong、25s 心跳、`{"arg":{channel,instId},"data":[...]}` 推送、看门狗重连 → **可直接照搬该模板**，
  仅改 URL + arg(无 instType) + ack 解析。接入成本低。
- 推荐: WS 优先(trades 带 side / OI / mark-price / tickers)走热路径；funding + 历史K + 全市场基线走 REST 周期。

---

## 2) Binance 永续 — ❌ DISCARD（本环境零可用永续路径，实证全封）
| 路径 | 实测 | 判定 |
|---|---|---|
| REST `fapi.binance.com/fapi/v1/*`(ticker/premiumIndex/openInterest) | **451** 地理封锁 | discard |
| REST `fapi.binance.com/futures/data/*`(OI 历史/多空比) | **451** | discard |
| REST `api.binance.com/api/v3/*`(现货主站) | **451** | discard |
| WS `wss://fstream.binance.com/ws/*`(aggTrade/markPrice@1s/!ticker@arr) | **握手 200 但永续流零帧**(伪可用，最隐蔽) | discard |
| WS `wss://stream.binance.com:9443/*`(现货主站) | **451 拒握手** | discard |
| `*.binance.vision/fapi/*` | **404**(vision 无 futures) | discard |
| `fapi.binance.vision` | **DNS 不解析** | discard |

### ⚠️ 现货可用（白名单，仅作参考价，**勿用于永续**）
- REST `data-api.binance.vision/api/v3/*` → **200 且完全实时**（价 64158 与 Bitget 64155 / HL 64117 三源吻合）。
- WS `wss://data-stream.binance.vision/ws/*` → 现货 trade/depth 实测收到数据。

### 纠正旧结论
- `RESUME_OKX.md:11` 的「data-api.binance.vision 滞后685天」**与本轮实测不符**：该域现货价格/kline 完全实时。
  → 旧记录过时/错误，应更新；但 vision **仅现货无永续**，对永续监控仍是 discard。
- **黑名单(写入避免重踩)**: fapi.binance.com/fapi/*、/futures/data/*、api.binance.com/api/v3/*、
  fstream.binance.com WS(伪连接零帧)、stream.binance.com:9443、*.binance.vision/fapi、fapi.binance.vision。

### 替代方案
- 永续数据继续用 **Hyperliquid + Bitget + OKX**（三所已覆盖主流永续）。Binance 永续 OI/funding/多空比若需要，
  考虑 Coinglass 聚合（见 §3，受 no-key 约束影响）。

---

## 3) Coinglass — ⚠️ 价值高但 keyless 裸 curl DISCARD（需选对抓取方式，待用户拍板）
实证(2026-06-22，亲自 curl)：

### 端点真实但 data 被签名头封锁
| 端点 | HTTP | 返回 | 结论 |
|---|---|---|---|
| `www.coinglass.com/` | 200 | Next.js HTML，AWS CloudFront | **无 Cloudflare 挑战** |
| `capi.coinglass.com/api/futures/liquidation/order?...` | 200 | `{"code":"0","success":true}` **无 data** | 端点在线、参数校验真实，**data 被抹除** |
| `capi.../api/futures/longShortChart?symbol=` | 200 | 无参报 `Required param 'symbol'` | 端点真实 |
| `open-api-v4.coinglass.com/api/...` | 200 | `{"code":"401","msg":"API key missing"}` | **官方 API 强制 CG-API-KEY** |
- **封锁机制(实测 OPTIONS)**：`access-control-expose-headers: user, encryption, language, time, v`。
  capi 端点须携带**浏览器页面加载时计算的签名头 `user`**(+encryption/time/v)才返真 data；裸 curl(含伪造头)只得空壳。
- 连发 6 次全 200，**无 429、无限流、无 Cloudflare**；但 keyless 拿不到 data。

### 独家高价值数据(对本系统前瞻 positioning 的增量)
1. **全所聚合清算 / 爆仓地图** `/api/futures/liquidation/*` — 最高价值，前瞻"清算磁吸位"(本项目单所数据覆盖不到)。
2. **多空账户比** `/api/futures/longShortChart` — 散户多空占比 vs 大户持仓比背离 = 庄反向 positioning 领先信号。
3. **聚合 OI(跨所)** — 比单所 OI 速度/加速度更干净、抗单所噪声。
4. 资金费聚合 + 套利偏离；5. ETF 流 / 期权(GEX/Max Pain/PCR) — 本项目完全没有。

### 三条可行路径(keyless 裸 curl = discard，替代按推荐度)
1. **浏览器自动化抓 XHR(keyless 合规，推荐)**：用 `claude-in-chrome`/Playwright 打开 `/liquidations` 等页，
   让页面自算签名头发请求，从 network 截 `capi.coinglass.com/api/*` 真实 JSON。**满足 no-key 硬约束**，
   代价：需常驻无头浏览器(比 aiohttp 重，偏离现有纯 asyncio 轻量架构)。
2. **官方免费 API key(最干净但破约束)**：`open-api-v4` 有 Hobbyist 免费档；工程最稳，**但违反"无 API key"硬约束**。
3. **逆向签名头(最脆，不推荐)**：复现 `user`/`encryption` 算法用 aiohttp；易随前端改版失效。

- **未竟项(诚实标注)**：未逆向出 `user` 签名算法 → 未实测拿到真实 data body；字段名(`liqLong/longVolUsd/totalVolUsd`)
  来自 React chunk，结构可信但**未经真实响应交叉验证**。
- **决策**：端点 keep(可达/无 CF/无限流)，但 **keyless 裸 curl discard**；采用与否 = 架构原则取舍，**须用户拍板**(见下方问题)。

---

## 4) 钱包地址监控"全方面" — 现状盘点（缺口评估待续）
现有覆盖: HL 链上地址级(`address_monitor` userFills/webData2)、地址画像/关联/档案、`wallet_portfolio`、
`whale_discovery/momentum`、onchain EVM 大额转账、Solana 供应量、交易所资金流(`exchange_flow` BTC/EVM 稳定币)。
→ 已相当完整；"全方面"增量待评估(多链余额聚合 / 地址标签实体识别 / CEX 充提细化)。

---

## 本轮 keep/discard 小结（最终）
- **KEEP**: OKX 永续(WS+REST，可直接复用 BitgetWSClient 模板) → **round-002 首先实现**。
- **DISCARD**: Binance 永续(本环境全封，伪 WS 零帧坑已入黑名单)；Binance 现货 vision 域仅作参考价(非永续)。
- **CONDITIONAL**: Coinglass(数据价值高，但 keyless 裸 curl 拿不到 data) → 须用户在「浏览器自动化 / 官方key / 暂缓」三选一。
- **现状充分**: 钱包"全方面"监控现有覆盖已相当完整(HL 地址级 + onchain EVM/Solana + 交易所资金流)，增量为可选增强。

## 旁路修正项（实证副产品）
- `RESUME_OKX.md:11`「data-api.binance.vision 滞后685天」**已证伪**(实测实时，三源吻合)→ 建议更新该记录。

## round-002 实现规划（基于 KEEP：OKX，主会话串行写，规避竞态吞噬）
仿 RESUME_OKX.md 设计 + 本轮实证，OKX 接入 story 序列：
1. `okx/__init__.py` + `okx/client.py`(OKXClient REST: ticker/candles/OI/funding/mark-price + instruments 全市场)
2. `okx/ws_client.py`(OKXWSClient：照搬 `bitget/ws_client.py`，订阅 trades/open-interest/mark-price/tickers，25s 文本 ping)
3. `monitor/okx_perp_monitor.py`(OKX 永续 trades 净流向 + OI/funding 异动，仿 `bitget_oi_monitor.py`)
4. `storage/db.py`：okx 永续数据表 + insert 方法
5. `app.py`：TradingSystem 接入 self.okx_ws + monitor，asyncio.gather 并发；`config.py` OKXCfg；`cli.py` okx 子命令
6. `tests/test_okx.py`(fake session 注入，不联网) + 全量 pytest 全绿(≥581) + 真实实证 + commit
- **纪律**：每个 story 主会话直接写 → 立即 `pytest` + `py_compile` → 通过即 commit 锁定(不派并发写 agent)。
