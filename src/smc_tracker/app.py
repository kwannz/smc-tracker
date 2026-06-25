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
from typing import Any

import yaml

from .bitget import BitgetREST, BitgetSub, BitgetWSClient
from .config import Config, WatchAddress, diff_config
from .indicators import VolumeMonitor, analyze as ta_analyze, fmt_analysis
from .hyperliquid import HyperliquidInfo, HyperliquidWSClient, Subscription
from .llm import build_analyst
from .memecoins import normalize
from .monitor import (AddressMonitor, BitgetOIMonitor, EventType, HLOrderbookMonitor,
                      MemeTradeMonitor, SmartMoneyEvent)
from .monitor.candle_ingest import detect_and_fill_gap as _detect_and_fill_gap  # A2：统一缺口检测接线
from .monitor.whale_discovery import discover_smart_money, fetch_leaderboard_rows
from .monitor.address_correlation import AddressCorrelation
from .monitor.whale_momentum import WhaleMomentum, pnl_rows_from
from .monitor.wallet_portfolio import WalletPortfolio
from .notify import HLDigest, build_notifier, build_report
from .onchain import (ExchangeFlowMonitor, OnchainMemeMonitor, SolanaSupplyMonitor,
                      fmt_flow_alert)
from .health import HealthMonitor
from .perf import LatencyTracker
from .review import PredictionReview, fmt_accuracy
from .signals import (ConfluenceAggregator, ConfluenceSignal, ConsensusSignal,
                      DivergenceDetector, DivergenceSignal, FlowPredictor, PositionChange,
                      PumpRadar, Signal, SignalEngine, SignalEfficacy, TASignal, WhaleConsensus,
                      WhalePositionTracker, orderbook_imbalance, positioning,
                      SetupDedup, oi_directional_velocity)
from .signals.harmonic_review import build_harmonic_predictions
from .smc import LiquidityEngine, StructureEvent, StructureFeed, ZoneEngine
from .storage import Store
from .supervisor import supervise  # A3：per-task 指数退避重启监督

log = logging.getLogger("app")

from .util import fmt_hms as _hms          # 简洁 HH:MM:SS（高频控制台行）
from .util import fmt_ts as _ts            # 完整 日期+时间+时区（推送告警，便于事后回顾）
from .util import to_float as _f           # 统一安全数值解析
from .util import fmt_px as _fmt_px        # 统一价格格式（非科学计数法完整数字，见 util.fmt_px）
from .util import is_placeholder_addr      # 占位/零地址判别（跳过示例配置残留地址）

