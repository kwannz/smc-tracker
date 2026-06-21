# round-hl-001 — HL 挂单墙动态监控(l2Book 领先信号)

## Task Contract
- 目标: HL l2Book WS 实时挂单墙(大额未成交挂单)检测 + 出现/抽单动态 → 前瞻意图告警. CLAUDE.md #1 领先信号.
- 已知事实(实证): l2Book WS 可用(ACK+实时推送), data={coin,time,levels:[bids,asks]}, 档={px,sz,n}, 20档
  - HL Subscription(type,coin) 支持 l2Book(ws_client.py:31-43), 现仅 REST 快照用(app.py:659)
  - orderbook_imbalance 已在 flow_predictor.py:18(静态失衡), 缺动态墙追踪
  - PLAN.md:297 明确下一步「多档订单簿动态(挂单墙增减)」
- 执行边界: 新建 monitor/orderbook_monitor.py(HLOrderbookMonitor+detect_walls纯函数) + db(hl_orderbook_walls表) + 测试 + scripts 入口(可达). 不改 app.py(避免冲突)
- 诚实定位: 挂单墙=意图告警(可能 spoof), 非确定方向. bid墙=支撑/吸筹意图, ask墙=压制
- 决策门槛: keep=detect_walls+动态检测+落库+测试+可达+全量绿(≥629+新测试)

## 结果: KEEP
- HLOrderbookMonitor(l2Book WS)+detect_walls纯函数+build/pull动态+db表+脚本入口+9测试
- workflow实现+对抗验证(独立复算detect_walls非tautology)+主会话核验: 638 passed, 非孤儿, app.py未改
- 实跑端到端: 9帧l2Book→21墙事件(build13/pull8), 真实大墙ETH$2.9M/BTC$1.38M, 失衡BTC+0.201

## HL 后续候选(待定)
- 墙信号接入信号层(persist+push告警, 诚实不喂confluence因方向语义=意图)
- 墙监控接入 app TradingSystem(现仅脚本可达)
- 其他HL领先信号: activeAssetCtx(mark/oracle/funding/OI), userEvents
