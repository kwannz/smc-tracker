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
from .app_handlers import EventHandlersMixin
from .app_periodic import PeriodicTasksMixin, _apply_reconcile  # re-export: 测试从此处导入
from .app_periodic_data import PeriodicDataMixin

log = logging.getLogger("app")

from .util import fmt_hms as _hms          # 简洁 HH:MM:SS（高频控制台行）
from .util import fmt_ts as _ts            # 完整 日期+时间+时区（推送告警，便于事后回顾）
from .util import to_float as _f           # 统一安全数值解析
from .util import fmt_px as _fmt_px        # 统一价格格式（非科学计数法完整数字，见 util.fmt_px）
from .util import is_placeholder_addr      # 占位/零地址判别（跳过示例配置残留地址）

_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


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


class TradingSystem(EventHandlersMixin, PeriodicTasksMixin, PeriodicDataMixin):
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
                    # select_base_universe：统一选币纯函数（监控清单模式用清单，否则 universe_cfg）
                    from .config import select_base_universe  # noqa: PLC0415
                    _mc_on = self.cfg.monitored_coins.enabled
                    _monitored = self.store.get_monitored_coins() if _mc_on else {}
                    if _mc_on and not _monitored:
                        log.warning("监控清单为空(monitored_coins.enabled=true)，本轮不纳入任何币；"
                                    "用 `watch add` 或 dashboard 添加")
                    vol_c2s = select_base_universe(
                        _mc_on, _monitored, base_map, tickers_map, self.cfg.universe)
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
                # 并入「发现搜集」币 → 持续监控（harmonic_extra_coins 纯函数）：监控模式返回 {}(清单已是基集)；
                # 默认模式并入 harmonic_collected(旧迁移残留) ∪ monitored_coins(discover 现写此表)，
                # 修 discover 真相源错位回归(写 monitored_coins 但默认只读 harmonic_collected)。
                try:
                    from .config import harmonic_extra_coins  # noqa: PLC0415
                    harm_c2s.update(harmonic_extra_coins(
                        self.cfg.monitored_coins.enabled,
                        self.store.get_harmonic_collected(),
                        self.store.get_monitored_coins()))
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
                # collect_timeframes 纯函数：监控模式取 monitored∪bb∪harm 并集，否则 bb∪harm（修周期错配 P2-1）
                from .config import collect_timeframes  # noqa: PLC0415
                cc_tfs = collect_timeframes(
                    self.cfg.monitored_coins.enabled,
                    self.cfg.monitored_coins.timeframes,
                    self.cfg.bollinger.timeframes,
                    self.cfg.harmonic.timeframes)
                cc_bars = max(self.cfg.bollinger.bars, self.cfg.harmonic.bars)
                # 监控清单模式：采集集=清单(vol_c2s)
                if self.cfg.monitored_coins.enabled:
                    cc_c2s = dict(vol_c2s)
                self.candle_collector = BitgetCandleCollector(
                    cc_c2s, cc_tfs, cc_bars, self.store)
                log.info("K线采集器已建，%d 币 × %d 周期 → DB（轮转模式）", len(cc_c2s), len(cc_tfs))

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
        # append-only 时序表补裁剪（修审计 P2：原无保留→长跑无界增长）
        ("hl_orderbook_walls",   "ts",       7  * 86_400_000),
        ("okx_perp",             "ts",       7  * 86_400_000),
        ("okx_liquidations",     "ts",       7  * 86_400_000),
        ("okx_signals",          "ts",       30 * 86_400_000),
    ]

    # K 线滚动保留：每 (coin,tf) 保留最新 N 根（历史+实时统一上限；用户#：每周期 3000 bar）
    # bitget_candles 是计数型上限（非时间型），故独立于 _DB_RETAIN，由 prune_candles_to 裁剪。
    _CANDLE_RETAIN_BARS: int = 3000

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
