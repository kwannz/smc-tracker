"""聪明钱/链上/研判类周期任务 mixin（PeriodicDataMixin）。

按域拆分自 app_periodic：链上轮询/前瞻流预测/地址协同/日报/LLM 研判/共识/背离/
回顾校准/有效性/Solana 供应/钱包画像/交易所资金流/庄 PnL/排行榜发现。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .hyperliquid import HyperliquidInfo
from .memecoins import normalize
from .monitor.whale_discovery import discover_smart_money, fetch_leaderboard_rows
from .monitor.whale_momentum import pnl_rows_from
from .notify import build_report
from .onchain import fmt_flow_alert
from .review import fmt_accuracy
from .signals import positioning, orderbook_imbalance, oi_directional_velocity
from .util import fmt_hms as _hms
from .util import fmt_ts as _ts
from .util import to_float as _f
from .util import is_placeholder_addr

log = logging.getLogger("app")


class PeriodicDataMixin:
    async def _periodic_onchain(self, every: float = 30.0) -> None:
        while not self._stopping:
            await asyncio.sleep(every)
            # 用 Bitget OI 的标记价喂给链上 USD 过滤器，使 min_amount_usd 真正生效
            if self.oi_monitor:
                prices: dict[str, float] = {}
                for symbol, coin in self.oi_monitor.symbol_to_coin.items():
                    row = self.store.latest_oi(symbol)
                    if row and row[4]:            # row[4]=mark_px
                        prices[coin] = row[4]
                self.onchain.prices = prices
            try:
                got = await self.onchain.poll_once(lookback=4)
                now = int(time.time() * 1000)
                for t in got:
                    print(f"[{_hms()}] ⛓️  [链上] {t.coin}@{t.chain} {t.amount:,.0f} "
                          f"{t.from_addr[:10]}…→{t.to_addr[:10]}…")
                    # 喂信号引擎（按 HL 币名归键）：链上大额转账=信心加成
                    hl = self.canon_to_hl.get(t.coin.upper())
                    if hl:
                        usd = self.onchain._amount_usd(t) or self.onchain.min_amount_usd
                        self.signal_engine.set_onchain(hl, usd, now)
            except Exception as e:  # noqa: BLE001
                log.warning("链上轮询失败: %s", e)
    async def _periodic_flow_predict(self, every: float = 30.0) -> None:
        """前瞻资金流预测：采样净流向加速度 + 订单簿挂单意图 + OI 速度 → 预测方向。

        C.2: flow_acceleration 返回 float|None，用 abs(... or 0.0) 兼容 None。
        C.3: OI 速度用 oi_directional_velocity（方向化），替换裸比率。
        C.1: book_intent 优先（WS 逐帧 OFI+queue+micro），降级到 REST orderbook_imbalance。
        """
        while not self._stopping:
            await asyncio.sleep(every)
            now = int(time.time() * 1000)
            # 1) 采样所有 meme 的净流向增量
            for coin in self.meme_markets:
                cur = self.meme_monitor.coin_net(coin)
                delta = cur - self._last_coin_net.get(coin, cur)
                self._last_coin_net[coin] = cur
                self.flow_predictor.push(coin, delta, now)
            # 2) 取资金流加速度最强的几个币，拉订单簿 + OI 速度 → 预测
            # C.2: flow_acceleration 可返回 None（样本不足），用 or 0.0 兼容
            ranked = sorted(self.meme_markets,
                            key=lambda c: abs(
                                self.flow_predictor.flow_acceleration(c, now) or 0.0
                            ),
                            reverse=True)[:3]
            try:
                async with HyperliquidInfo(self.cfg.hyperliquid.rest_url) as info:
                    for coin in ranked:
                        # C.1: WS book_intent 优先，降级到 REST orderbook_imbalance
                        book_imb: float = (
                            self.orderbook_monitor.book_intent(coin, now)
                            if self.orderbook_monitor is not None else None
                        ) or 0.0
                        if book_imb == 0.0:
                            # REST 降级
                            try:
                                l2 = await info._post({"type": "l2Book", "coin": coin})
                                lv = l2.get("levels") or [[], []]
                                book_imb = orderbook_imbalance(lv[0], lv[1])["imbalance"]
                            except Exception:  # noqa: BLE001
                                pass
                        # C.3: 方向化 OI 速度（替换裸比率 (chg[0]-chg[1])/chg[1]）
                        oi_vel = 0.0
                        sym = self.coin_to_symbol.get(normalize(coin))
                        if sym:
                            chg = self.store.oi_change(sym, 600_000, now)
                            if chg and chg[1]:
                                # 取同窗口价（用当前 mid；无价史则退化到 0）
                                price_now = self._mids.get(coin, 0.0)
                                price_past = self._last_close.get(coin, 0.0)
                                oi_vel = oi_directional_velocity(
                                    chg[0], chg[1], price_now, price_past
                                )
                        pred = self.flow_predictor.predict(coin, now, book_imb, oi_vel)
                        if pred and now - self._flow_pred_seen.get(coin, 0) >= 600_000:
                            self._flow_pred_seen[coin] = now
                            msg = (f"[{_ts(now)}] {pred.fmt()}{self._price_tag(coin)}"
                                   + self.efficacy.label_of("前瞻"))
                            print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
                            self._push(msg)
                            self._record_pred(coin, "前瞻", pred.direction)
                            # 落库前瞻预测供 ConfluenceAggregator 作为领先独立共振源
                            self.store.insert_flow_prediction((
                                now, coin, pred.direction,
                                float(pred.score),
                                float(pred.flow_velocity),
                                float(pred.flow_accel),
                                float(pred.book_imbalance),
                            ))
            except Exception as e:  # noqa: BLE001
                log.warning("前瞻预测失败: %s", e)
    async def _periodic_correlation(self, every: float = 300.0) -> None:
        """实时地址关联：从近 30min meme 成交检测协同行动的地址群(疑似庄家集团)。

        硬编码判别核心：滑窗+不应期统计协同事件 + 跨币数。跨币(coins≥2)是同一实体的硬证据，
        单币人群(coins=1)降级为「疑似拉盘人群」标注，避免把追涨散户误判为庄家集团。
        """
        while not self._stopping:
            now = int(time.time() * 1000)
            try:
                # min_coins=2：要求跨≥2 个不同币协同——跨市场协同是同一实体的硬证据，
                # 且避免单币重叠把追涨人群污染合并成大团(高精确度路线)。
                # O(W²)滑窗+全表扫描移出事件循环(修审计P2:避免同步阻塞)
                groups = await asyncio.to_thread(
                    self.correlation.clusters_detailed,
                    now - 1_800_000, window_sec=120, min_shared=3, min_coins=2)
            except Exception as e:  # noqa: BLE001
                log.warning("关联扫描失败: %s", e)
                await asyncio.sleep(every)
                continue
            for d in groups:
                g = d["members"]
                if len(g) < 2:
                    continue
                sig = tuple(sorted(g))
                if sig in self._seen_clusters:
                    continue
                self._seen_clusters.add(sig)
                # 核心地址的最相关伙伴(correlated_with)
                rel = await asyncio.to_thread(
                    self.correlation.correlated_with, g[0], now - 1_800_000, min_shared=2)
                rel_s = (" 核心" + g[0][:8] + "…最相关:"
                         + ",".join(f"{a[:8]}…×{c}" for a, c in rel[:3])) if rel else ""
                strength = f"跨{d['coins']}币·协同{d['events']}次·{d['links']}对"
                # lead-lag：识别群内谁先动（核心 leader），供跟庄前瞻决策
                try:
                    leader_info = await asyncio.to_thread(
                        self.correlation.cluster_leader, g, now - 1_800_000, window_sec=120)
                except Exception:  # noqa: BLE001
                    leader_info = None
                leader_s = (f" 核心leader:{leader_info[0][:10]}…(领先{leader_info[1]}次)"
                            if leader_info else "")
                msg = (f"[{_ts(now)}] 🕸️庄家集团(跨市场协同,{len(g)}地址,{strength}): "
                       + "、".join(a[:10] + "…" for a in g[:6])
                       + (f" 涉{','.join(d['coin_list'][:4])}" if d['coin_list'] else "")
                       + rel_s + leader_s)
                print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
                self._push(msg)
            await asyncio.sleep(every)
    async def _periodic_report(self, every: float = 3600.0) -> None:
        """周期摘要日报：控制台打印 + webhook 推送。"""
        while not self._stopping:
            try:
                now = int(time.time() * 1000)
                report = build_report(self.store, now - int(every * 1000), now)
                lat = self.latency.fmt()              # 热路径延迟实测(P50/P99/max)
                if lat:
                    report += f"\n\n⏱️ 热路径延迟(实测):\n{lat}"
                print(f"\n{report}\n")
                self._push(report)
            except Exception as e:  # noqa: BLE001
                log.warning("周期日报任务失败: %s", e)
            await asyncio.sleep(every)
    def _hardcoded_context(self, now: int) -> str:
        """汇总硬编码核心算法产出(庄家集团 + 聪明钱筛选 Top)，供 LLM 分析层研判。

        体现「硬编码才是核心，LLM 只做分析」：确定性算法先筛地址/识集团，再把结果喂模型解读。
        """
        lines: list[str] = []
        try:
            groups = self.correlation.clusters_detailed(
                now - 1_800_000, window_sec=120, min_shared=3, min_coins=2)[:3]
        except Exception:  # noqa: BLE001
            groups = []
        if groups:
            lines.append("🕸️ 庄家集团(算法识别·跨市场协同):")
            for d in groups:
                lines.append(f"  {d['size']}地址 跨{d['coins']}币·协同{d['events']}次 "
                             f"涉{','.join(d['coin_list'][:4])}: "
                             + "、".join(a[:10] + "…" for a in d["members"][:5]))
        try:
            tops = self.store.top_profiles(limit=5)
        except Exception:  # noqa: BLE001
            tops = []
        if tops:
            lines.append("🔍 聪明钱筛选 Top(算法评分):")
            for r in tops:
                lines.append(f"  {r[0][:10]}… 评分{r[1]:.0f} 全期${r[3]:,.0f} "
                             f"近月${r[4]:,.0f} 偏{r[8]} {r[9]}")
        return "\n".join(lines)
    async def _periodic_llm(self) -> None:
        """LLM(Codex GPT-5.4) 前瞻研判：周期把双所态势摘要 + 硬编码核心产出喂给模型 → 抓庄研判 → 推送。

        未配置 llm.enabled 时本任务立即退出(不占资源)；研判失败优雅降级(不影响监控)。
        """
        if self.analyst is None:
            return
        every = float(getattr(self.cfg.llm, "interval_sec", 3600.0))
        while not self._stopping:
            await asyncio.sleep(every)
            now = int(time.time() * 1000)
            report = build_report(self.store, now - int(every * 1000), now,
                                  title="抓庄态势")
            verdict = await self.analyst.analyze(report, extra=self._hardcoded_context(now))
            if verdict:
                msg = f"🧠 LLM 抓庄研判（GPT-5.4 · {_ts(now)}）\n{verdict}"
                print(f"\n{'🧠'*30}\n{msg}\n{'🧠'*30}\n")
                self._push(msg)
    async def _periodic_consensus(self, every: float = 90.0) -> None:
        """多庄共识扫描 + 庄持仓面板（谁在多/空什么）。

        首轮短延迟（依赖 webData2 异步填充地址持仓，至少等 30s 让播种完成）；之后 work-first。
        """
        # 首轮短延迟：地址持仓由 WS webData2 异步播种，需等片刻才有数据
        await asyncio.sleep(min(every, 30.0))
        while not self._stopping:
            positions = self.address_monitor.all_positions()
            if not positions or not self._mids:
                await asyncio.sleep(every)
                continue
            labels = {a: self.address_monitor.label_of(a) for (a, _c) in positions}
            now = int(time.time() * 1000)
            try:
                self.consensus.scan(positions, self._mids, labels, now)
                self.pos_tracker.scan(positions, self._mids, labels, now)   # 平仓/反手/减仓预警
                self.confluence.scan(now)                                   # 多信号叠加共振
                panel = positioning(positions, self._mids, labels)[:6]
                if panel:
                    print(f"[{_hms()}] 📋 庄持仓面板(净名义Top):")
                    for p in panel:
                        side = "净多🟢" if p.net_notional >= 0 else "净空🔴"
                        print(f"     {p.coin:<8} {side} ${abs(p.net_notional):,.0f} "
                              f"(多{p.n_long}/空{p.n_short})")
            except Exception as e:  # noqa: BLE001
                log.warning("共识扫描失败: %s", e)
            await asyncio.sleep(every)
    async def _periodic_divergence(self, every: float = 60.0) -> None:
        """三源背离扫描：逐 meme 比对 CEX 资金费/OI 与 DEX 聪明钱流向。"""
        while not self._stopping:
            await asyncio.sleep(every)
            if not self.oi_monitor:
                continue
            now = int(time.time() * 1000)
            for symbol, coin in self.oi_monitor.symbol_to_coin.items():
                row = self.store.latest_oi(symbol)
                if not row:
                    continue
                funding = row[5] or 0.0
                chg = self.store.oi_change(symbol, window_ms=900_000, now_ms=now)
                oi_pct = (chg[0] - chg[1]) / chg[1] if chg and chg[1] else 0.0
                hl = self.canon_to_hl.get(coin)
                dex_flow = self.meme_monitor.coin_net(hl) if hl else 0.0
                self.divergence.evaluate(coin, funding, oi_pct, dex_flow, now)
    async def _periodic_review(self, every: float = 1800.0) -> None:
        """预测准确率回顾：定期评估到期预测，并（有样本时）推送准确率摘要报告。

        评估间隔 30 分钟（1800s），确保每小时预测在 2 次内完成评估；
        报告仅当 evaluate_due 实际评估到条目时推送，避免空推扰频道。
        """
        while not self._stopping:
            now = int(time.time() * 1000)

            # 构造 price_of：优先 HL allMids，回退 Bitget OI 标记价
            def price_of(coin: str) -> float | None:
                px = _f(self._mids.get(coin, 0.0))
                if px > 0:
                    return px
                sym = self.coin_to_symbol.get(normalize(coin))
                if self.oi_monitor and sym:
                    pc = self.oi_monitor.price_change(sym)
                    if pc is not None:
                        v = _f(pc[0])
                        return v if v > 0 else None
                return None

            try:
                n = self.review.evaluate_due(price_of, now)
                if n > 0:
                    # 产出过去 24 小时的准确率报告，推送给频道供复盘
                    rep = self.review.accuracy_report(now - 86_400_000, now)
                    summary = fmt_accuracy(rep)
                    self._push("📊 " + summary)
                    print(f"\n{'='*60}\n{summary}\n{'='*60}\n")
            except Exception as exc:  # noqa: BLE001 — 回顾失败不影响监控热路径
                log.warning("预测回顾任务失败: %s", exc)
            await asyncio.sleep(every)
    async def _periodic_efficacy(self, every: float = 1800.0) -> None:
        """信号有效性自适应加权刷新：定期从 predictions 表读取历史评估，更新各 kind 权重。

        仅当任一 kind 达到 min_sample 才推送（避免空推/噪声）；失败不影响监控热路径。
        """
        while not self._stopping:
            now = int(time.time() * 1000)
            try:
                table = self.efficacy.refresh(now)
                fmt = self.efficacy.fmt()
                print(f"\n[{_hms(now)}] 📈 信号有效性(实证自适应):\n{fmt}\n")
                # 仅当存在任一 kind 已达 min_sample 时才推送，避免空推/噪声
                has_sufficient = any(e.n >= self.efficacy.min_sample for e in table.values())
                if has_sufficient:
                    self._push("📈 信号有效性(实证自适应):\n" + fmt)
            except Exception as exc:  # noqa: BLE001 — 失败不影响监控热路径
                log.warning("信号有效性刷新失败: %s", exc)
            await asyncio.sleep(every)
    async def _periodic_solana(self, every: float = 120.0) -> None:
        """SOL meme 供应量监控（mint/burn）。较长间隔，避开公开 RPC 限流。"""
        while not self._stopping:
            await asyncio.sleep(every)
            try:
                now = int(time.time() * 1000)
                for ch in await self.sol_monitor.poll_once(now):
                    arrow = "增发⚠" if ch.kind == "mint" else "销毁"
                    print(f"[{_hms()}] 🪙 [SOL供应] {ch.coin} {arrow} {ch.pct*100:+.2f}% "
                          f"({ch.prev_supply:,.0f}→{ch.new_supply:,.0f})")
            except Exception as e:  # noqa: BLE001
                log.warning("Solana 供应轮询失败: %s", e)
    async def _periodic_wallet_portfolio(self, every: float = 300.0) -> None:
        """钱包完整持仓画像周期刷新（每 5 分钟）：拉全量持仓、落库、控制台打印+推送。"""
        while not self._stopping:
            if not self.cfg.watchlist:
                await asyncio.sleep(every)
                continue
            now = int(time.time() * 1000)
            try:
                snaps = await self.wallet_portfolio.refresh(self.cfg.watchlist, now)
                for snap in snaps:
                    if is_placeholder_addr(snap.address):
                        continue  # 跳过占位/零地址(0x0..0 示例配置残留)——非真实钱包
                    if snap.is_empty:
                        continue  # 跳过空画像(净值$0/0持仓/无币种方向)——用户#要求去噪，不推空壳地址
                    fmt = self.wallet_portfolio.fmt(snap)
                    print(fmt)
                    self._push(fmt)
            except Exception as e:  # noqa: BLE001
                log.warning("钱包持仓画像刷新失败: %s", e)
            await asyncio.sleep(every)
    async def _periodic_exchange_flow(self, every: float = 3600.0) -> None:
        """交易所链上资金流监控（BTC，keyless blockstream）：每小时核对各所 24h 净流入/流出。

        大额净流入交易所 → 潜在抛压；净流出 → 吸筹。仅对越阈值（threshold_btc）的推送告警。
        """
        if self.exchange_flow is None:
            return
        while not self._stopping:
            now = int(time.time() * 1000)
            try:
                rows = await self.exchange_flow.poll_once(now)
                for row in rows:
                    arrow = "净流入🔴" if row["net"] >= 0 else "净流出🟢"
                    print(f"[{_hms(now)}] 🏦 [交易所流] {row['exchange']} {row['chain']} "
                          f"{arrow} {abs(row['net']):,.0f} BTC "
                          f"(入{row['inflow']:,.0f}/出{row['outflow']:,.0f})")
                    if row.get("alert"):
                        self._push(f"[{_ts(now)}] {fmt_flow_alert(row)}")
            except Exception as e:  # noqa: BLE001 — 资金流轮询失败不影响监控
                log.warning("交易所资金流轮询失败: %s", e)
            await asyncio.sleep(every)
    async def _periodic_whale_pnl(self, every: float = 300.0) -> None:
        """庄 PnL 快照周期落库（每 5 分钟）：从排行榜拉 PnL 写 whale_pnl_snapshots 表。

        解决审计发现的 streaming 从未接入 WhaleMomentum → 表永空 → dashboard 健康报"空表"问题。
        work-first：启动后先落一次数据，再进入周期睡眠。失败 log.warning 不崩。
        注：排行榜拉取约 16MB，需要网络，首次与 _seed 并发（_seed 完成后 run() 调度）。
        """
        while not self._stopping:
            now = int(time.time() * 1000)
            try:
                rows = await fetch_leaderboard_rows()
                if rows:
                    pnl_rows = pnl_rows_from(rows, top_n=50)
                    if pnl_rows:
                        self.whale_momentum.snapshot(pnl_rows, now)
                        log.info("庄 PnL 快照落库 %d 条", len(pnl_rows))
            except Exception as e:  # noqa: BLE001
                log.warning("庄 PnL 快照失败: %s", e)
            await asyncio.sleep(every)
    async def _periodic_discover(self, every: float = 3600.0) -> None:
        """周期排行榜发现新聪明钱（每 1 小时）：即使 watchlist 非空也定期拉排行榜刷新候选庄。

        解决审计发现的 discover_smart_money 只在 watchlist 为空时运行 → 排行榜永不刷新 →
        健康巡检报"排行榜缓存未建立"。新发现地址 merge 进 watchlist + 订阅监控 + 落库。
        work-first：启动后先跑一次（与 _seed 错开 30s，避免重叠网络）。
        """
        # 等 _seed 播种完成后再首轮发现（避免与播种并发争 REST 带宽）
        await asyncio.sleep(30.0)
        while not self._stopping:
            now = int(time.time() * 1000)
            try:
                discovered = await discover_smart_money(top_n=15)
                existing = {w.address.lower() for w in self.cfg.watchlist}
                added = 0
                for w in discovered:
                    if w.address.lower() not in existing:
                        self.cfg.watchlist.append(w)
                        existing.add(w.address.lower())
                        self.address_monitor.subscribe_address(w)
                        self.store.upsert_wallet(w.address, w.label, "discover", now)
                        added += 1
                if added:
                    log.info("排行榜周期发现新庄地址 %d 个", added)
                else:
                    log.debug("排行榜周期发现：无新增地址（watchlist=%d）", len(self.cfg.watchlist))
            except Exception as e:  # noqa: BLE001
                log.warning("排行榜周期发现失败: %s", e)
            await asyncio.sleep(every)
