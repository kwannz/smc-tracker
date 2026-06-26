"""看板/TA/采集类周期任务 mixin（PeriodicTasksMixin）+ _apply_reconcile 模块级函数。

按域拆分自原 app_periodic：缓冲冲刷/清理保留/配置热加载/健康/HL汇总/行情板/
K线采集/布林带/谐波/波动板/PRZ 逼近。聪明钱/链上/研判类见 app_periodic_data.py。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from .bitget import BitgetREST
from .monitor.candle_ingest import detect_and_fill_gap as _detect_and_fill_gap
from .signals.harmonic_review import build_harmonic_predictions
from .util import fmt_hms as _hms
from .util import fmt_ts as _ts
from .util import fmt_px as _fmt_px

log = logging.getLogger("app")


def _apply_reconcile(monitor: Any, target: dict[str, str]) -> bool:
    """把 monitor.coin_to_symbol 对账到 target（增删都应用）；有变更返回 True。

    监控清单热载入复用入口（采集器/谐波/BB 共用）：
    monitor 需有 .coin_to_symbol(dict) 属性，可选 .top_n（同步为新币数）。
    """
    from .config import reconcile_universe  # noqa: PLC0415
    added, removed = reconcile_universe(monitor.coin_to_symbol, target)
    if not added and not removed:
        return False
    for c in removed:
        monitor.coin_to_symbol.pop(c, None)
    monitor.coin_to_symbol.update(added)
    if hasattr(monitor, "top_n"):
        monitor.top_n = len(monitor.coin_to_symbol)
    return True


class PeriodicTasksMixin:
    # ---- 周期任务（看板/TA/采集）----
    async def _periodic_flush(self, every: float = 5.0) -> None:
        while not self._stopping:
            await asyncio.sleep(every)
            self.meme_monitor.flush()
            self.orderbook_monitor.flush()   # 挂单墙事件批量落库（dashboard 消费）
            if self.oi_monitor:
                self.oi_monitor.flush()
            # A2a：sm_events 热路径缓冲批量落库（asyncio.to_thread 不阻塞 event loop）
            if self._sm_buffer:
                rows, self._sm_buffer = self._sm_buffer, []
                await asyncio.to_thread(self.store.insert_sm_events_batch, rows)
    async def _periodic_cleanup(self, every: float = 600.0) -> None:
        """周期清理无界增长的内存累积器 + DB 时间序列旧数据（防长跑内存/磁盘泄漏）。"""
        while not self._stopping:
            await asyncio.sleep(every)
            now = int(time.time() * 1000)
            # 跟庄累积器 _whale_acc：删除超 10× 窗口未更新的过期 (庄,coin) 键
            ttl = 10 * self._WHALE_WINDOW_MS
            for k in [k for k, v in self._whale_acc.items()
                      if now - max(v[1], v[2]) > ttl]:
                del self._whale_acc[k]
            # 已告警集群签名：超上限清空(允许久后重提醒，但有界)
            if len(self._seen_clusters) > 5000:
                self._seen_clusters.clear()
            # DB 时间序列保留：裁剪各表保留窗口外的旧行（保守窗口，不误删功能回看数据）
            total_pruned = 0
            for table, ts_col, keep_ms in self._DB_RETAIN:
                cutoff = now - keep_ms
                deleted = self.store.prune_before(table, ts_col, cutoff)
                total_pruned += deleted
            if total_pruned:
                log.info("数据质量：DB 清理旧数据 %d 行", total_pruned)
            # K 线滚动保留：每 (coin,tf) 仅留最新 _CANDLE_RETAIN_BARS 根（历史+实时统一上限），
            # 超额删最旧，防 bitget_candles 无界增长（用户#：每周期保持 3000 bar）
            try:
                pruned_c = self.store.prune_candles_to(self._CANDLE_RETAIN_BARS)
                if pruned_c:
                    log.info("数据质量：K线滚动保留删旧 %d 根（每币周期上限 %d）",
                             pruned_c, self._CANDLE_RETAIN_BARS)
            except Exception as exc:  # noqa: BLE001
                log.warning("K线滚动保留失败: %s", exc)
            # A1：每轮清理末尾触发 PRAGMA optimize（SQLite 分析查询计划优化，
            # 只在有足够变更时才执行，通常毫秒级；不放 __init__，空库无意义）
            try:
                self.store.conn.execute("PRAGMA optimize")
            except Exception:  # noqa: BLE001
                pass
    async def _periodic_config_reload(self, every: float = 30.0) -> None:
        """mtime 看门狗：每 30s 检查配置文件修改时间，有变化则热加载。"""
        while not self._stopping:
            await asyncio.sleep(every)
            if not self._cfg_path:
                continue
            try:
                p = Path(self._cfg_path)
                if not p.exists():
                    continue
                mtime = os.path.getmtime(self._cfg_path)
                if mtime > self._cfg_mtime:
                    self._cfg_mtime = mtime
                    self._reload_config()
            except Exception as exc:  # noqa: BLE001
                log.warning("配置 mtime 检查失败: %s", exc)
    async def _periodic_health(self, every: float = 600.0) -> None:
        """系统健康检查：周期核对数据新鲜度 + WS 状态 + 延迟；仅异常时推送（不空推）。

        使用 HealthMonitor（绑定 app，内部复用 system_health 作为 DB 真相源），
        聚合 WS/延迟/内存/数据新鲜度 → 中文摘要。纯本地查询，失败不影响监控热路径。
        """
        while not self._stopping:
            try:
                now = int(time.time() * 1000)
                snap = self.health.snapshot(now)
                overall = snap.get("overall", "ok")
                if overall != "ok":
                    summary = self.health.fmt(now)
                    self._push("🩺 " + summary)
                    print(f"\n{'='*60}\n{summary}\n{'='*60}\n")
                # ok 时静默不推送（避免噪声）
            except Exception as exc:  # noqa: BLE001 — 健康检查失败不影响监控
                log.warning("健康检查任务失败: %s", exc)
            await asyncio.sleep(every)
    async def _periodic_hl_digest(self, every: float = 0.0) -> None:
        """周期推送 **HL 抓庄分类汇总卡片**：把窗内零散 HL 事件按分类合并成一张卡片（降噪去刷屏）。

        digest 关闭则不启用（事件已即时推，见 _emit）。无事件时不空推；周期取自 cfg.digest.interval_sec。
        紧急信号（超级共振/可疑地址）已在 _emit 即时单独推过，此处汇总仍会再列出（便于完整复盘）。
        """
        if not self.cfg.digest.enabled:
            return
        period = every or self.cfg.digest.interval_sec
        while not self._stopping:
            try:
                card = self.hl_digest.render(int(time.time() * 1000))
                if card:
                    print(card)
                    self._push(card)
            except Exception as e:  # noqa: BLE001
                log.warning("HL digest 推送失败: %s", e)
            await asyncio.sleep(period)
    async def _periodic_ticker_board(self, every: float = 300.0) -> None:
        """周期推送行情监控板：价格/涨跌幅/资金费率/OI（每 5 分钟）。

        用户#要求：价/涨跌幅/费率/OI 行情维度不需要 → 默认关闭（cfg.output.push_ticker_board=False），
        本任务直接退出，聚焦 HL 抓庄；需要时配置开启即恢复（保持可达、可配置，不留死代码）。
        风格参考 BWE_OI_Price_monitor 频道：涨跌幅最大的排前，每次推 Top 20；无行情数据时不空推。
        """
        if not self.cfg.output.push_ticker_board:
            return
        while not self._stopping:
            await asyncio.sleep(every)
            if self.oi_monitor is None:
                continue
            rows = self.oi_monitor.board_rows()[:20]
            if not rows:
                continue
            now = int(time.time() * 1000)
            lines: list[str] = [f"📊 行情监控板 [{_ts(now)}] (数据源: Bitget 永续 · 价/涨跌幅/费率/OI)"]
            for r in rows:
                coin = r["coin"] or r["symbol"]
                price = r["price"]
                chg = r["chg24"]
                funding = r["funding"]
                oi_usd = r["oi_usd"]
                # 涨跌方向标识
                chg_sign = "🟢+" if chg >= 0 else "🔴"
                line = (
                    f"{coin:<10} ${_fmt_px(price)}"
                    f"  {chg_sign}{chg * 100:.2f}%"
                    f"  费率{funding * 100:+.4f}%"
                    f"  OI${oi_usd:,.0f}"
                )
                lines.append(line)
            board_text = "\n".join(lines)
            print(board_text)
            self._push(board_text)
    async def _periodic_candle_collect(self, every: float = 300.0) -> None:
        """周期采集 Bitget 永续 K 线落 DB（BB/谐波多周期计算共用，减重复拉 + 跨重启持久）。

        启动后先延迟 20s（避开 HL seed 带宽），采一次填 DB（让首个板周期可直接读 DB），
        之后每 every 秒刷新（work-first 已在延迟后立即执行）。

        增量轮转（661 币分多轮）：每轮采 batch_size 个币，_collect_offset 环绕滚动，
        避免单轮爆量请求。采集失败只 log.warning，不阻塞其他任务。

        冷启动加速（CLAUDE.md §三-1 第一性原理）：
          DB 覆盖度 < 80% 时（冷启动期）：batch_size=120（稳态 60），且批次间不等待（every=0），
          快速铺满全集 665 币 K 线（冷启动约 5~6 轮 × 120 = 720 币次即可全覆盖，远快于稳态 11 轮）。
          覆盖度 >= 80% 时（稳态）：batch_size=60，每轮等待 every=300s，恢复正常节奏。
          覆盖度检测用首个 tf 做代理（多 tf 覆盖通常同步），若无 tf 配置降级为稳态。
          Bitget _SEMA=8 且谐波侧已验证多轮无 429，冷启动 batch_size=120 并发量在安全范围内。
        """
        if self.candle_collector is None:
            return
        # work-first：延迟 20s 后立即首轮采集（让 BB/谐波首个板周期可直接读 DB）
        await asyncio.sleep(20.0)

        # 冷启动参数
        _COLD_BATCH = 120      # 冷启动：每轮采集币数（更快铺满）
        _WARM_BATCH = 60       # 稳态：每轮采集币数（维持现有节奏）
        _COLD_THRESHOLD = 0.8  # 覆盖度低于此阈值 → 冷启动模式
        # 用首个 tf 做冷启动代理（多 tf 同步，取最有代表性的一个）
        _probe_tf: str | None = (
            self.candle_collector.timeframes[0]
            if self.candle_collector.timeframes else None
        )
        _total_coins = len(self.candle_collector.coin_to_symbol)

        while not self._stopping:
            # ---- 监控清单热载入：每轮对账采集器币集（增删都反映，无需重启）----
            _mc_enabled = self.cfg.monitored_coins.enabled
            _sleep_s = (self.cfg.monitored_coins.collect_interval_sec
                        if _mc_enabled else every)
            if _mc_enabled:
                try:
                    target = self.store.get_monitored_coins()
                    if not target:
                        log.warning("监控清单为空，本轮跳过采集（用 `watch add` 或 dashboard 添加）")
                        await asyncio.sleep(_sleep_s)
                        continue
                    if _apply_reconcile(self.candle_collector, target):
                        log.info("采集器币集已对账监控清单：%d 币", len(target))
                    _total_coins = len(self.candle_collector.coin_to_symbol)
                except Exception as exc:  # noqa: BLE001
                    log.warning("采集器清单对账失败: %s", exc)

            # ---- 冷启动 vs 稳态判断 ----
            is_cold = False
            if _probe_tf and _total_coins > 0:
                covered = self.candle_collector.covered_coin_count(_probe_tf)
                coverage = covered / _total_coins
                is_cold = coverage < _COLD_THRESHOLD
                if is_cold:
                    log.info(
                        "K线采集冷启动模式：已覆盖 %d/%d 币（%.1f%%），优先采集未覆盖币，连续回填",
                        covered, _total_coins, coverage * 100,
                    )

            try:
                if is_cold and _probe_tf:
                    # 冷启动：优先采集 DB 中缺数据的币（每批最多 _COLD_BATCH 个）
                    # 保证每批必然新增覆盖，避免盲目轮转浪费在已覆盖区。
                    # uncovered_symbols 基于 probe_tf 查询；个别币超时会被 _fetch_one
                    # 的 retry_bars 重试逻辑跳过，下轮再试，诚实不假装成功。
                    uncovered = self.candle_collector.uncovered_symbols(_probe_tf)
                    # 每批取前 _COLD_BATCH 个未覆盖币；若全部已覆盖则 probe_tf 已达标，
                    # 但其它 tf 可能还有缺口——此时 is_cold=False 不会进入此分支。
                    batch_subset = uncovered[:_COLD_BATCH]
                    n_written = await self.candle_collector.collect_symbols(batch_subset)
                    log.info(
                        "K线冷启动采集：本批 %d 个未覆盖币，写入 %d 根（probe_tf=%s）",
                        len(batch_subset), n_written, _probe_tf,
                    )
                else:
                    # 稳态：原有 offset 轮转逻辑，完全向后兼容
                    self._collect_offset = await self.candle_collector.collect_batch(
                        self._collect_offset, _WARM_BATCH)
                    log.info(
                        "K线批量采集落 DB（offset→%d，batch=%d，稳态）",
                        self._collect_offset, _WARM_BATCH,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("K线采集失败: %s", exc)

            # 冷启动期不等待：立即开始下一批，尽快铺满 DB；稳态等 _sleep_s 秒
            # （监控清单模式用 collect_interval_sec，否则用 every）
            if not is_cold:
                await asyncio.sleep(_sleep_s)
    async def _periodic_bb_board(self) -> None:
        """周期推送 Bitget 永续多周期布林带压力/支撑卡片（默认 15 分钟）。

        未配置 cfg.bollinger.enabled=False 或 bb_monitor 未初始化时直接返回，
        不阻塞其他周期任务。
        """
        if not self.cfg.bollinger.enabled or self.bb_monitor is None:
            return
        # 启动后短延迟即首轮（等采集器填 DB ~40s），避免重启后长时间空窗；之后每 interval 一轮
        await asyncio.sleep(90.0)
        while not self._stopping:
            # 监控清单热载入：enabled 时每轮对账 BB 监控币集（增删都反映）
            if self.cfg.monitored_coins.enabled and self.bb_monitor is not None:
                try:
                    _apply_reconcile(self.bb_monitor, self.store.get_monitored_coins())
                except Exception:  # noqa: BLE001
                    pass
            try:
                now = int(time.time() * 1000)
                rows = await self.bb_monitor.refresh(now)
                card = self.bb_monitor.render(rows, now)
                if card:
                    print(card)
                    self._push_harmonic(card)   # 独立 TA 通道（与 HL 分开）
                # BB 压力层落库（供 dashboard 多周期 S/R 叠加）；落库失败只 warn，不阻塞推送
                try:
                    bb_recs = self.bb_monitor.to_bb_records(rows, now)
                    if bb_recs:
                        self.store.insert_bb_levels(bb_recs)
                except Exception as db_exc:  # noqa: BLE001
                    log.warning("BB levels 落库失败: %s", db_exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("布林带周期推送失败: %s", exc)
            await asyncio.sleep(self.cfg.bollinger.interval_sec)
    async def _periodic_harmonic_board(self) -> None:
        """周期推送 Bitget 永续多周期谐波形态前瞻卡片（默认 15 分钟）。

        未配置 cfg.harmonic.enabled=False 或 harmonic_monitor 未初始化时直接返回，
        不阻塞其他周期任务。
        """
        if not self.cfg.harmonic.enabled or self.harmonic_monitor is None:
            return
        # 启动后短延迟即首轮（错峰 BB +60s，等采集器填 DB），首轮即落库出数据，避免 /harmonic 长空窗
        await asyncio.sleep(150.0)
        while not self._stopping:
            try:
                now = int(time.time() * 1000)
                # 监控清单热载入：enabled 时全量对账(增删都反映)；否则保留旧 harmonic_collected 加性并入
                try:
                    if self.cfg.monitored_coins.enabled:
                        _apply_reconcile(self.harmonic_monitor, self.store.get_monitored_coins())
                    else:
                        # 默认模式：加性并入 harmonic_collected + monitored_coins(discover 现写后者)
                        coll = dict(self.store.get_harmonic_collected())
                        coll.update(self.store.get_monitored_coins())
                        if coll and any(c not in self.harmonic_monitor.coin_to_symbol for c in coll):
                            self.harmonic_monitor.coin_to_symbol.update(coll)
                            self.harmonic_monitor.top_n = len(self.harmonic_monitor.coin_to_symbol)
                except Exception:  # noqa: BLE001
                    pass
                # A2：每轮 refresh 前检测并回填 K 线缺口（冷启动/周期补缺）
                # detect_and_fill_gap 检测 DB 最新 bar 到当前已收盘 bar 的缺口，
                # 缺口 >= 1 → backfill；无缺口 → 静默返回 0（不重复拉）。
                # 仅在 store 模式下有效（live 模式 store=None 时跳过）。
                _hmon = self.harmonic_monitor
                if _hmon.store is not None:
                    try:
                        async with BitgetREST() as _bg_gap:
                            _gap_sema = asyncio.Semaphore(4)  # 限流，避免批量补缺撞限速

                            async def _fill_one(coin: str, symbol: str, tf: str) -> None:
                                async with _gap_sema:
                                    try:
                                        await _detect_and_fill_gap(
                                            _bg_gap, coin, symbol, tf, _hmon.store
                                        )
                                    except Exception as _gf_exc:  # noqa: BLE001
                                        log.debug(
                                            "谐波缺口检测失败 %s/%s: %s", coin, tf, _gf_exc
                                        )

                            _gap_tasks = [
                                _fill_one(coin, sym, tf)
                                for coin, sym in _hmon.coin_to_symbol.items()
                                for tf in _hmon.timeframes
                            ]
                            if _gap_tasks:
                                await asyncio.gather(*_gap_tasks)
                    except Exception as _gap_exc:  # noqa: BLE001
                        log.warning("谐波 detect_and_fill_gap 批量失败: %s", _gap_exc)

                rows = await self.harmonic_monitor.refresh(now)
                card = self.harmonic_monitor.render(rows, now)
                if card:
                    print(card)
                    self._push_harmonic(card)   # 谐波系统专用独立飞书（与 HL 分开）
                # 谐波 setups 落库（供 dashboard 独立页读取）；落库失败只 warn，不阻塞推送
                try:
                    self.store.insert_harmonic_setups(
                        self.harmonic_monitor.to_records(rows, now)
                    )
                except Exception as db_exc:  # noqa: BLE001
                    log.warning("谐波 setups 落库失败: %s", db_exc)
                # R1 review 闭环：completed setup 进 predictions 表（kind=谐波-反应式），
                # 到期核对真价 → 诚实前瞻命中率（accuracy_report by_kind）。
                # 只记 completed（forming 推迟到逼近 PRZ）；结构指纹去重；bg_px 用 Bitget 价修覆盖。
                # 失败不阻塞推送热路径。
                try:
                    for rec in build_harmonic_predictions(rows, self._harmonic_dedup, now):
                        self._record_pred(
                            rec["coin"], rec["kind"], rec["direction"],
                            bg_px_override=rec["bg_px"],
                        )
                except Exception as rev_exc:  # noqa: BLE001
                    log.warning("谐波 review 闭环记录失败: %s", rev_exc)
                # 更新 forming PRZ 缓存（供 _periodic_prz_approach 用实时价检查逼近）
                if getattr(self, "harmonic_approach", None) is not None:
                    try:
                        self.harmonic_approach.update(rows, now)
                    except Exception as ap_exc:  # noqa: BLE001
                        log.warning("forming PRZ 缓存更新失败: %s", ap_exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("谐波形态周期推送失败: %s", exc)
            await asyncio.sleep(self.cfg.harmonic.interval_sec)
    async def _periodic_volatility_board(self) -> None:
        """周期推送实时波动追踪板（监控清单币逐周期 速度/加速度/σ/ATR/PD 溢价折价）。

        opt-in：仅 monitored_coins.enabled 且 vol_board_sec>0 时启用（默认关，零新增噪声）。
        每轮重读 get_monitored_coins() → 自动热载入；读 DB 已采 K 线（无网络），失败只 warn。
        """
        # 启动守卫只读一次（opt-in 开关运行期不变）；循环内重读 self.cfg 以支持配置热载入（修 P2-6）
        if not self.cfg.monitored_coins.enabled or self.cfg.monitored_coins.vol_board_sec <= 0:
            return
        from .monitor.volatility_monitor import VolatilityMonitor  # noqa: PLC0415
        from .monitor.volatility_regime_tracker import VolatilityRegimeTracker  # noqa: PLC0415
        tracker = VolatilityRegimeTracker()
        await asyncio.sleep(120.0)   # 等采集器填 DB
        while not self._stopping:
            mc = self.cfg.monitored_coins   # 每轮重读：_reload_config 替换 self.cfg 后即时生效
            try:
                coins = self.store.get_monitored_coins()
                if coins:
                    mon = VolatilityMonitor(coins, list(mc.timeframes), self.store)
                    now = int(time.time() * 1000)
                    rows = mon.rank(now)
                    card = mon.render(rows, now)
                    if card:
                        print(card)
                        self._push_harmonic(card)   # 独立 TA 通道
                    events = tracker.update(rows, now)          # 波动扩张确认检测
                    bo = tracker.render(events, now)
                    if bo:
                        print(bo); self._push_harmonic(bo)      # 扩张确认推送
            except Exception as exc:  # noqa: BLE001
                log.warning("波动追踪板推送失败: %s", exc)
            await asyncio.sleep(mc.vol_board_sec)
    async def _periodic_prz_approach(self, every: float = 15.0) -> None:
        """forming PRZ 实时逼近：两轮谐波重算之间，用 Bitget trade 流的实时价检查现价是否
        进入已投影 forming PRZ → 秒级 🎯逼近告警 + 记 review（"谐波-逼近"，QA H1：forming 在
        触达 PRZ 才记，非投影时记）。

        QA H6 安全：检查在独立周期任务（非 WS 热回调）做，且 check 是纯内存判定；落库走
        _record_pred（周期任务里，非热路径）。harmonic_approach/harmonic_trade 未建则直接返回。
        """
        if getattr(self, "harmonic_approach", None) is None or \
                getattr(self, "harmonic_trade", None) is None:
            return
        await asyncio.sleep(160.0)   # 错峰：等谐波首轮填好 forming PRZ 缓存
        while not self._stopping:
            await asyncio.sleep(every)
            try:
                now = int(time.time() * 1000)
                for coin in list(self.harmonic_monitor.coin_to_symbol):
                    px = self.harmonic_trade.last_price(coin)
                    if not px or px <= 0:
                        continue
                    for ev in self.harmonic_approach.check(coin, px, now):
                        arrow = "🟢看多" if ev["direction"] == "long" else "🔴看空"
                        msg = (f"🎯 [谐波逼近] {coin} {ev['tf']} {ev['pattern']} {arrow} "
                               f"现价 {_fmt_px(px)} 进入 PRZ [{_fmt_px(ev['prz_lo'])}~{_fmt_px(ev['prz_hi'])}]"
                               f" · 前瞻反转预警（非入场信号，待确认）"
                               + self.efficacy.label_of("谐波-逼近"))
                        print(f"[{_hms()}] {msg}")
                        self._push_harmonic(msg)
                        # forming 反转预测在触达 PRZ 时记（诚实前瞻命中率）
                        self._record_pred(coin, "谐波-逼近", ev["direction"], bg_px_override=px)
            except Exception as exc:  # noqa: BLE001
                log.warning("forming PRZ 逼近检查失败: %s", exc)