_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


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
        self.correlation = AddressCorrelation(store, cfg=cfg.correlation)  # 地址协同(庄家集团)检测
        self._seen_clusters: set[tuple] = set()
        self.flow_predictor = FlowPredictor()          # 前瞻资金流预测(挂单意图+流加速度)
        self._harmonic_dedup = SetupDedup()            # 谐波 completed 进 review 闭环的结构指纹去重
        self._last_coin_net: dict[str, float] = {}     # 上次采样的 per-coin 净流向
        self._flow_pred_seen: dict[str, int] = {}      # coin -> 上次前瞻预测 ts(冷却)
        self.pump_radar = PumpRadar()                  # 暴涨暴跌实时预警(历史验证规则)
        self.ta_signal = TASignal()                    # TA 多因子(指标+combo+PA+双顶双底+道氏)
        self.volume_monitor = VolumeMonitor(spike_mult=3.0)   # 放量监控
        self._candles: dict[str, list] = {}            # 每 coin 近 K 线缓冲
        self._pump_seen: dict[str, int] = {}           # coin -> 上次预警 ts(冷却)
        self._ta_seen: dict[str, int] = {}             # coin -> 上次 TA 信号 ts(冷却)
        self._wall_seen: dict[tuple[str, str], int] = {}  # (coin,side) -> 上次墙告警 ts(冷却)
        self._div_seen: dict[tuple[str, str], int] = {}   # (coin,direction) -> 上次背离 ts(冷却,降噪)
        self._DIV_COOLDOWN_MS = 900_000     # 同币同向背离 15 分钟冷却(避免每60s重复+高自相关预测)
        self._mids: dict[str, float] = {}     # 全市场中间价（allMids），共识估值用
        self.notifier = build_notifier(cfg)   # webhook + Telegram 多渠道推送（HL 主通道）
        # 谐波系统**专用独立飞书**（用户#：与 HL 信号分开推送）；未配置则回退主 notifier
        from .notify.feishu import FeishuNotifier  # noqa: PLC0415
        from .notify.multi import MultiNotifier    # noqa: PLC0415
        _hfs = FeishuNotifier(cfg.harmonic.feishu_webhook, cfg.harmonic.feishu_secret)
        self.harmonic_notifier = MultiNotifier([_hfs]) if _hfs.enabled else self.notifier
        # HL 事件分类汇总：零散事件按分类聚合，_periodic_hl_digest 周期推一张汇总卡片（降噪去刷屏）
        self.hl_digest = HLDigest(cfg.digest.max_per_cat)
        # 推送串行队列：(text, notifier) 入队，单 worker 按最小间隔逐条发到指定 notifier（防限流丢卡 + 多通道路由）
        # A4：maxsize=2000 背压，爆发推送有界不 OOM（drain worker 37.5条/min，正常远不触发）
        self._push_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=2000)
        # A4：丢最旧计数器（满时弃头，不静默——log+计数可见）
        self._push_dropped: int = 0
        self.analyst = build_analyst(cfg)     # LLM(Codex GPT-5.4) 前瞻研判，未配置则 None
        self.latency = LatencyTracker()       # 热路径「接收→信号」延迟埋点(实证低延迟)
        self.coin_to_symbol: dict[str, str] = {}              # canonical -> bitget symbol
        self.canon_to_hl = {normalize(c): c for c in meme_markets}  # canonical -> HL 币名
        self._collect_offset: int = 0   # K 线批量轮转偏移（collect_batch 环绕用）

        # SMC 结构 + 信号的币种全集 = 主流币 + meme（meme 才有聪明钱流向/OI 共振）
        self.signal_universe = list(dict.fromkeys(cfg.markets + meme_markets))

        # ---- System 2: Bitget ----
        self.bg_ws = BitgetWSClient()
        self.oi_monitor: BitgetOIMonitor | None = None     # 启动时建（需符号映射）
        self.bb_monitor = None        # BitgetBBMonitor，_seed 后按配置构建（需 tickers 成交额）
        self.harmonic_monitor = None  # HarmonicMonitor，_seed 后按配置构建
        self.harmonic_candle_ws = None  # HarmonicCandleWS，_seed 后按配置构建（B1：K线 WS 增量实时）
        self.candle_collector = None  # BitgetCandleCollector，_seed 后构建（拉 K 线落 DB 供两板共用）
        self.onchain = OnchainMemeMonitor(store, EVM_RPC,
                                          min_amount_usd=cfg.detection.large_fill_notional_usd)
        self.sol_monitor = SolanaSupplyMonitor(store)   # SOL meme 供应量(mint/burn)监控
        self._stopping = False
        self._bg_tasks: set[asyncio.Task] = set()       # 持有 _push 后台任务引用，防 GC
        self._okx_task: asyncio.Task | None = None      # OKX streaming 任务（仅 enabled 时创建）
        # A2a：sm_events 热路径缓冲（WS 回调 append，_periodic_flush 批量落，不阻塞 event loop）
        self._sm_buffer: list[tuple] = []

        # ---- 钱包持仓画像管理器 ----
        self.wallet_portfolio = WalletPortfolio(store, cfg.hyperliquid.rest_url)

        # ---- 庄 PnL 动量快照写手（streaming 接入，解决 whale_pnl_snapshots 永空）----
        self.whale_momentum = WhaleMomentum(store)

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
                msg = (f"[{_ts(now)}] {alert.fmt()}{self._price_tag(coin)}"
                       + self.efficacy.label_of("暴涨")
                       + f"\n{ctx}")
                print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
                self._emit("pump", msg)
                self._record_pred(coin, "暴涨", "up" if alert.kind == "pump" else "down")
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
        msg = (f"[{_ts(sig.ts)}] {sig.fmt()}{self._price_tag(sig.coin)}"
               + self.efficacy.label_of("背离"))
        print(msg)
        self._emit("divergence", msg)
        self._record_pred(
            sig.coin, "背离", "up" if sig.direction == "bullish" else "down"
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

        # 3) Bitget 符号映射 + meme 合约地址（若缺）+ 布林带监控器
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

            # ---- 按 tickers 24h 成交额排序选币（BB + 谐波**共用**，修复谐波曾误用 OI 插入序）----
            # 使用 resolve_universe 统一选币（配置化，支持 all/top_n/list + asset_filter/exclude）
            vol_c2s: dict[str, str] = {}   # coin -> symbol，按成交额降序（全市场，所有模式基础集）
            if self.cfg.bollinger.enabled or self.cfg.harmonic.enabled:
                try:
                    tickers_map = await bg.tickers()
                    # P0 守卫：成交额全 0 → 字段可能变更，选币退化为插入序，告警不静默失真
                    vols = [_f(tk.get("quoteVolume") or tk.get("usdtVolume") or 0)
                            for tk in tickers_map.values()]
                    if vols and all(v <= 0 for v in vols):
                        log.warning("选币：tickers 成交额全为 0（quoteVolume 字段可能已变更），退化插入序")
                    # resolve_universe：统一选币（配置化，支持 mode/asset_filter/exclude）
                    from .config import resolve_universe, resolve_monitored_universe  # noqa: PLC0415
                    if self.cfg.monitored_coins.enabled:
                        # 监控清单模式（watchlist-multi-tf）：只采清单内币，替换 all_perp/top_n
                        _monitored = self.store.get_monitored_coins()
                        if not _monitored:
                            log.warning("监控清单为空(monitored_coins.enabled=true)，本轮不纳入任何币；"
                                        "用 `watch add` 或 dashboard 添加")
                        vol_c2s = resolve_monitored_universe(_monitored, base_map, tickers_map)
                    else:
                        vol_c2s = resolve_universe(base_map, tickers_map, self.cfg.universe)
                    # 无 universe 配置（默认 top_n=12）时行为不变；mode=all 则返回全部符合条件的币
                except Exception as exc:  # noqa: BLE001
                    log.warning("选币 tickers 拉取失败（不影响主流程）: %s", exc)

            # ---- 布林带监控器 ----
            if self.cfg.bollinger.enabled and (vol_c2s or self.cfg.monitored_coins.enabled):
                from .monitor.bitget_bb_monitor import BitgetBBMonitor  # noqa: PLC0415
                bb_n = self.cfg.bollinger.top_n
                # resolve_universe 已按成交额排序；bb_n 控制监控窗口上限（universe 可能含更多币）
                bb_c2s = dict(list(vol_c2s.items())[:bb_n])
                self.bb_monitor = BitgetBBMonitor(
                    coin_to_symbol=bb_c2s,
                    timeframes=self.cfg.bollinger.timeframes,
                    bars=self.cfg.bollinger.bars,
                    period=self.cfg.bollinger.period,
                    k=self.cfg.bollinger.k,
                    top_n=bb_n,
                    store=self.store,   # 优先读 DB K线缓存，不足回退 live（减 API/跨重启持久）
                )
                log.info("BB 监控器已建，top_%d 币: %s", bb_n, list(bb_c2s.keys())[:6])

            # ---- 谐波形态监控器（同一成交额序选币，与 BB 口径一致）----
            if self.cfg.harmonic.enabled and (vol_c2s or self.cfg.monitored_coins.enabled):
                from .monitor.harmonic_monitor import HarmonicMonitor  # noqa: PLC0415
                from .monitor.harmonic_forward import HarmonicForwardSignals  # noqa: PLC0415
                from .monitor.bitget_trade_monitor import BitgetTradeMonitor  # noqa: PLC0415
                from .monitor.forming_approach import FormingApproachTracker  # noqa: PLC0415
                harm_n = self.cfg.harmonic.top_n
                harm_umode = self.cfg.harmonic.universe_mode

                if self.cfg.monitored_coins.enabled:
                    # 监控清单模式：谐波宇宙=清单(=vol_c2s)，不再 all_perp/top_n，也不并 harmonic_collected
                    harm_c2s = dict(vol_c2s)
                    log.info("谐波 monitored_coins 模式：纳入清单 %d 币", len(harm_c2s))
                elif harm_umode == "all_perp":
                    # all_perp 模式：用全部 USDT 永续合约，按 24h 成交额降序排序（高 vol 优先）
                    # 直接从 base_map + tickers_map 构建，不受全局 universe 配置 top_n 限制
                    from .config import resolve_universe as _resolve_universe, UniverseCfg  # noqa: PLC0415
                    harm_c2s = _resolve_universe(
                        base_map, tickers_map,
                        UniverseCfg(mode="all", asset_filter="all"),
                    )
                    log.info(
                        "谐波 universe_mode=all_perp：全市场 %d 个 USDT 永续合约（按成交额降序）",
                        len(harm_c2s),
                    )
                else:
                    # top_n 模式（默认，向后兼容）：按成交额取前 harm_n 个
                    harm_c2s = dict(list(vol_c2s.items())[:harm_n])
                # 并入用户「发现搜集」的币 → 持续监控。enabled 时清单已是唯一真相源(上面 harm_c2s=vol_c2s)；
                # 默认模式并入 monitored_coins(discover 现写此表) + harmonic_collected(旧迁移残留)，
                # 修 discover 真相源错位回归(写 monitored_coins 但默认只读 harmonic_collected)。
                if not self.cfg.monitored_coins.enabled:
                    try:
                        harm_c2s.update(self.store.get_harmonic_collected())
                        harm_c2s.update(self.store.get_monitored_coins())
                    except Exception as exc:  # noqa: BLE001
                        log.warning("读取 collected/monitored 失败: %s", exc)
                # 逐笔 taker 监控：订阅 Bitget trade channel → 资金流加速度 flow_score（补 R2 最后数据源）
                harm_s2c = {sym: coin for coin, sym in harm_c2s.items()}
                self.harmonic_trade = BitgetTradeMonitor(harm_s2c, self.bg_ws)
                # forming 逼近检测器：缓存 forming PRZ，用 trade 流实时价做周期检查（QA H6 安全:非热回调）
                # band_pct=0.008：价进入 PRZ 带 ±0.8% 即"逼近"预警（真前瞻提前量，非到达才报，修 M3）
                self.harmonic_approach = FormingApproachTracker(
                    ttl_ms=1_800_000, cooldown_ms=1_800_000, band_pct=0.008, invalidate_pct=0.02)
                # 前瞻信号 provider：每轮 refresh 用 Bitget tickers(OI/funding) 更新 + flow_score（逐笔）；
                # 对 completed+forming 施加前瞻乘子（funding 极值 + 资金流加速度）。
                self.harmonic_forward = HarmonicForwardSignals(
                    flow_source=self.harmonic_trade.flow_score)
                self.harmonic_monitor = HarmonicMonitor(
                    coin_to_symbol=harm_c2s,
                    timeframes=self.cfg.harmonic.timeframes,
                    bars=self.cfg.harmonic.bars,
                    order=self.cfg.harmonic.order,
                    tol=self.cfg.harmonic.tol,
                    top_n=len(harm_c2s),   # 含并入的 collected 币，全部纳入扫描
                    account_usd=self.cfg.harmonic.account_usd,
                    risk_pct=self.cfg.harmonic.risk_pct,
                    target_rr=self.cfg.harmonic.target_rr,
                    ob_provider=self.orderbook_monitor,  # 订单流确认层（HL l2Book，主流币）
                    store=self.store,   # 优先读 DB K线缓存，不足回退 live（减 API/跨重启持久）
                    forward_provider=self.harmonic_forward,  # 前瞻置信（OI/funding；completed+forming）
                )
                # 日志用真实纳入扫描的币数 len(harm_c2s)（all_perp 下=全永续；含并入 collected），
                # 不用 harm_n（=cfg.top_n，仅 top_n 模式语义），避免 all_perp 误显 "top_12"。
                log.info("谐波监控器已建，纳入扫描 %d 币(mode=%s): %s",
                         len(harm_c2s), harm_umode, list(harm_c2s.keys())[:6])

            # ---- 谐波 K线 WS 增量驱动（B1+B2）：可选实时性层（harmonic.realtime_ws=True 时启用）----
            # realtime_ws=False 时跳过，行为不变（纯 periodic refresh 模式，向后兼容）。
            # 启用时：订阅 candle{tf} channel，收盘线即触发增量 analyze_candles + 可选回调。
            # periodic refresh 保留作全量兜底（两者共存，互不排斥）。
            #
            # B2 落库协调设计（per-coin latest，解 B1 gap）：
            #   - recent_harmonic_setups() 已改为 per-coin per-tf latest 子查询（GROUP BY coin,tf）。
            #   - 实时层现可安全按单币落库（不会导致其他币消失）：
            #     on_update 先 delete_harmonic_coin_tf(coin,tf) 清旧行，再 insert_harmonic_setups 写新行。
            #   - 7 天 prune_before 是长期防膨胀；delete_harmonic_coin_tf 是短期去重。
            #   - periodic refresh 仍按需落全量（多币同 ts 批），两者共存无冲突（per-coin latest 读）。
            if (self.cfg.harmonic.realtime_ws
                    and self.harmonic_monitor is not None
                    and self.cfg.harmonic.enabled):
                from .monitor.harmonic_candle_ws import HarmonicCandleWS  # noqa: PLC0415

                # B2: on_update 回调——收盘分析完成 → 落库（单币）+ 推送实时卡片
                _hmon_ref = self.harmonic_monitor  # 避免闭包捕获 self 循环引用
                _store_ref = self.store            # 避免闭包捕获 self 循环引用

                async def _on_harmonic_ws_update(
                    coin: str, tf: str, result: dict | None, now_ms: int,
                ) -> None:
                    """K 线 WS 收盘后谐波分析完成回调：per-coin 落库 + 推送单币实时通知。

                    B2 实现：
                    1. 先删该 (coin,tf) 旧行（防短期堆积），再写新行（per-coin latest 语义）。
                    2. 推送实时卡片（同 B1 行为，不阻塞热路径）。
                    落库失败只 warn，不阻塞推送。
                    """
                    if result is None:
                        return  # 无形态，静默跳过
                    # 1. 单币落库（B2：per-coin upsert 策略）
                    try:
                        # 生成 to_records 格式（复用 harmonic_monitor.to_records）
                        row_dict = {
                            "coin":      coin,
                            "symbol":    _hmon_ref.coin_to_symbol.get(coin, coin + "USDT"),
                            "price":     float(result.get("price", 0.0) or 0.0),
                            "tf":        tf,
                            "completed": result.get("completed") or [],
                            "forming":   result.get("forming") or [],
                        }
                        records = await asyncio.to_thread(
                            _hmon_ref.to_records, [row_dict], now_ms
                        )
                        if records:
                            # 先删旧行，再写新行（防同 coin/tf 堆积）
                            await asyncio.to_thread(
                                _store_ref.delete_harmonic_coin_tf, coin, tf
                            )
                            await asyncio.to_thread(
                                _store_ref.insert_harmonic_setups, records
                            )
                            log.debug(
                                "谐波 WS B2 落库 %s/%s: %d 行",
                                coin, tf, len(records),
                            )
                    except Exception as _db_exc:  # noqa: BLE001
                        log.warning("谐波 WS on_update 落库失败 %s/%s: %s", coin, tf, _db_exc)
                    # 2. 推送实时卡片（同 B1，落库失败不影响推送）
                    try:
                        completed = result.get("completed") or []
                        forming = result.get("forming") or []
                        if not completed and not forming:
                            return  # 该 (coin,tf) 无形态，不推空卡
                        # 构造单币行（与 render 格式兼容）
                        row = {
                            "coin":      coin,
                            "symbol":    _hmon_ref.coin_to_symbol.get(coin, coin + "USDT"),
                            "price":     float(result.get("price", 0.0) or 0.0),
                            "tf":        tf,
                            "completed": completed,
                            "forming":   forming,
                        }
                        card = _hmon_ref.render([row], now_ms)
                        if card:
                            self._push_harmonic(f"[WS实时] {card}")
                            log.debug(
                                "谐波 WS on_update 推送 %s/%s: completed=%d forming=%d",
                                coin, tf, len(completed), len(forming),
                            )
                    except Exception as _exc:  # noqa: BLE001
                        log.warning("谐波 WS on_update 推送失败 %s/%s: %s", coin, tf, _exc)

                self.harmonic_candle_ws = HarmonicCandleWS(
                    harmonic_monitor=self.harmonic_monitor,
                    bg_ws=self.bg_ws,
                    on_update=_on_harmonic_ws_update,
                )
                log.info("谐波 K线 WS 增量驱动已初始化（realtime_ws=True，on_update 已接入）")
            else:
                self.harmonic_candle_ws = None

            # ---- Bitget K线采集器：周期拉永续 K 线落 DB，供 BB/谐波多周期计算共用（减 API 重复拉）----
            # 采集器用 vol_c2s 全集（resolve_universe 输出），轮转覆盖；BB/谐波各取其 top_n
            if (vol_c2s or self.cfg.monitored_coins.enabled) and (self.cfg.bollinger.enabled or self.cfg.harmonic.enabled):
                from .monitor.candle_collector import BitgetCandleCollector  # noqa: PLC0415
                # 采集币集 = resolve_universe 输出（已含 BB + 谐波所需的所有币）
                # all_perp 模式下，harm_c2s 含全部永续合约，合并保证采集器覆盖谐波所有需要的币
                # 周期 = 两板周期并集；bars 取较大者
                cc_c2s = dict(vol_c2s)   # 从全局 universe 起步
                if self.harmonic_monitor is not None and self.cfg.harmonic.universe_mode == "all_perp":
                    # all_perp：把谐波 universe 全部合并进采集集（键重复时保持已有映射，新的追加）
                    for _coin, _sym in harm_c2s.items():
                        if _coin not in cc_c2s:
                            cc_c2s[_coin] = _sym
                cc_tfs = list(dict.fromkeys(
                    list(self.cfg.bollinger.timeframes) + list(self.cfg.harmonic.timeframes)))
                cc_bars = max(self.cfg.bollinger.bars, self.cfg.harmonic.bars)
                # 监控清单模式：采集集=清单(vol_c2s)；周期取并集(monitored 含用户 6H ∪ bollinger ∪ harmonic 含 30m)，
                # 避免谐波 30m 落空走 live 回退读陈旧数据（修周期错配 P2-1）
                if self.cfg.monitored_coins.enabled:
                    cc_c2s = dict(vol_c2s)
                    cc_tfs = list(dict.fromkeys(
                        list(self.cfg.monitored_coins.timeframes)
                        + list(self.cfg.bollinger.timeframes)
                        + list(self.cfg.harmonic.timeframes))) or cc_tfs
                self.candle_collector = BitgetCandleCollector(
                    cc_c2s, cc_tfs, cc_bars, self.store)
                log.info("K线采集器已建，%d 币 × %d 周期 → DB（轮转模式）", len(cc_c2s), len(cc_tfs))

    # ---- 周期任务 ----
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
        # 谐波历史 + BB 压力层：保留 7 天供历史回看与多周期 S/R（v2 新增）
        ("harmonic_setups",      "ts",       7 * 86_400_000),
        ("bb_levels",            "ts",       7 * 86_400_000),
    ]

    # K 线滚动保留：每 (coin,tf) 保留最新 N 根（历史+实时统一上限；用户#：每周期 3000 bar）
    # bitget_candles 是计数型上限（非时间型），故独立于 _DB_RETAIN，由 prune_candles_to 裁剪。
    _CANDLE_RETAIN_BARS: int = 3000

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
                groups = self.correlation.clusters_detailed(
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
        mc = self.cfg.monitored_coins
        if not mc.enabled or mc.vol_board_sec <= 0:
            return
        from .monitor.volatility_monitor import VolatilityMonitor  # noqa: PLC0415
        await asyncio.sleep(120.0)   # 等采集器填 DB
        while not self._stopping:
            try:
                coins = self.store.get_monitored_coins()
                if coins:
                    mon = VolatilityMonitor(coins, list(mc.timeframes), self.store)
                    now = int(time.time() * 1000)
                    card = mon.render(mon.rank(now), now)
                    if card:
                        print(card)
                        self._push_harmonic(card)   # 独立 TA 通道
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
        # 谐波逐笔 taker 监控（Bitget trade channel → flow_score 资金流加速度）
        if getattr(self, "harmonic_trade", None) is not None:
            self.harmonic_trade.attach()
        # 谐波 K线 WS 增量驱动（B1）：收盘线即触发增量分析（realtime_ws=True 时启用）
        if self.harmonic_candle_ws is not None:
            self.harmonic_candle_ws.attach()
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
        # A3：每个任务用 supervise 包裹，任一异常 → 指数退避重启，不连累其余（不静默死）
        # WS run() 自身有自重连，supervise 仅兜逃逸到任务边界的非网络异常（backoff 稍大）
        # return_exceptions=True 作第二道防线（supervise 已吞，理论不触发；防御性）
        def _sv(fn, name: str, base: float = 1.0, max_b: float = 60.0):
            """快捷：factory = lambda 包装当前方法，返回 supervise coro。"""
            return supervise(fn, name=name, base_backoff=base, max_backoff=max_b, log=log)

        await asyncio.gather(
            _sv(lambda: self.hl_ws.run(), "hl_ws", base=5.0, max_b=60.0),
            _sv(lambda: self.bg_ws.run(), "bg_ws", base=5.0, max_b=60.0),
            _sv(lambda: self._periodic_flush(), "periodic_flush"),
            _sv(lambda: self._periodic_onchain(), "periodic_onchain"),
            _sv(lambda: self._periodic_solana(), "periodic_solana"),
            _sv(lambda: self._periodic_divergence(), "periodic_divergence"),
            _sv(lambda: self._periodic_consensus(), "periodic_consensus"),
            _sv(lambda: self._periodic_correlation(), "periodic_correlation"),
            _sv(lambda: self._periodic_flow_predict(), "periodic_flow_predict"),
            _sv(lambda: self._periodic_report(), "periodic_report"),
            _sv(lambda: self._periodic_llm(), "periodic_llm"),
            _sv(lambda: self._periodic_cleanup(), "periodic_cleanup"),
            _sv(lambda: self._periodic_review(), "periodic_review"),
            _sv(lambda: self._periodic_efficacy(), "periodic_efficacy"),
            _sv(lambda: self._periodic_health(), "periodic_health"),
            _sv(lambda: self._periodic_ticker_board(), "periodic_ticker_board"),
            _sv(lambda: self._periodic_hl_digest(), "periodic_hl_digest"),
            _sv(lambda: self._periodic_exchange_flow(), "periodic_exchange_flow"),
            _sv(lambda: self._periodic_wallet_portfolio(), "periodic_wallet_portfolio"),
            _sv(lambda: self._periodic_config_reload(), "periodic_config_reload"),
            _sv(lambda: self._periodic_bb_board(), "periodic_bb_board"),
            _sv(lambda: self._periodic_harmonic_board(), "periodic_harmonic_board"),
            _sv(lambda: self._periodic_volatility_board(), "periodic_volatility_board"),
            _sv(lambda: self._periodic_prz_approach(), "periodic_prz_approach"),
            _sv(lambda: self._periodic_candle_collect(), "periodic_candle_collect"),
            _sv(lambda: self._periodic_whale_pnl(), "periodic_whale_pnl"),
            _sv(lambda: self._periodic_discover(), "periodic_discover"),
            _sv(lambda: self._periodic_push_drain(), "periodic_push_drain"),
            return_exceptions=True,
        )

    async def stop(self) -> None:
        self._stopping = True
        self.meme_monitor.flush()
        self.orderbook_monitor.flush()   # 退出前冲刷剩余挂单墙事件
        card = self.hl_digest.render(int(time.time() * 1000))   # 退出前冲刷剩余 HL 分类汇总，不丢事件
        if card:
            self._push(card)
        if self.oi_monitor:
            self.oi_monitor.flush()
        # A2a：退出前同步冲刷剩余 sm_events 缓冲（退出非热路径，同步落库即可，不丢事件）
        if self._sm_buffer:
            rows, self._sm_buffer = self._sm_buffer, []
            self.store.insert_sm_events_batch(rows)
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
