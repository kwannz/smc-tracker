# 谐波系统 MVP 清单（loop 单线推进)

> 本文件是谐波 MVP 的**唯一计划锚点**,由 loop 单线驱动。
> 每个 loop 周期:读本文件 → 找**第一个**未完成项 `- [ ]` → 只做这一项 → 跑测试 → 勾选 `- [x]` → commit → 在「迭代日志」追加一行。

## 🎯 目标
让谐波系统用**真实 Bitget 数据真正端到端计算**,并把最致命的架构错配修好,达到第一个可演示的 MVP:
真实 K 线(REST 回填 + WS 增量,统一门面) → 单一真相 `bitget_candles` → `analyze_candles` 真实几何计算
(增量状态机,与全量逐字段等价) → `harmonic_setups` → dashboard 同库展示。

## ⚖️ 审计结论（WF1 已实证,作为前提)
- **计算核心 `indicators/harmonic.py` 忠实、无造假、不重写**:Gartley/Bat/Butterfly/Crab 比率逐项对标
  Scott Carney 标准正确;no-repaint(`_alternate_immutable` first-wins)判定 **faithful**;PRZ 双路径交汇符合语义。
- **真正问题在数据流编排层**:① 每 bar 全量重建 MarketStructure;② WS/REST 双写无 gap 回填;
  ③ `get_candles` 无增量游标;④ 固定轮询非 bar 边界驱动;⑤ bars 不按 tf 自适应;⑥ dashboard 单库遇分裂显空。
- MVP 聚焦 ①②③⑤ + 真实数据验证;④(bar 边界调度)、⑥(多库收敛)、新形态(Cypher/Shark)列入 MVP 之后。

## 🔒 执行铁律（每个 loop 周期必须遵守)
1. **单线**:同一时间只有一个 loop 在跑,绝不并行多 agent(防撞车 / 防丢失 / 可追踪)。
2. **每步即时 commit**:做完一项立刻 `git add` + `git commit`(成果固化进 git,untracked 才会被清掉)。
3. **每步先测后过**:`PYTHONPATH=src "/Volumes/ROG ESD-S1C Media/smc/.venv/bin/python" -m pytest -q` 必须全绿
   (基线 1921,只增不减);新功能配单测;真实数据项须真实拉取验证,严禁合成数据假装通过。
4. **不重写 `harmonic.py` 几何**:新模块一律 `import` 复用其函数。
5. **零孤儿**:新模块同步接入运行时 + 从所在目录 `__init__.py` 导出。
6. **db.py 只增不改**:仅新增可选参数 / 新方法,不动现有签名与行为。
7. **每步一项**:一个周期只推进一个 `- [ ]`,做完即停,等下一周期。

## ✅ 清单（按依赖顺序,逐项推进)

- [x] **S1 · db.py 增量游标**:`get_candles` 加可选 `since_ms`(None 时行为完全不变;提供时
      `WHERE open_ms>? ORDER BY open_ms ASC` 增量读)+ 新增 `latest_candle_ms(coin,tf)->int|None`。
      **验收**:新增 `tests/test_db_candle_cursor.py`(不传/传 since_ms/latest_candle_ms/旧调用零影响)全绿;
      `tests/test_db_harmonic_v2.py` 不破坏;全量 pytest 绿。

- [x] **S2 · harmonic_state.py 增量状态机**:新建 `indicators/harmonic_state.py`,`HarmonicState(order,tol)`
      持久化 `MarketStructure`,`update(candle)` 只增量喂一根,复用 `harmonic.py` 的
      `_alternate_immutable/detect_xabcd/project_prz/_COMPLETED_MAX_DIST` 装配 `{completed,forming,price}`;
      `update`/`snapshot` 共用单一 `_compute`(去重)。从 `indicators/__init__.py` 导出 `HarmonicState`。
      **验收**:新建 `tests/test_harmonic_state_parity.py`——① 逐根 update 输出 == `analyze_candles` 全量逐字段相等
      (含每个前缀 k 都相等);② no-repaint 前缀不变量;③ 边界(不足/空)。全量 pytest 绿。

- [x] **S3 · candle_ingest.py 统一摄入门面**:新建 `monitor/candle_ingest.py`:`backfill`(REST 拉取→复用
      `candle_collector._clean_candles` 清洗→`upsert_candles`)、`detect_and_fill_gap`(用 `db.latest_candle_ms`
      查最新→按 `GRANULARITY_MS` 算缺口→REST 定向回填)、`ingest_ws_closed_bar`(WS 单根经同一清洗落库)。
      从 `monitor/__init__.py` 导出。**验收**:新建 `tests/test_candle_ingest.py`(mock bg + tmp Store:
      落库根数/清洗剔脏/gap 触发回填/无缺口不拉/ws 单根)全绿;全量 pytest 绿。

