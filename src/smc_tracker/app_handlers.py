"""事件回调 mixin：_price_tag / _on_* / _push* / _emit / _periodic_push_drain。"""
from __future__ import annotations

import asyncio
import time
import logging
from typing import Any

from .config import WatchAddress
from .indicators import analyze as ta_analyze, fmt_analysis
from .memecoins import normalize
from .monitor import EventType, SmartMoneyEvent
from .signals import ConfluenceSignal, ConsensusSignal, DivergenceSignal, PositionChange, Signal, pred_kind
from .smc import LiquidityEngine, StructureEvent, ZoneEngine
from .util import fmt_hms as _hms
from .util import fmt_ts as _ts
from .util import to_float as _f
from .util import fmt_px as _fmt_px

log = logging.getLogger("app")


class EventHandlersMixin:
    # ---- 价格标签 ----
    def _price_tag(self, coin: str) -> str:
        """返回形如 ' 💲$0.08350 (Bitget现价)' 的实时**价格**标签（带数值来源），无数据时返回空串。

        用户#要求：涨跌幅/费率/OI 等行情维度不需要 → 价格标签只保留**价格 + 数据来源**（核心抓庄信号
        附现价上下文即可，行情维度噪声移除）。优先 Bitget OI monitor 价，回退 HL allMids。
        """
        px: float = 0.0
        src = ""
        # 1) 先查 Bitget（meme 永续 lastPr）
        sym = self.coin_to_symbol.get(normalize(coin))
        if self.oi_monitor and sym:
            tk = self.oi_monitor.ticker(sym)
            if tk is not None:
                px = tk["price"]
                src = "Bitget现价"
        # 2) 回退到 HL allMids
        if px <= 0:
            px = self._mids.get(coin, 0.0)
            src = "HL现价"
        if px <= 0:
            return ""
        # 仅价格（非科学计数法完整数字）+ 数值来源
        return f" 💲${_fmt_px(px)} ({src})"

    # ---- 回调 ----
    def _on_sm_event(self, evt: SmartMoneyEvent) -> None:
        big = evt.notional >= self.cfg.detection.large_fill_notional_usd
        if self.cfg.output.console:
            print(f"[{_hms(evt.time_ms)}] {'🔴' if big else '  '} {evt.fmt()}")
        # A2a：热路径不直接写 DB，append 入缓冲（_periodic_flush 每 5s 批量落，不阻塞 event loop）
        # 推送链路不依赖 DB 读，仍即时；sm_events 仅复盘用，5s 延迟无害
        self._sm_buffer.append((
            evt.time_ms, evt.type.value, evt.address, evt.label, evt.coin,
            evt.side.name, evt.sz, evt.px, evt.notional,
            evt.position_before, evt.position_after, evt.closed_pnl, int(evt.is_taker)))
        # 🐋 跟庄信号：仅累积「建/加/反手仓」(position-increasing)的净流向；
        #    HL 大单会碎成多笔，故在时间窗内累积该庄对该 coin 的净建仓额，越阈值才发信号。
        if evt.type in (EventType.OPEN, EventType.ADD, EventType.FLIP):
            key = (evt.address, evt.coin)
            signed = evt.notional if evt.side.name == "BUY" else -evt.notional
            acc = self._whale_acc.get(key)
            if acc is None or evt.time_ms - acc[1] > self._WHALE_WINDOW_MS:
                acc = [0.0, evt.time_ms, acc[2] if acc else 0]   # 窗口过期重置(保留冷却ts)
            acc[0] += signed
            self._whale_acc[key] = acc
            thr = self.cfg.detection.large_fill_notional_usd
            cooled = acc[2] == 0 or evt.time_ms - acc[2] >= self._WHALE_COOLDOWN_MS
            if abs(acc[0]) >= thr and cooled:
                net = acc[0]
                direction = "long" if net > 0 else "short"
                self.store.insert_whale_signal((
                    evt.time_ms, evt.address, evt.label, evt.coin, evt.type.value,
                    direction, abs(net), evt.px, evt.position_after, int(evt.is_taker)))
                d = "做多🟢" if direction == "long" else "做空🔴"
                # #192 撤回 #190 的入场领先标注:#186 的"+0.46%/4h"经币内配对+更好覆盖证伪(coin-selection 伪影,
                # 扣成本净利负、净胜率<50%);庄技巧持续(#185)但盈利非来自可复制入场timing→信号作"聪明钱活跃"语境,非"照入场跟单即盈利"。
                msg = (f"[{_ts(evt.time_ms)}] 🐋跟庄信号 {evt.label or evt.address[:8]} "
                       f"净{d} {evt.coin} ${abs(net):,.0f}(3min累积) @ {_fmt_px(evt.px)}(HL成交价)"
                       + self._price_tag(evt.coin)
                       + self.efficacy.label_of("跟庄"))
                print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
                self._emit("whale", msg)
                self._record_pred(evt.coin, "跟庄", direction)
                acc[2] = evt.time_ms
                acc[0] = 0.0

    def _on_suspicious(self, info: dict) -> None:
        """公开成交流里发现激进建仓的可疑地址 → 标记 + 升级为全量追踪（不放过）。"""
        addr = info["address"]
        now, coin, net = info["time_ms"], info["coin"], info["net_usd"]
        known = addr.lower() in {w.address.lower() for w in self.cfg.watchlist}
        if known or self.store.is_flagged(addr):
            self.store.flag_address(addr, now, coin, "复现", net, promoted=1)
            return
        reason = f"meme激进{'净买' if net > 0 else '净卖'}{coin}"
        self.store.flag_address(addr, now, coin, reason, net, promoted=1)
        label = f"可疑庄({addr[:8]})"
        self.address_monitor.subscribe_address(WatchAddress(
            addr, label, notional_alert_usd=self.cfg.detection.large_fill_notional_usd))
        self.cfg.watchlist.append(WatchAddress(addr, label))
        d = "净买🟢" if net > 0 else "净卖🔴"
        msg = (f"[{_ts(now)}] 🚨可疑地址 {addr[:10]}… {d} {coin} "
               f"${abs(net):,.0f}(3min累积) → 已升级全量追踪"
               + self._price_tag(coin))
        print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
        if self.cfg.digest.enabled:
            self.hl_digest.add_bias(coin, net > 0, "可疑")
        self._emit("suspicious", msg, urgent=True)

    def _on_wall_signal(self, ev: dict) -> None:
        """l2Book 大额挂单墙动态（领先意图）→ 仅对新出现的大墙(build)节流推送。

        诚实定位：挂单墙是「尚未成交的意图」，先于成交，但可能 spoof（虚挂诱导）/冰山，
        非确定方向，须与成交/OI 交叉验证。落库由 monitor.flush() 负责，此回调仅做告警。
        kind="build"(出现)/"pull"(抽单)；side="bid"(支撑/吸筹意图)/"ask"(压制/分销意图)。
        """
        if ev.get("kind") != "build":
            return  # 仅前瞻性的「墙出现」推送；抽单(pull)仅落库供 dashboard 复盘
        coin, side = ev.get("coin", ""), ev.get("side", "")
        ntl = _f(ev.get("notional"))
        # 仅推送显著大墙（≥2× 默认大单阈值），且同币同侧 5 分钟冷却，避免高频刷屏
        if ntl < self.cfg.detection.large_fill_notional_usd * 2:
            return
        now = int(ev.get("ts") or time.time() * 1000)
        key = (coin, side)
        if now - self._wall_seen.get(key, 0) < 300_000:
            return
        self._wall_seen[key] = now
        side_tag = "🟢bid墙(支撑/吸筹意图)" if side == "bid" else "🔴ask墙(压制/分销意图)"
        msg = (f"[{_ts(now)}] 🧱挂单墙 {coin} {side_tag} @ {_fmt_px(ev.get('px', 0.0))} "
               f"${ntl:,.0f}(领先意图·可能spoof，须与成交/OI 交叉验证)"
               + self._price_tag(coin))
        print(f"[{_hms(now)}] {msg}")
        # 挂单墙：digest 开启 → 按币结构化聚合（汇总卡片出「整体分析+单币总结」，非逐条原始）；
        # 关闭 → 回退逐条即时推（旧行为）。用真实 l2Book 墙事件，无模拟数据。
        if self.cfg.digest.enabled:
            self.hl_digest.add_wall(coin, side, ntl, _f(ev.get("px", 0.0)))
        else:
            self._push(msg)

    def _on_meme_trade(self, t: dict) -> None:
        # 大单 meme 成交（含主动方地址）。t 是 MemeTradeMonitor on_trade 传入的 record dict
        # （键 coin/taker_side/notional/taker，见 meme_trade_monitor.py:28,100-108），
        # 须按 dict 取键——此前误用属性访问导致 AttributeError 被回调 try/except 吞掉、告警静默失效。
        print(f"[{_hms()}] 🟡 [meme] {t['coin']} {'买' if t['taker_side']=='B' else '卖'} "
              f"${t['notional']:,.0f} taker={t['taker'][:12]}…")

    def _on_structure(self, coin: str, e: StructureEvent) -> None:
        arrow = "↑" if e.direction == "bull" else "↓"
        print(f"[{_hms()}] 📐 [SMC] {coin} {e.type} {arrow} 突破 {_fmt_px(e.level)} "
              f"(trend→{self.structure.structure(coin).trend})")
        # 结构事件 = 信号触发点：先刷新该 coin 的聪明钱流向 + OI 环境，再评估共振
        now = int(time.time() * 1000)
        self.signal_engine.set_flow(coin, self.meme_monitor.coin_net(coin))
        symbol = self.coin_to_symbol.get(normalize(coin))
        if symbol:
            # A2b：改读 OI monitor 内存环形缓存，完全不碰 DB（热路径）。
            # 回退：内存无足够历史时返回 None，与 `if chg and chg[1]` 守卫兼容。
            chg = (self.oi_monitor.oi_window(symbol, window_ms=600_000, now_ms=now)
                   if self.oi_monitor else None)
            if chg and chg[1]:
                self.signal_engine.set_oi_change(coin, (chg[0] - chg[1]) / chg[1])
        # 区域共振：突破方向是否存在未回补的同向 OB/FVG
        ze = self.zones.get(coin)
        self.signal_engine.set_zone(coin, bool(ze and ze.active_zones(e.direction)))
        # 流动性扫荡确认：近 30 分钟内是否有同向扫荡（聪明钱反转信号）
        sw = self._last_sweep.get(coin)
        want = "bullish" if e.direction == "bull" else "bearish"
        self.signal_engine.set_sweep(
            coin, bool(sw and sw[0] == want and now - sw[1] <= 1_800_000))
        # 风险参数价位：当前价 + 结构摆动位 + 同向 OB 边界
        ms = self.structure.structure(coin)
        ob_bottom = ob_top = 0.0
        if ze:
            obs = [z for z in ze.active_zones(e.direction) if z.kind == "OB"]
            if obs:
                ob = max(obs, key=lambda z: z.created_at)
                ob_bottom, ob_top = ob.bottom, ob.top
        self.signal_engine.set_levels(
            coin, self._last_close.get(coin, 0.0),
            swing_low=ms.ref_low.price if ms and ms.ref_low else 0.0,
            swing_high=ms.ref_high.price if ms and ms.ref_high else 0.0,
            ob_bottom=ob_bottom, ob_top=ob_top)
        self.signal_engine.on_structure(coin, e, now)

    def _on_closed_candle(self, coin: str, candle) -> None:
        self._last_close[coin] = candle.c
        # 维护近 K 线缓冲 → 暴涨暴跌实时预警 + KNN/TA 特征(用户#:KNN 需 2000 bar 训练集)
        buf = self._candles.setdefault(coin, [])
        buf.append(candle)
        if len(buf) > 2100:                  # 保留 ~2000 bar(KNN feature_matrix 训练集;DB 滚动 3000 充足)
            del buf[:100]
        now = int(time.time() * 1000)
        if now - self._pump_seen.get(coin, 0) >= 1_800_000:   # 30min 冷却
            alert = self.pump_radar.evaluate(coin, buf, now)
            if alert is not None:
                self._pump_seen[coin] = now
                ctx = fmt_analysis(coin, ta_analyze(buf, now))   # 附 TA 全景上下文
                # 暴涨/暴跌分桶(修审计 P2:原 dump 也记入「暴涨」桶并贴「暴涨命中%」,分母语义错位)
                kind_label = "暴涨" if alert.kind == "pump" else "暴跌"
                msg = (f"[{_ts(now)}] {alert.fmt()}{self._price_tag(coin)}"
                       + self.efficacy.label_of(kind_label)
                       + f"\n{ctx}")
                print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
                self._emit("pump", msg)
                self._record_pred(coin, kind_label, "up" if alert.kind == "pump" else "down")
        # 放量监控(成交量异动)
        vev = self.volume_monitor.update(coin, candle)
        if vev is not None:
            print(f"[{_hms(now)}] 📊 [放量] {coin} {vev['ratio']:.1f}× 均量 (量={_fmt_px(vev['vol'])})")
        # TA 多因子信号(指标+combo+PA+双顶双底+道氏 全链路在生产执行)
        if len(buf) >= 60 and now - self._ta_seen.get(coin, 0) >= 1_800_000:
            sig = self.ta_signal.evaluate(buf, None, now)
            if sig is not None:
                self._ta_seen[coin] = now
                print(f"[{_ts(now)}] 📐 {self.ta_signal.fmt(sig)}{self._price_tag(coin)}")
                if self.cfg.digest.enabled:
                    self.hl_digest.add_bias(coin, sig["direction"] == "long", "TA")
                self._emit("ta", f"[{_ts(now)}] {self.ta_signal.fmt(sig)}{self._price_tag(coin)}")
        ze = self.zones.get(coin)
        if ze is None:
            ze = ZoneEngine(min_gap_pct=self.cfg.smc.fvg_min_gap_pct)
            self.zones[coin] = ze
        for z in ze.update(candle):
            tag = "看涨" if z.direction == "bull" else "看跌"
            print(f"[{_hms()}] 🟦 [{z.kind}] {coin} {tag} 区 [{_fmt_px(z.bottom)}, {_fmt_px(z.top)}]")
        # 流动性扫荡
        le = self.liquidity.get(coin)
        if le is None:
            le = LiquidityEngine(lookback=self.cfg.smc.swing_lookback)
            self.liquidity[coin] = le
        for sw in le.update(candle):
            self._last_sweep[coin] = (sw.direction, candle.close_time_ms)
            tag = "看涨(扫SSL)" if sw.direction == "bullish" else "看跌(扫BSL)"
            eq = "等高等低" if sw.equal else ""
            print(f"[{_hms()}] 💧 [扫荡] {coin} {tag} @ {_fmt_px(sw.price)} {eq}")

    def _record_pred(
        self, coin: str, kind: str, direction: str, horizon_ms: int | None = None,
        bg_px_override: float | None = None,
    ) -> None:
        """记录前瞻预测到回顾层，统一 MTF 多水平线落库（发推后立即调用）。

        读 cfg.review.horizons_min 转 ms 批量记录 7 个 TF（5m/15m/30m/1h/4h/12h/1d）。
        horizon_ms 参数保留向后兼容签名，但统一走 MTF（忽略显式单值，保持所有信号源一致性）。
        hl_px：从 self._mids 取；bg_px：从 Bitget OI 监控取价格（price_change()[0]）。
        任何失败不影响主推送热路径。
        """
        # 币种多空比例（用户#）：此 choke point 覆盖 跟庄/SMC/背离/共识/超级/暴涨 六类方向信号
        if self.cfg.digest.enabled:
            self.hl_digest.add_bias(coin, direction in ("long", "up", "bullish"), kind)
        try:
            hl = _f(self._mids.get(coin, 0.0))
            bg = 0.0
            if bg_px_override is not None and bg_px_override > 0:
                # 谐波等 Bitget 宇宙币：直接用调用方传入的 Bitget 价（修 H_price：
                # coin_to_symbol 仅含 meme 币，谐波币走此处避免静默丢失幸存者偏差）
                bg = _f(bg_px_override)
            else:
                sym = self.coin_to_symbol.get(normalize(coin))
                if self.oi_monitor and sym:
                    pc = self.oi_monitor.price_change(sym)
                    if pc is not None:
                        bg = _f(pc[0])
            # MTF：7 个时间段各记一条，诊断哪个 TF 有真 alpha
            horizons_ms = [h * 60_000 for h in self.cfg.review.horizons_min]
            self.review.record_mtf(
                ts=int(time.time() * 1000),
                coin=coin,
                kind=kind,
                direction=direction,
                hl_px=hl,
                bg_px=bg,
                horizons_ms=horizons_ms,
            )
        except Exception as exc:  # noqa: BLE001 — 记录失败不影响推送
            log.debug("_record_pred 失败 %s %s: %s", kind, coin, exc)

    def _enqueue_push(self, text: str, notifier: Any) -> None:
        """A4 背压入队：满时丢最旧一条（get_nowait 弃头）再入新，保证最新告警不丢、队列有界。

        maxsize=2000，drain worker 37.5条/min，正常工况远不触发背压。
        满时不静默：弃头 + _push_dropped 计数 + log.warning。
        """
        try:
            self._push_queue.put_nowait((text, notifier))
        except asyncio.QueueFull:
            try:
                self._push_queue.get_nowait()      # 弃最旧
                self._push_queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self._push_queue.put_nowait((text, notifier))
            self._push_dropped += 1
            log.warning("push queue 已满，丢弃最旧告警（累计丢弃=%d）", self._push_dropped)

    def _push(self, text: str) -> None:
        """非阻塞**入队**推送到 HL 主通道：经单一 worker 按最小间隔逐条发，
        避免飞书(1.5s)/TG(1.2s) 限流静默丢卡（多周期任务并发推送易撞车）。"""
        if self.notifier.enabled:
            self._enqueue_push(text, self.notifier)

    def _push_harmonic(self, text: str) -> None:
        """谐波系统**专用通道**推送（独立飞书，与 HL 分开）；未配置独立飞书时回退主通道。"""
        n = self.harmonic_notifier
        if getattr(n, "enabled", False):
            self._enqueue_push(text, n)

    async def _periodic_push_drain(self, min_interval_sec: float = 1.6) -> None:
        """推送排队串行发送：单 worker 逐条出队、按最小间隔(>飞书1.5s)发送到**指定 notifier**，杜绝限流丢卡。

        发送失败只 log.warning 不中断（韧性）；min_interval 略大于飞书 1.5s/TG 1.2s 限流窗口。
        """
        while not self._stopping:
            text, notifier = await self._push_queue.get()
            try:
                if getattr(notifier, "enabled", False):
                    await notifier.send(text, int(time.time() * 1000))
            except Exception as exc:  # noqa: BLE001 — 单条发送失败不拖垮队列
                log.warning("推送发送失败: %s", exc)
            finally:
                self._push_queue.task_done()
            await asyncio.sleep(min_interval_sec)

    def _emit(self, category: str, text: str, urgent: bool = False) -> None:
        """HL 事件出口：digest 开启则按分类入汇总缓冲（周期合并成一张分类卡片，降噪去刷屏）；
        关闭则回退旧行为（每条即时推）。urgent 且配置允许 → 核心前瞻信号仍即时单独推（不延迟）。"""
        if not self.cfg.digest.enabled:
            self._push(text)
            return
        self.hl_digest.add(category, text)
        if urgent and self.cfg.digest.urgent_instant:
            self._push(text)

    def _on_signal(self, sig: Signal) -> None:
        msg = f"[{_ts(sig.ts)}] {sig.fmt()}" + self.efficacy.label_of("SMC")
        print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
        self._emit("signal", msg)
        self._record_pred(sig.coin, "SMC", sig.direction)   # 进回顾闭环(事后评估命中率→efficacy 加权)

    def _on_divergence(self, sig: DivergenceSignal) -> None:
        # 同币同向背离冷却：_periodic_divergence 每 60s 扫描，条件持续会每分钟重复同一背离
        #（实测 TRUMP吸筹/PEPE分销 每分钟刷屏）→ 15min 冷却，降噪 + 避免高自相关预测污染命中率。
        key = (sig.coin, sig.direction)
        if sig.ts - self._div_seen.get(key, 0) < self._DIV_COOLDOWN_MS:
            return
        self._div_seen[key] = sig.ts
        # #193 降级:#170 逼空"+0.83pp"与 #186/#187 同类(小coin样本前瞻收益,n=17),该类已两次币内配对证伪翻转→
        # edge 未确立,不再宣称"✅有edge";拆 kind 仅为生产持续审判,以 efficacy 实盘命中率为准,两侧均当弱上下文。
        edge_mark = (" (逼空背离·edge未确立#193,看实盘efficacy)" if sig.direction == "bullish"
                     else " (分销背离·实测弱~0)")
        # #176:落表/efficacy label 同走拆分 kind(逼空背离/分销背离),让实盘独立审判不对称 edge,
        # 闭合"展示已减噪(edge_mark)、验证仍混桶"的裂缝。pred_kind 为两生产路径共用单一真相源。
        pk = pred_kind(sig.direction)
        msg = (f"[{_ts(sig.ts)}] {sig.fmt()}{self._price_tag(sig.coin)}{edge_mark}"
               + self.efficacy.label_of(pk))
        print(msg)
        self._emit("divergence", msg)
        self._record_pred(
            sig.coin, pk, "up" if sig.direction == "bullish" else "down"
        )

    def _on_consensus(self, sig: ConsensusSignal) -> None:
        msg = (f"[{_ts(sig.ts)}] {sig.fmt()}{self._price_tag(sig.coin)}"
               + self.efficacy.label_of("共识"))
        print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
        self._emit("consensus", msg)
        self._record_pred(sig.coin, "共识", sig.direction)

    def _on_pos_change(self, pc: PositionChange) -> None:
        msg = f"[{_ts(pc.ts)}] {pc.fmt()}"
        print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
        self._emit("position", msg)

    def _on_confluence(self, sig: ConfluenceSignal) -> None:
        msg = f"[{_ts(sig.ts)}] {sig.fmt()}" + self.efficacy.label_of("超级")
        print(f"\n{'🌟'*30}\n{msg}\n{'🌟'*30}\n")
        self._emit("super", msg, urgent=True)
        self._record_pred(sig.coin, "超级", sig.direction)   # 进回顾闭环

    def _on_candle_ws(self, data, recv_ns: int = 0) -> None:
        """WS K 线推送入口：埋点「接收→处理(含收盘信号计算)」端到端延迟。

        recv_ns 是 WS 接收即打的单调戳(monotonic_ns)，此处同钟测量差值=真实端到端延迟。
        """
        self.structure.on_candle_ws(data, recv_ns)
        if recv_ns:
            self.latency.record("接收→处理", (time.monotonic_ns() - recv_ns) / 1e6)

    def _on_all_mids(self, data, recv_ns) -> None:
        for coin, px in (data.get("mids") or {}).items():
            v = _f(px)                       # 统一安全解析(拒 NaN/inf/脏值)
            if v > 0:
                self._mids[coin] = v

    def _on_oi_surge(self, evt: dict) -> None:
        """OI 异动回调，匹配 BitgetOIMonitor.SurgeCallback 协议（单 dict 参数）。

        evt keys: symbol, prev_oi, oi_size, change (比率), ts, coin
        修复：原先签名 (symbol, prev, cur) 与协议不符，导致每次 OI 异动都抛
        TypeError，回调体从未执行，OI 异动控制台输出静默死亡。
        """
        symbol = evt.get("symbol", "")
        prev = evt.get("prev_oi", 0.0)
        cur = evt.get("oi_size", 0.0)
        pct = (cur - prev) / prev * 100 if prev else 0
        print(f"[{_hms()}] 📊 [OI异动] {symbol} {prev:,.0f}→{cur:,.0f} ({pct:+.2f}%)")
