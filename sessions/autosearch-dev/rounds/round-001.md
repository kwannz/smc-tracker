# autosearch-dev round-001 — OKX 接入跨所信号层

## Task Contract
- **目标**: OKX 成为跨所信号一等公民 —— `okx_signals` 表 + `run_okx_streaming` 落库 OKX 背离信号
  + 接入 `confluence._SOURCES`，让 OKX/HL/Bitget 跨所同向可共振(超级信号)。
- **已知事实(出处)**:
  - `confluence._SOURCES` 模式 `(table, name, to_dir)`，`SELECT coin,direction FROM {table} WHERE ts>=?` — `signals/confluence.py:41,76`
  - OKX `detect_divergences`/`funding_flow_divergence` 已在 `okx/stream.py`(本会话 commit `46704cb`)
  - `run_okx_streaming(store, okx_cfg)` 常驻落库任务 — `okx/stream.py:108`
  - OKX 未接入 `signals/`(grep okx signals/ 零命中)；M6 信号引擎 🔄 `PLAN.md:118`
  - 领先信号(flow_velocity/accel/orderbook_imbalance)已在 `flow_predictor.py`(非缺口，已验证)
- **要验证假设**: OKX 背离落 `okx_signals(coin,direction,ts)` + 加入 `_SOURCES` → OKX 参与共振，全量绿。
- **执行边界**: 改 `storage/db.py`(okx_signals 表+insert) + `okx/stream.py`(run_okx_streaming 落库背离)
  + `signals/confluence.py`(_SOURCES 加 OKX) + 测试。**不改其他 venue 逻辑**。
- **决策门槛**: keep = okx_signals 表落库 + confluence 能聚合 OKX 源 + 全量绿(≥612+新测试)；否则 discard 回滚。
- **验证命令**: `pytest -q` 全绿；db roundtrip + confluence 含 okx_signals 源单测。

## 执行: builder-agent(Sonnet)，Opus 审核 + git show --stat 核验真实落盘
## 结果: (待回填)

## 结果: KEEP
- okx_signals 表 + insert/recent + confluence._SOURCES 加 OKX 源 + run_okx_streaming 落库背离
- 独立核验: 全量 618 passed, confluence 聚合 OKX 测试 6 passed, _SOURCES:49 真加, 落库去重逻辑正确
- commit 已核验四文件落盘(db+35/stream+23/confluence+2/test+119)

## round-002 实证(可行, 待开发): OKX 强平监控
- liquidation-orders 频道 keyless 可用(本会话 probe: ACK ok + 2 真实强平事件)
- 数据 details[{posSide,side,sz张,bkPx强平价,ts}]; 名义=sz×ctVal×bkPx; 多头被平=抛压级联
- 设计要点: OKXSub 需支持 instType=SWAP 订阅(非per-instId), _on_liquidation 按监控集过滤聚合