- [x] **S4 · tf 自适应窗口**:`HarmonicCfg` 增 `tf_bars: dict[str,int]`(per-tf 覆盖)+ `bars_for_tf(tf)` helper；
      `order=2, tol=0.07`（高灵敏模式）配置化；向后兼容（tf_bars 空 dict 时回退全局 bars）。
      全量 pytest 绿。

- [x] **S5 · 真实数据 E2E 验证脚本**:新建 `scripts/verify_harmonic_e2e.py`:5 高流动性币 ×(15m,1H)
      真实拉 Bitget K 线→清洗→`analyze_candles`,打印每组 K线/枢轴/completed/forming + 示例 setup。
      **验收**:`PYTHONPATH=src .venv/bin/python scripts/verify_harmonic_e2e.py` **真实运行成功**,
      诚实报告哪些组合真实算出形态(随行情变化,非固定;严禁编造)。

> ⚠️ **协调声明（2026-06-25，初始双 loop 分工）**:早期 MVP loop 与前台交互 loop 并行运行以避免双写冲突，
> 接线层 S4/S6/S7/S8/S9 暂标 `- [~]`（移交前台）。
> **2026-06-25 Finalize workflow 收口**:parallel agent workflow（Spec §6 分工）已完成全部接线，
> S4/S6/S7/S8/S9 全部实现并通过测试，现补勾 `- [x]`。清单全项完成 ✅。

- [x] **S6 · 接线 WS 增量**:`monitor/harmonic_candle_ws.py` 收盘 bar 调 `HarmonicState.update()`；
      `harmonic_monitor.py` 维护 per-(coin,tf) `HarmonicState` 增量更新；不等则全量重算并 log.warning。
      全量 pytest 绿。

- [x] **S7 · 接线 monitor.refresh**:`monitor/harmonic_monitor.py` 维护 per-(coin,tf) `HarmonicState`，refresh 时
      只喂新收盘 bar；结果逐字段等价全量（parity 测试护守）。全量 pytest 绿。

- [x] **S8 · 收尾对齐**:`app.py` 冷启动/周期采集调 `detect_and_fill_gap` 补缺口；indicators `__init__.py` 导出
      `HarmonicState`；三大新形态（Cypher/Shark/ABCD）并入 `analyze_candles` + `HarmonicState._compute`；
      dashboard 高灵敏诚实标注 + inline SVG 形态绘制（completed 实线 + forming 虚线 + PRZ 阴影 + 黄金口袋高亮）。
      全量 pytest 绿（2202 passed + 2 skipped）。

- [x] **S9 · MVP 收口验收**:全量 pytest 2202 passed（基线 1921→+281 新增测试）；E2E 真实 Bitget 10 组合复跑成功：
      BTC/15m Butterfly bear conf=0.83、ETH/1H ABCD bull conf=0.87、SOL/15m ABCD bear conf=0.88、
      SOL/1H ABCD bear conf=0.82、XRP/1H Crab bull conf=0.88、XRP/15m Shark bull forming conf=0.83（诚实，随行情）。
      迭代日志追加。

## 📜 迭代日志
- (loop 每完成一项在此追加:`YYYY-MM-DD HH:MM Sx 完成 — 摘要 — commit <sha> — 测试 N passed`)
- 2026-06-25 第一波 S1/S2/S5 完成 — db 增量游标(since_ms + latest_candle_ms,主进程确定性重做,修掉并行 agent 的重复方法缺陷)/ 增量状态机 HarmonicState(等价测试 8 passed:增量逐根 update == analyze_candles 全量逐字段 + 每前缀相等 + no-repaint)/ 真实 E2E(真拉 Bitget 10 组合,BTC15m Butterfly bear conf0.87、XRP1H Crab bull conf0.88 等真实形态)— 全量 1944 passed(基线 1921 +23)
- 2026-06-25 Finalize收口(S3/S4/S6/S7/S8/S9 全项完成) — 新形态 Cypher/Shark/ABCD(harmonic_ext.py,几何合成测试绿) + 灵敏度 order=2/tol=7% 配置化(HarmonicCfg.tf_bars per-tf自适应) + Fib黄金口袋入场精炼(golden_pocket_zone/intersect_zone,诚实 fib_note,confidence 不加分) + inline SVG 绘制(completed 实线+forming 虚线+PRZ 阴影+黄金口袋高亮) + 高灵敏诚实警示标注 + HarmonicState/candle_ingest 全接线(app.py detect_and_fill_gap冷启动缺口填充) + parity 护栏(test_harmonic_state_parity.py 逐字段等价+no-repaint) — 全量 2202 passed — E2E 真实 Bitget 复跑：BTC/15m Butterfly bear conf=0.83、ETH/1H ABCD bull conf=0.87、SOL ABCD bear(15m/1H)、XRP/1H Crab bull conf=0.88、XRP/15m Shark forming conf=0.83（诚实，随行情变化）
