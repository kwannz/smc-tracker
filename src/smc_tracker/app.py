"""统一双所编排器：System 1 (Hyperliquid) + System 2 (Bitget) 并发运行，全程落 SQLite。

System 1 — Hyperliquid（链上地址级）：
  · AddressMonitor   watchlist 聪明钱地址 → 开/加/减/平/反手 事件
  · MemeTradeMonitor meme 永续成交（带买卖双方地址）→ 地址级净主动流向
  · StructureFeed    实时 K 线 → SMC 市场结构 BOS/CHoCH
System 2 — Bitget（CEX 市场级 + 链上）：
  · BitgetOIMonitor   meme 永续 OI/资金费 实时 + OI 异动
  · OnchainMemeMonitor 公开 EVM RPC 直查 meme 大额链上转账（无 key）

运行：PYTHONPATH=src ./.venv/bin/python -m smc_tracker.app --config config/config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from pathlib import Path

import yaml

from .bitget import BitgetREST, BitgetSub, BitgetWSClient
from .config import Config, WatchAddress, diff_config
from .indicators import VolumeMonitor, analyze as ta_analyze, fmt_analysis
from .hyperliquid import HyperliquidInfo, HyperliquidWSClient, Subscription
from .llm import build_analyst
from .memecoins import normalize
from .monitor import (AddressMonitor, BitgetOIMonitor, EventType, HLOrderbookMonitor,
                      MemeTradeMonitor, SmartMoneyEvent)
from .monitor.whale_discovery import discover_smart_money
from .monitor.address_correlation import AddressCorrelation
from .monitor.wallet_portfolio import WalletPortfolio
from .notify import build_notifier, build_report
from .onchain import (ExchangeFlowMonitor, OnchainMemeMonitor, SolanaSupplyMonitor,
                      fmt_flow_alert)
from .health import HealthMonitor
from .perf import LatencyTracker
from .review import PredictionReview, fmt_accuracy
from .signals import (ConfluenceAggregator, ConfluenceSignal, ConsensusSignal,
                      DivergenceDetector, DivergenceSignal, FlowPredictor, PositionChange,
                      PumpRadar, Signal, SignalEngine, SignalEfficacy, TASignal, WhaleConsensus,
                      WhalePositionTracker, orderbook_imbalance, positioning)
from .smc import LiquidityEngine, StructureEvent, StructureFeed, ZoneEngine
from .storage import Store

log = logging.getLogger("app")

from .util import fmt_hms as _hms          # 简洁 HH:MM:SS（高频控制台行）
from .util import fmt_ts as _ts            # 完整 日期+时间+时区（推送告警，便于事后回顾）
from .util import to_float as _f           # 统一安全数值解析

_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def _fmt_px(px: float) -> str:
    """自适应精度格式化价格：≥1 保 4 位有效数字；<1 用 6 位有效数字去末尾零。"""
    if px >= 1:
        return f"{px:,.4g}"
    return f"{px:.6g}"


EVM_RPC = {
    "ETH": "https://ethereum-rpc.publicnode.com",
    "BSC": "https://bsc-rpc.publicnode.com",
    "BASE": "https://base-rpc.publicnode.com",
}


def load_meme_markets(root: Path) -> list[str]:
    p = root / "config" / "meme_markets.yaml"
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return raw.get("meme_markets") or []


class TradingSystem:
    def __init__(
        self,
        cfg: Config,
        meme_markets: list[str],
        store: Store,
        root: Path,
        cfg_path: str = "",
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.root = root
        self.meme_markets = meme_markets
        # 配置热加载：记录路径 + 初始 mtime，_periodic_config_reload 监控变化
        self._cfg_path: str = cfg_path
        self._cfg_mtime: float = (
            os.path.getmtime(cfg_path) if cfg_path and Path(cfg_path).exists() else 0.0
        )
        now = int(time.time() * 1000)

        # ---- System 1: Hyperliquid ----
        self.hl_ws = HyperliquidWSClient(
            ws_url=cfg.hyperliquid.ws_url,
            ping_interval_sec=cfg.hyperliquid.ping_interval_sec,
            reconnect_max_backoff_sec=cfg.hyperliquid.reconnect_max_backoff_sec,
        )
        self.address_monitor = AddressMonitor(
            cfg.watchlist, self.hl_ws, self._on_sm_event,
            large_fill_notional_usd=cfg.detection.large_fill_notional_usd)
        self.meme_monitor = MemeTradeMonitor(
            meme_markets, self.hl_ws, store,
            large_notional_usd=cfg.detection.large_fill_notional_usd,
            on_trade=self._on_meme_trade,
            on_suspicious=self._on_suspicious,
            suspicious_notional=cfg.detection.large_fill_notional_usd * 2)
        # 挂单墙动态监控（l2Book 领先意图：大额挂单出现/抽单 = 资金就位/收网）。
        # 币集用主流币 cfg.markets（BTC/ETH 等），控制 l2Book 负载——不订阅全 meme，
        # 因 l2Book 逐档高频推送，仅主流币深度足够大且墙信号有意义。
        self.orderbook_monitor = HLOrderbookMonitor(
            list(cfg.markets), self.hl_ws, store=store,
            on_wall_signal=self._on_wall_signal)
        self.structure = StructureFeed(cfg.smc.swing_lookback, on_event=self._on_structure,
                                       on_closed=self._on_closed_candle)
        self.zones: dict[str, ZoneEngine] = {}   # 每 coin 一个 FVG/OB 引擎
        self.liquidity: dict[str, LiquidityEngine] = {}   # 每 coin 一个流动性引擎
        self._last_close: dict[str, float] = {}  # 每 coin 最近收盘价（入场参考）
        self._last_sweep: dict[str, tuple[str, int]] = {}  # coin -> (扫荡方向, ts)
        # 跟庄累积器：(庄地址,coin) -> [窗口内净建仓USD, 窗口起始ts, 上次发信号ts]
        self._whale_acc: dict[tuple[str, str], list[float]] = {}
        self._WHALE_WINDOW_MS = 180_000     # 3 分钟累积窗口
        self._WHALE_COOLDOWN_MS = 300_000   # 同庄同币 5 分钟冷却

        # ---- 信号引擎（融合 System1+System2）----
        self.signal_engine = SignalEngine(store=store, on_signal=self._on_signal,
                                          require_sweep=cfg.detection.require_sweep)
        self.divergence = DivergenceDetector(store=store, on_signal=self._on_divergence)
        self.consensus = WhaleConsensus(store=store, on_signal=self._on_consensus)
        self.pos_tracker = WhalePositionTracker(store=store, on_change=self._on_pos_change)
        self.confluence = ConfluenceAggregator(store, on_signal=self._on_confluence)
        self.correlation = AddressCorrelation(store)   # 地址协同(庄家集团)检测
        self._seen_clusters: set[tuple] = set()
        self.flow_predictor = FlowPredictor()          # 前瞻资金流预测(挂单意图+流加速度)
        self._last_coin_net: dict[str, float] = {}     # 上次采样的 per-coin 净流向
        self._flow_pred_seen: dict[str, int] = {}      # coin -> 上次前瞻预测 ts(冷却)
        self.pump_radar = PumpRadar()                  # 暴涨暴跌实时预警(历史验证规则)
        self.ta_signal = TASignal()                    # TA 多因子(指标+combo+PA+双顶双底+道氏)
        self.volume_monitor = VolumeMonitor(spike_mult=3.0)   # 放量监控
        self._candles: dict[str, list] = {}            # 每 coin 近 K 线缓冲
        self._pump_seen: dict[str, int] = {}           # coin -> 上次预警 ts(冷却)
        self._ta_seen: dict[str, int] = {}             # coin -> 上次 TA 信号 ts(冷却)
        self._wall_seen: dict[tuple[str, str], int] = {}  # (coin,side) -> 上次墙告警 ts(冷却)
        self._mids: dict[str, float] = {}     # 全市场中间价（allMids），共识估值用
        self.notifier = build_notifier(cfg)   # webhook + Telegram 多渠道推送
        self.analyst = build_analyst(cfg)     # LLM(Codex GPT-5.4) 前瞻研判，未配置则 None
        self.latency = LatencyTracker()       # 热路径「接收→信号」延迟埋点(实证低延迟)
        self.coin_to_symbol: dict[str, str] = {}              # canonical -> bitget symbol
        self.canon_to_hl = {normalize(c): c for c in meme_markets}  # canonical -> HL 币名

        # SMC 结构 + 信号的币种全集 = 主流币 + meme（meme 才有聪明钱流向/OI 共振）
        self.signal_universe = list(dict.fromkeys(cfg.markets + meme_markets))

        # ---- System 2: Bitget ----
        self.bg_ws = BitgetWSClient()
        self.oi_monitor: BitgetOIMonitor | None = None     # 启动时建（需符号映射）
        self.onchain = OnchainMemeMonitor(store, EVM_RPC,
                                          min_amount_usd=cfg.detection.large_fill_notional_usd)
        self.sol_monitor = SolanaSupplyMonitor(store)   # SOL meme 供应量(mint/burn)监控
        self._stopping = False
        self._bg_tasks: set[asyncio.Task] = set()       # 持有 _push 后台任务引用，防 GC
        self._okx_task: asyncio.Task | None = None      # OKX streaming 任务（仅 enabled 时创建）

        # ---- 钱包持仓画像管理器 ----
        self.wallet_portfolio = WalletPortfolio(store, cfg.hyperliquid.rest_url)

        # ---- 预测正确性回顾校准层 ----
        self.review = PredictionReview(store)

        # ---- 信号有效性自适应加权（meta-labeling 闭环）----
        self.efficacy = SignalEfficacy(store)
        # 将 efficacy 注入 confluence，使超级信号按各源历史命中率加权
        self.confluence.set_efficacy(self.efficacy)

        # ---- 系统健康监控（绑定 app，周期聚合 WS/延迟/内存/数据新鲜度）----
        self.health = HealthMonitor(self)

        # ---- 交易所链上资金流监控（BTC，keyless blockstream）----
        # 大额流入交易所=潜在抛压；净流出=吸筹。注册表为公开已知地址种子，可在 config 增补。
        self.exchange_flow: ExchangeFlowMonitor | None = None
        try:
            ew = root / "config" / "exchange_wallets.yaml"
            if ew.exists():
                ew_data = yaml.safe_load(ew.read_text(encoding="utf-8")) or {}
                registry = ew_data.get("exchanges") or {}
                evm_cfg  = ew_data.get("evm")          # EVM 稳定币流配置（可选）
                if registry:
                    self.exchange_flow = ExchangeFlowMonitor(
                        store, registry,
                        threshold_btc=getattr(cfg.detection, "exchange_flow_btc", 500.0),
                        evm_cfg=evm_cfg)
        except Exception as e:  # noqa: BLE001 — 资金流监控失败不影响主流程
            log.warning("交易所资金流注册表加载失败: %s", e)

    # ---- 价格标签 ----
    def _price_tag(self, coin: str) -> str:
        """返回形如 ' 💲$0.0835 🟢+0.36% 费率+0.0100%' 的实时价格+涨幅+资金费标签；无数据时返回空串。

        优先查 Bitget OI monitor（含 lastPr/change24h/funding），回退到 HL allMids（仅价格，无涨幅）。
        """
        px: float = 0.0
        chg: float | None = None
        funding: float | None = None

        # 1) 先查 Bitget（meme 永续，含 lastPr + change24h + funding）
        sym = self.coin_to_symbol.get(normalize(coin))
        if self.oi_monitor and sym:
            tk = self.oi_monitor.ticker(sym)
            if tk is not None:
                px = tk["price"]
                chg = tk["chg24"]
                funding = tk["funding"]

        # 2) 回退到 HL allMids（无涨幅/资金费数据）
        if px <= 0:
            px = self._mids.get(coin, 0.0)

        if px <= 0:
            return ""

        # 格式化价格
        px_str = _fmt_px(px)
        # 格式化涨跌幅
        if chg is not None:
            sign = "🟢+" if chg >= 0 else "🔴"
            chg_str = f" {sign}{chg * 100:.2f}%"
        else:
            chg_str = ""
        # 格式化资金费率（有值才追加）
        if funding is not None:
            funding_str = f" 费率{funding * 100:+.4f}%"
        else:
            funding_str = ""
        return f" 💲${px_str}{chg_str}{funding_str}"

    # ---- 回调 ----
    def _on_sm_event(self, evt: SmartMoneyEvent) -> None:
        big = evt.notional >= self.cfg.detection.large_fill_notional_usd
        if self.cfg.output.console:
            print(f"[{_hms(evt.time_ms)}] {'🔴' if big else '  '} {evt.fmt()}")
        self.store.insert_sm_event((
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
                msg = (f"[{_ts(evt.time_ms)}] 🐋跟庄信号 {evt.label or evt.address[:8]} "
                       f"净{d} {evt.coin} ${abs(net):,.0f}(3min累积) @ {evt.px:g}"
                       + self._price_tag(evt.coin)
                       + self.efficacy.label_of("跟庄"))
                print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
                self._push(msg)
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
        self._push(msg)

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
        self._push(msg)

    def _on_meme_trade(self, t: dict) -> None:
        # 大单 meme 成交（含主动方地址）。t 是 MemeTradeMonitor on_trade 传入的 record dict
        # （键 coin/taker_side/notional/taker，见 meme_trade_monitor.py:28,100-108），
        # 须按 dict 取键——此前误用属性访问导致 AttributeError 被回调 try/except 吞掉、告警静默失效。
        print(f"[{_hms()}] 🟡 [meme] {t['coin']} {'买' if t['taker_side']=='B' else '卖'} "
              f"${t['notional']:,.0f} taker={t['taker'][:12]}…")

    def _on_structure(self, coin: str, e: StructureEvent) -> None:
        arrow = "↑" if e.direction == "bull" else "↓"
        print(f"[{_hms()}] 📐 [SMC] {coin} {e.type} {arrow} 突破 {e.level:g} "
              f"(trend→{self.structure.structure(coin).trend})")
        # 结构事件 = 信号触发点：先刷新该 coin 的聪明钱流向 + OI 环境，再评估共振
        now = int(time.time() * 1000)
        self.signal_engine.set_flow(coin, self.meme_monitor.coin_net(coin))
        symbol = self.coin_to_symbol.get(normalize(coin))
        if symbol:
            chg = self.store.oi_change(symbol, window_ms=600_000, now_ms=now)
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
        # 维护近 K 线缓冲 → 暴涨暴跌实时预警
        buf = self._candles.setdefault(coin, [])
        buf.append(candle)
        if len(buf) > 400:
            del buf[:100]
        now = int(time.time() * 1000)
        if now - self._pump_seen.get(coin, 0) >= 1_800_000:   # 30min 冷却
            alert = self.pump_radar.evaluate(coin, buf, now)
            if alert is not None:
                self._pump_seen[coin] = now
                ctx = fmt_analysis(coin, ta_analyze(buf, now))   # 附 TA 全景上下文
                msg = f"[{_ts(now)}] {alert.fmt()}{self._price_tag(coin)}\n{ctx}"
                print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
                self._push(msg)
                self._record_pred(coin, "暴涨", "up" if alert.kind == "pump" else "down")
        # 放量监控(成交量异动)
        vev = self.volume_monitor.update(coin, candle)
        if vev is not None:
            print(f"[{_hms(now)}] 📊 [放量] {coin} {vev['ratio']:.1f}× 均量 (量={vev['vol']:g})")
        # TA 多因子信号(指标+combo+PA+双顶双底+道氏 全链路在生产执行)
        if len(buf) >= 60 and now - self._ta_seen.get(coin, 0) >= 1_800_000:
            sig = self.ta_signal.evaluate(buf, None, now)
            if sig is not None:
                self._ta_seen[coin] = now
                print(f"[{_ts(now)}] 📐 {self.ta_signal.fmt(sig)}{self._price_tag(coin)}")
                self._push(f"[{_ts(now)}] {self.ta_signal.fmt(sig)}{self._price_tag(coin)}")
        ze = self.zones.get(coin)
        if ze is None:
            ze = ZoneEngine(min_gap_pct=self.cfg.smc.fvg_min_gap_pct)
            self.zones[coin] = ze
        for z in ze.update(candle):
            tag = "看涨" if z.direction == "bull" else "看跌"
            print(f"[{_hms()}] 🟦 [{z.kind}] {coin} {tag} 区 [{z.bottom:g}, {z.top:g}]")
        # 流动性扫荡
        le = self.liquidity.get(coin)
        if le is None:
            le = LiquidityEngine(lookback=self.cfg.smc.swing_lookback)
            self.liquidity[coin] = le
        for sw in le.update(candle):
            self._last_sweep[coin] = (sw.direction, candle.close_time_ms)
            tag = "看涨(扫SSL)" if sw.direction == "bullish" else "看跌(扫BSL)"
            eq = "等高等低" if sw.equal else ""
            print(f"[{_hms()}] 💧 [扫荡] {coin} {tag} @ {sw.price:g} {eq}")

    def _record_pred(
        self, coin: str, kind: str, direction: str, horizon_ms: int | None = None
    ) -> None:
        """记录前瞻预测到回顾层，统一 MTF 多水平线落库（发推后立即调用）。

        读 cfg.review.horizons_min 转 ms 批量记录 7 个 TF（5m/15m/30m/1h/4h/12h/1d）。
        horizon_ms 参数保留向后兼容签名，但统一走 MTF（忽略显式单值，保持所有信号源一致性）。
        hl_px：从 self._mids 取；bg_px：从 Bitget OI 监控取价格（price_change()[0]）。
        任何失败不影响主推送热路径。
        """
        try:
            hl = _f(self._mids.get(coin, 0.0))
            bg = 0.0
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

    def _push(self, text: str) -> None:
        """非阻塞推送 webhook（已配置才发）。持有 task 引用防其在完成前被 GC 取消。"""
        if self.notifier.enabled:
            t = asyncio.create_task(self.notifier.send(text, int(time.time() * 1000)))
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)

    def _on_signal(self, sig: Signal) -> None:
        msg = f"[{_ts(sig.ts)}] {sig.fmt()}" + self.efficacy.label_of("SMC")
        print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
        self._push(msg)
        self._record_pred(sig.coin, "SMC", sig.direction)   # 进回顾闭环(事后评估命中率→efficacy 加权)

    def _on_divergence(self, sig: DivergenceSignal) -> None:
        msg = (f"[{_ts(sig.ts)}] {sig.fmt()}{self._price_tag(sig.coin)}"
               + self.efficacy.label_of("背离"))
        print(msg)
        self._push(msg)
        self._record_pred(
            sig.coin, "背离", "up" if sig.direction == "bullish" else "down"
        )

    def _on_consensus(self, sig: ConsensusSignal) -> None:
        msg = (f"[{_ts(sig.ts)}] {sig.fmt()}{self._price_tag(sig.coin)}"
               + self.efficacy.label_of("共识"))
        print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
        self._push(msg)
        self._record_pred(sig.coin, "共识", sig.direction)

    def _on_pos_change(self, pc: PositionChange) -> None:
        msg = f"[{_ts(pc.ts)}] {pc.fmt()}"
        print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
        self._push(msg)

    def _on_confluence(self, sig: ConfluenceSignal) -> None:
        msg = f"[{_ts(sig.ts)}] {sig.fmt()}" + self.efficacy.label_of("超级")
        print(f"\n{'🌟'*30}\n{msg}\n{'🌟'*30}\n")
        self._push(msg)
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

    def _on_oi_surge(self, symbol: str, prev: float, cur: float) -> None:
        pct = (cur - prev) / prev * 100 if prev else 0
        print(f"[{_hms()}] 📊 [OI异动] {symbol} {prev:,.0f}→{cur:,.0f} ({pct:+.2f}%)")

    # ---- 启动播种 ----
    async def _seed(self) -> None:
        now = int(time.time() * 1000)
        ms = _INTERVAL_MS.get(self.cfg.smc.candle_interval, 300_000)
        # 0) 从 watched_wallets 注册表接力恢复上次观察的钱包（重启不丢）
        try:
            loaded = self.store.load_wallets()
            if loaded:
                existing_addrs = {w.address.lower() for w in self.cfg.watchlist}
                for row in loaded:
                    # row: (address,label,source,first_seen_ms,last_seen_ms,
                    #        account_value,total_ntl_pos,n_positions)
                    addr, label = row[0], row[1] or ""
                    if addr.lower() not in existing_addrs:
                        from .config import WatchAddress as _WA
                        wa = _WA(addr, label)
                        self.cfg.watchlist.append(wa)
                        existing_addrs.add(addr.lower())
                self.address_monitor.add_addresses(self.cfg.watchlist)
                log.info("从注册表接力 %d 个观察钱包", len(loaded))
        except Exception as e:  # noqa: BLE001
            log.warning("接力观察钱包失败: %s", e)
        # 1) watchlist 为空 → 自动从排行榜发现聪明钱(庄)地址，纳入监控
        if not self.cfg.watchlist:
            try:
                whales = await discover_smart_money(top_n=15)
                self.address_monitor.add_addresses(whales)
                self.cfg.watchlist = whales
                log.info("自动发现并监控聪明钱(庄) %d 个：%s", len(whales),
                         ", ".join(w.label for w in whales[:5]) + " …")
                # 落库新发现的地址
                for w in whales:
                    self.store.upsert_wallet(w.address, w.label, "discover", now)
            except Exception as e:  # noqa: BLE001
                log.warning("聪明钱发现失败（继续无 watchlist 运行）: %s", e)
        else:
            # watchlist 非空时也把当前地址落库（接力保障）
            for w in self.cfg.watchlist:
                self.store.upsert_wallet(w.address, w.label, "discover", now)
        async with HyperliquidInfo(self.cfg.hyperliquid.rest_url) as info:
            # 1) watchlist 真实持仓播种
            for w in self.cfg.watchlist:
                try:
                    pos = await info.positions(w.address)
                    self.address_monitor.seed_positions(w.address, {p.coin: p.szi for p in pos})
                except Exception as e:  # noqa: BLE001
                    log.warning("播种持仓失败 %s: %s", w.address[:10], e)
            # 2) SMC K 线历史播种（主流币 + meme，并发，限流到 6 并发）
            start = now - ms * self.cfg.smc.history_bars
            sem = asyncio.Semaphore(6)
            async def seed_one(coin: str) -> int:
                async with sem:
                    try:
                        candles = await info.candle_snapshot(
                            coin, self.cfg.smc.candle_interval, start, now)
                        self.structure.seed(coin, candles)
                        ze = ZoneEngine(min_gap_pct=self.cfg.smc.fvg_min_gap_pct)
                        le = LiquidityEngine(lookback=self.cfg.smc.swing_lookback)
                        for cd in candles:
                            ze.update(cd)
                            le.update(cd)
                        self.zones[coin] = ze
                        self.liquidity[coin] = le
                        self._candles[coin] = list(candles)   # 暴涨暴跌预警缓冲
                        return len(candles)
                    except Exception as e:  # noqa: BLE001
                        log.warning("K线播种失败 %s: %s", coin, e)
                        return 0
            seeded = await asyncio.gather(*(seed_one(c) for c in self.signal_universe))
            log.info("SMC 播种完成 %d 个币（共 %d 根 %s K线）",
                     len(seeded), sum(seeded), self.cfg.smc.candle_interval)

        # 3) Bitget 符号映射 + meme 合约地址（若缺）
        canon = {normalize(c) for c in self.meme_markets}
        async with BitgetREST() as bg:
            base_map = await bg.perp_base_coins()
            symbol_to_coin: dict[str, str] = {}
            for symbol, base in base_map.items():
                n = normalize(base)
                if n in canon and n not in {normalize(s) for s in symbol_to_coin}:
                    symbol_to_coin[symbol] = n
            self.oi_monitor = BitgetOIMonitor(
                list(symbol_to_coin), symbol_to_coin, self.bg_ws, self.store,
                surge_pct=0.03, on_surge=self._on_oi_surge)
            # canonical -> bitget symbol（信号引擎查 OI 用）
            self.coin_to_symbol = {coin: sym for sym, coin in symbol_to_coin.items()}
            if self.store.count("meme_contracts") == 0:
                all_chains = await bg.all_coin_chains()
                for n in canon:
                    for chain, addr in all_chains.get(n.upper(), []):
                        self.store.upsert_contract(n, chain, addr, now)
                log.info("已补 meme 合约地址 %d 条", self.store.count("meme_contracts"))

    # ---- 周期任务 ----
    async def _periodic_flush(self, every: float = 5.0) -> None:
        while not self._stopping:
            await asyncio.sleep(every)
            self.meme_monitor.flush()
            self.orderbook_monitor.flush()   # 挂单墙事件批量落库（dashboard 消费）
            if self.oi_monitor:
                self.oi_monitor.flush()

    # DB 时间序列保留策略（窗口远大于功能回看，保守删除）
    # 格式：(表名, 时间列, 保留毫秒)
    # predictions：MTF ×7 增长，保留 90 天以覆盖全水平线评估闭环（诚实复盘基石）
    _DB_RETAIN: list[tuple[str, str, int]] = [
        ("bitget_oi",            "ts",      7  * 86_400_000),
        ("hl_meme_trades",       "time_ms", 7  * 86_400_000),
        ("sm_events",            "ts",      30 * 86_400_000),
        ("signals",              "ts",      30 * 86_400_000),
        ("divergence",           "ts",      30 * 86_400_000),
        ("consensus",            "ts",      30 * 86_400_000),
        ("confluence_signals",   "ts",      30 * 86_400_000),
        ("whale_signals",        "ts",      30 * 86_400_000),
        ("position_changes",     "ts",      30 * 86_400_000),
        ("wallet_positions_full","ts",       3 * 86_400_000),
        ("whale_pnl_snapshots",  "ts",      30 * 86_400_000),
        ("flow_predictions",     "ts",      30 * 86_400_000),
        ("predictions",          "ts",      90 * 86_400_000),
    ]

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
        """前瞻资金流预测：采样净流向加速度 + 订单簿挂单意图 + OI 速度 → 预测方向。"""
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
            ranked = sorted(self.meme_markets,
                            key=lambda c: abs(self.flow_predictor.flow_acceleration(c, now)),
                            reverse=True)[:3]
            try:
                async with HyperliquidInfo(self.cfg.hyperliquid.rest_url) as info:
                    for coin in ranked:
                        book_imb = 0.0
                        try:
                            l2 = await info._post({"type": "l2Book", "coin": coin})
                            lv = l2.get("levels") or [[], []]
                            book_imb = orderbook_imbalance(lv[0], lv[1])["imbalance"]
                        except Exception:  # noqa: BLE001
                            pass
                        oi_vel = 0.0
                        sym = self.coin_to_symbol.get(normalize(coin))
                        if sym:
                            chg = self.store.oi_change(sym, 600_000, now)
                            if chg and chg[1]:
                                oi_vel = (chg[0] - chg[1]) / chg[1]
                        pred = self.flow_predictor.predict(coin, now, book_imb, oi_vel)
                        if pred and now - self._flow_pred_seen.get(coin, 0) >= 600_000:
                            self._flow_pred_seen[coin] = now
                            msg = f"[{_ts(now)}] {pred.fmt()}{self._price_tag(coin)}"
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
            await asyncio.sleep(every)
            now = int(time.time() * 1000)
            try:
                # min_coins=2：要求跨≥2 个不同币协同——跨市场协同是同一实体的硬证据，
                # 且避免单币重叠把追涨人群污染合并成大团(高精确度路线)。
                groups = self.correlation.clusters_detailed(
                    now - 1_800_000, window_sec=120, min_shared=3, min_coins=2)
            except Exception as e:  # noqa: BLE001
                log.warning("关联扫描失败: %s", e)
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
                rel = self.correlation.correlated_with(g[0], now - 1_800_000, min_shared=2)
                rel_s = (" 核心" + g[0][:8] + "…最相关:"
                         + ",".join(f"{a[:8]}…×{c}" for a, c in rel[:3])) if rel else ""
                strength = f"跨{d['coins']}币·协同{d['events']}次·{d['links']}对"
                # lead-lag：识别群内谁先动（核心 leader），供跟庄前瞻决策
                try:
                    leader_info = self.correlation.cluster_leader(
                        g, now - 1_800_000, window_sec=120)
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

    async def _periodic_report(self, every: float = 3600.0) -> None:
        """周期摘要日报：控制台打印 + webhook 推送。"""
        while not self._stopping:
            await asyncio.sleep(every)
            now = int(time.time() * 1000)
            report = build_report(self.store, now - int(every * 1000), now)
            lat = self.latency.fmt()              # 热路径延迟实测(P50/P99/max)
            if lat:
                report += f"\n\n⏱️ 热路径延迟(实测):\n{lat}"
            print(f"\n{report}\n")
            self._push(report)

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
        """多庄共识扫描 + 庄持仓面板（谁在多/空什么）。"""
        while not self._stopping:
            await asyncio.sleep(every)
            positions = self.address_monitor.all_positions()
            if not positions or not self._mids:
                continue
            labels = {a: self.address_monitor.label_of(a) for (a, _c) in positions}
            now = int(time.time() * 1000)
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
            await asyncio.sleep(every)
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

    async def _periodic_efficacy(self, every: float = 1800.0) -> None:
        """信号有效性自适应加权刷新：定期从 predictions 表读取历史评估，更新各 kind 权重。

        仅当任一 kind 达到 min_sample 才推送（避免空推/噪声）；失败不影响监控热路径。
        """
        while not self._stopping:
            await asyncio.sleep(every)
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

    # ---- 配置热加载 ----

    def _apply_config(self, new_cfg: Config) -> list[str]:
        """把新配置应用到运行时对象，返回变更字段描述列表。

        仅修改可热更字段：阈值/开关/推送渠道/LLM；不重启 WS 连接或重播种。
        """
        changes = diff_config(self.cfg, new_cfg)
        if not changes:
            return []

        det = new_cfg.detection
        # 检测阈值
        self.cfg.detection.large_fill_notional_usd = det.large_fill_notional_usd
        self.address_monitor.large_fill_notional_usd = det.large_fill_notional_usd
        self.meme_monitor.large_notional_usd = det.large_fill_notional_usd
        self.meme_monitor.suspicious_notional = det.large_fill_notional_usd * 2
        # 信号门槛
        self.signal_engine.require_sweep = det.require_sweep
        self.cfg.detection.require_sweep = det.require_sweep
        # 换仓百分比（仅写 cfg，position_tracker 从 cfg 引用）
        self.cfg.detection.position_change_pct = det.position_change_pct
        # 控制台开关
        self.cfg.output.console = new_cfg.output.console
        # 推送渠道变化 → 重建 notifier/analyst
        old_webhook = self.cfg.output.webhook_url
        old_tg_token = self.cfg.telegram.bot_token
        old_tg_chat = self.cfg.telegram.chat_id
        if (new_cfg.output.webhook_url != old_webhook
                or new_cfg.telegram.bot_token != old_tg_token
                or new_cfg.telegram.chat_id != old_tg_chat):
            self.cfg.output.webhook_url = new_cfg.output.webhook_url
            self.cfg.telegram.bot_token = new_cfg.telegram.bot_token
            self.cfg.telegram.chat_id = new_cfg.telegram.chat_id
            self.notifier = build_notifier(new_cfg)
        # LLM 变化 → 重建 analyst
        old_llm_enabled = self.cfg.llm.enabled
        old_llm_model = self.cfg.llm.model
        old_llm_interval = self.cfg.llm.interval_sec
        if (new_cfg.llm.enabled != old_llm_enabled
                or new_cfg.llm.model != old_llm_model
                or new_cfg.llm.interval_sec != old_llm_interval):
            self.cfg.llm.enabled = new_cfg.llm.enabled
            self.cfg.llm.model = new_cfg.llm.model
            self.cfg.llm.interval_sec = new_cfg.llm.interval_sec
            self.analyst = build_analyst(new_cfg)

        # watchlist 新增地址 → 运行时订阅(热加载即时追踪)；移除不退订(保留累计仓位/流向状态)。
        # 持仓由订阅后 webData2 snapshot 自动播种，无需在此同步发 REST(不阻塞热路径)。
        old_wl = {w.address.lower() for w in self.cfg.watchlist}
        now_ms = int(time.time() * 1000)
        for w in new_cfg.watchlist:
            if w.address.lower() not in old_wl and self.address_monitor.subscribe_address(w):
                self.store.upsert_wallet(w.address, w.label, "manual", now_ms)
                log.info("watchlist 热加载新增并订阅 %s %s", w.address[:10], w.label)

        # 最终同步 cfg 引用（使新 cfg 对象其余字段也可用）
        self.cfg = new_cfg
        return changes

    def _reload_config(self) -> None:
        """从 _cfg_path 重新加载配置并热应用；失败仅 warning，不崩溃。"""
        if not self._cfg_path or not Path(self._cfg_path).exists():
            return
        try:
            new_cfg = Config.load(self._cfg_path)
            changes = self._apply_config(new_cfg)
            if changes:
                msg = "🔧 配置热加载:\n" + "\n".join(changes)
                print(msg)
                self._push(msg)
            else:
                log.debug("配置热加载：无变更")
        except Exception as exc:  # noqa: BLE001 — 配置读取失败不崩溃
            log.warning("配置热加载失败: %s", exc)

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
            await asyncio.sleep(every)
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

    async def _periodic_ticker_board(self, every: float = 300.0) -> None:
        """周期推送行情监控板：显示所有监控币种的价格/涨跌幅/资金费率/OI（每 5 分钟）。

        风格参考 BWE_OI_Price_monitor 频道：涨跌幅最大的排前，每次推 Top 20。
        oi_monitor 为 None 时跳过；无行情数据时不推送（不空推）。
        """
        while not self._stopping:
            await asyncio.sleep(every)
            if self.oi_monitor is None:
                continue
            rows = self.oi_monitor.board_rows()[:20]
            if not rows:
                continue
            now = int(time.time() * 1000)
            lines: list[str] = [f"📊 行情监控板 [{_ts(now)}]"]
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
            await asyncio.sleep(every)
            if not self.cfg.watchlist:
                continue
            now = int(time.time() * 1000)
            try:
                snaps = await self.wallet_portfolio.refresh(self.cfg.watchlist, now)
                for snap in snaps:
                    fmt = self.wallet_portfolio.fmt(snap)
                    print(fmt)
                    self._push(fmt)
            except Exception as e:  # noqa: BLE001
                log.warning("钱包持仓画像刷新失败: %s", e)

    async def _periodic_exchange_flow(self, every: float = 3600.0) -> None:
        """交易所链上资金流监控（BTC，keyless blockstream）：每小时核对各所 24h 净流入/流出。

        大额净流入交易所 → 潜在抛压；净流出 → 吸筹。仅对越阈值（threshold_btc）的推送告警。
        """
        if self.exchange_flow is None:
            return
        while not self._stopping:
            await asyncio.sleep(every)
            now = int(time.time() * 1000)
            try:
                rows = await self.exchange_flow.poll_once(now)
            except Exception as e:  # noqa: BLE001 — 资金流轮询失败不影响监控
                log.warning("交易所资金流轮询失败: %s", e)
                continue
            for row in rows:
                arrow = "净流入🔴" if row["net"] >= 0 else "净流出🟢"
                print(f"[{_hms(now)}] 🏦 [交易所流] {row['exchange']} {row['chain']} "
                      f"{arrow} {abs(row['net']):,.0f} BTC "
                      f"(入{row['inflow']:,.0f}/出{row['outflow']:,.0f})")
                if row.get("alert"):
                    self._push(f"[{_ts(now)}] {fmt_flow_alert(row)}")

    async def run(self) -> None:
        await self._seed()
        # 挂载 System 1
        self.address_monitor.attach()
        self.meme_monitor.attach()
        self.orderbook_monitor.attach()   # l2Book 挂单墙动态（主流币领先意图）
        self.hl_ws.subscribe(Subscription(type="allMids"), self._on_all_mids)
        for coin in self.signal_universe:
            self.hl_ws.subscribe(
                Subscription(type="candle", coin=coin, interval=self.cfg.smc.candle_interval),
                self._on_candle_ws)          # 带延迟埋点的统一入口(handler 内按 data[s] 区分 coin)
        # 挂载 System 2
        if self.oi_monitor:
            self.oi_monitor.attach()
        # 挂载 System 3（OKX，默认 enabled=False，不影响现有路径）
        if self.cfg.okx.enabled:
            from .okx.stream import run_okx_streaming  # noqa: PLC0415
            self._okx_task = asyncio.create_task(
                run_okx_streaming(self.store, self.cfg.okx),
                name="okx_streaming",
            )
            log.info("OKX streaming 已启动 (top_n=%d)", self.cfg.okx.top_n)
        log.info("双所系统启动：watchlist=%d meme=%d markets=%s",
                 len(self.cfg.watchlist), len(self.meme_markets), self.cfg.markets)
        await asyncio.gather(
            self.hl_ws.run(),
            self.bg_ws.run(),
            self._periodic_flush(),
            self._periodic_onchain(),
            self._periodic_solana(),
            self._periodic_divergence(),
            self._periodic_consensus(),
            self._periodic_correlation(),
            self._periodic_flow_predict(),
            self._periodic_report(),
            self._periodic_llm(),
            self._periodic_cleanup(),
            self._periodic_review(),
            self._periodic_efficacy(),
            self._periodic_health(),
            self._periodic_ticker_board(),
            self._periodic_exchange_flow(),
            self._periodic_wallet_portfolio(),
            self._periodic_config_reload(),
        )

    async def stop(self) -> None:
        self._stopping = True
        self.meme_monitor.flush()
        self.orderbook_monitor.flush()   # 退出前冲刷剩余挂单墙事件
        if self.oi_monitor:
            self.oi_monitor.flush()
        # OKX streaming 任务：优雅取消（仅 enabled=True 时存在）
        if self._okx_task is not None and not self._okx_task.done():
            self._okx_task.cancel()
        await self.hl_ws.stop()
        await self.bg_ws.stop()


async def _amain(cfg_path: str) -> None:
    root = Path(__file__).resolve().parents[2]
    cfg = Config.load(cfg_path) if Path(cfg_path).exists() else Config()
    meme = load_meme_markets(root)
    store = Store(root / "data" / "smc.db")
    # 传入 cfg_path 以支持配置热加载（mtime 看门狗 + SIGHUP）
    app = TradingSystem(cfg, meme, store, root, cfg_path=cfg_path)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(app.stop()))
    # SIGHUP：Unix 传统「热加载配置」信号；Windows 无此信号，守卫跳过
    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(signal.SIGHUP, app._reload_config)
    try:
        await app.run()
    finally:
        store.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="SMC 双所聪明钱追踪系统")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(_amain(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
