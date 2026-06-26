"""轮询监控器（每 3600s 跑一轮，cron 友好，纯 REST 无 WS）。

每轮：发现庄 → 拉所有庄当前持仓(REST) → 与上次快照 diff(换仓:平仓/反手/减仓) →
多庄共识 → 持仓面板 → 落库 + 告警 + 保存快照。状态存 SQLite，跨运行接力。

为何轮询更适合抓庄：庄是持仓型，90s 内几乎不换仓；但**一小时**内平仓/反手/换仓真实发生，
小时级快照 diff 正好捕获这些「庄动作」，且无需常驻进程。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from ..bitget import BitgetREST
from ..config import Config, WatchAddress
from ..hyperliquid import HyperliquidInfo
from ..memecoins import normalize
from ..models import Side
from ..review import PredictionReview, fmt_accuracy
from ..signals import (ConfluenceAggregator, DivergenceDetector, WhaleConsensus,
                       WhalePositionTracker, positioning, pred_kind)
from ..storage import Store
from ..util import to_float
from .address_analyzer import AddressAnalyzer
from .address_correlation import AddressCorrelation
from .whale_discovery import fetch_leaderboard_rows, rank_smart_money
from .whale_momentum import WhaleMomentum, pnl_rows_from

log = logging.getLogger("poll")


def _hms(ms: int) -> str:
    import time
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000)) if ms else "--"


def _make_price_of(prices: dict[str, float]):
    """构造 coin→价格 查询闭包（normalize 容错：kPEPE/PEPE 等跨表命名统一命中）。

    跟庄/共识/背离用 HL 原始币名落库，超级信号用 normalize 后币名；统一的归一索引
    让 record(取发出价) 与 evaluate_due(取评估价) 两端都能命中，避免预测因命名漏评。
    """
    norm_index: dict[str, float] = {}
    for k, v in prices.items():
        if v > 0:
            norm_index.setdefault(normalize(k), v)

    def price_of(coin: str) -> float | None:
        v = prices.get(coin)
        if v and v > 0:
            return v
        return norm_index.get(normalize(coin))

    return price_of


def _merge_watchlist(
    whales: list[WatchAddress], watchlist: list[WatchAddress],
) -> list[WatchAddress]:
    """把 config.watchlist 显式追踪地址并入排行榜庄列表(去重，watchlist 追加在后)。

    小账户/非排行榜级地址(自动发现 min_account_value/min_alltime_pnl 门槛抓不到)经 config
    手动声明后，cron 轮询路径也能完整追踪其持仓/成交流/换仓 diff(此前 run_once 只追排行榜
    top_n，watchlist 永远抓不到)。whales[:N] 仍取排行榜真庄(追踪地址追加在末尾，不挤占庄画像
    Top3)。按地址大小写不敏感去重。
    """
    seen = {w.address.lower() for w in whales}
    out = list(whales)
    for w in watchlist:
        a = w.address.lower()
        if a not in seen:
            seen.add(a)
            out.append(w)
    return out


class PollMonitor:
    def __init__(self, cfg: Config, store: Store, top_n: int = 15,
                 min_change_usd: float = 1_000_000.0,
                 flow_window_ms: int = 3_600_000,
                 min_flow_usd: float = 200_000.0,
                 horizons: tuple[int, ...] = (3_600_000, 14_400_000, 86_400_000)) -> None:
        self.cfg = cfg
        self.store = store
        self.top_n = top_n
        self.min_change_usd = min_change_usd
        self.flow_window_ms = flow_window_ms      # 近 N ms 的成交净流向窗口
        self.min_flow_usd = min_flow_usd          # 跟庄建仓净流向阈值
        # MTF 多水平线：优先读 cfg.review.horizons_min（7 个 TF：5m/15m/30m/1h/4h/12h/1d），
        # 兜底使用构造参数 horizons（向后兼容老测试/直接实例化场景）。
        # 目的：诊断信号在哪个时间尺度有真 alpha（1h 已证≈随机；4h/12h/1d 最有希望）。
        cfg_horizons = [h * 60_000 for h in (cfg.review.horizons_min or [])]
        self.horizons: tuple[int, ...] = tuple(cfg_horizons) if cfg_horizons else horizons
        # 正确性回顾层：把每轮前瞻信号落 predictions 表，下轮到期用真实价核对方向对错
        # —— 部署轮询路径也闭环验证「追踪是否符合目的」(此前仅 app 流式模式有此闭环)。
        self.review = PredictionReview(store)

    async def run_once(self, now_ms: int) -> str:
        """跑一轮，返回文本摘要（同时已落库）。"""
        # 单次拉取排行榜(~16.8MB)：选庄排名 + PnL 动量共用，避免每轮重复下载(低延迟)
        lb_rows = await fetch_leaderboard_rows()
        whales = rank_smart_money(lb_rows, top_n=self.top_n)
        # 并入 config.watchlist 显式追踪地址(小账户/非排行榜级，自动发现抓不到 → 手动纳入)
        whales = _merge_watchlist(whales, self.cfg.watchlist)
        labels: dict[str, str] = {}
        positions: dict[tuple[str, str], float] = {}
        flow: dict[str, float] = defaultdict(float)   # coin -> 庄群近 Nh 净流向 USD
        since = now_ms - self.flow_window_ms
        async with HyperliquidInfo(self.cfg.hyperliquid.rest_url) as info:
            mids = await info.all_mids()
            # 安全解析 + 只保留有限正价(拒脏值/NaN/0)，防污染整轮估值
            prices = {k: px for k, v in mids.items() if (px := to_float(v)) > 0}
            for w in whales:
                a = w.address.lower()
                labels[a] = w.label
                try:
                    for p in await info.positions(w.address):
                        positions[(a, p.coin)] = p.szi
                except Exception as e:  # noqa: BLE001
                    log.warning("拉取 %s 持仓失败: %s", w.label, e)
                # 近窗口净流向（userFills 聚合：买为正卖为负）
                try:
                    for f in await info.user_fills(w.address):
                        if f.time_ms < since or f.coin.startswith("@"):
                            continue
                        flow[f.coin] += f.notional if f.side is Side.BUY else -f.notional
                except Exception as e:  # noqa: BLE001
                    log.warning("拉取 %s 成交失败: %s", w.label, e)

        # 1) 换仓 diff（vs 上次持久化快照）
        prev = self.store.load_whale_positions()
        tracker = WhalePositionTracker(store=self.store, min_notional=self.min_change_usd)
        tracker.seed_prev(prev)
        changes = tracker.scan(positions, prices, labels, now_ms)

        # 2) 多庄共识
        consensus = WhaleConsensus(store=self.store, cooldown_ms=0)
        cons = consensus.scan(positions, prices, labels, now_ms)

        # 3) 三源背离：Bitget 资金费(散户拥挤) ⟂ 庄群近窗净流向
        divs: list = []
        try:
            async with BitgetREST() as bg:
                tickers = await bg.tickers()
                base_map = await bg.perp_base_coins()
            canon_to_symbol: dict[str, str] = {}
            for sym, base in base_map.items():
                canon_to_symbol.setdefault(normalize(base), sym)
            det = DivergenceDetector(store=self.store)
            for coin, net in flow.items():
                sym = canon_to_symbol.get(normalize(coin))
                if not sym or sym not in tickers:
                    continue
                funding = float(tickers[sym].get("fundingRate") or 0)
                sig = det.evaluate(coin, funding, 0.0, net, now_ms)
                if sig:
                    divs.append(sig)
        except Exception as e:  # noqa: BLE001
            log.warning("背离计算失败: %s", e)

        # 4) 保存本轮快照
        snap = [(a, c, szi, szi * prices.get(c, 0.0), labels.get(a, ""), now_ms)
                for (a, c), szi in positions.items()]
        self.store.save_whale_positions(snap)

        # 4b) 庄家集团识别：近 30min meme 成交滑窗协同，跨≥2 币为同一实体硬证据
        # 消孤儿：AddressCorrelation 被 import 但之前 run_once 从未实例化，cron 路径缺庄家集团
        groups: list[dict] = []
        try:
            corr = AddressCorrelation(self.store, cfg=self.cfg.correlation)
            since_corr = now_ms - 1_800_000   # 近 30min
            groups = corr.clusters_detailed(
                since_corr, window_sec=120, min_shared=3, min_coins=2)
            # 为每群补充 leader 信息（领先建仓的核心地址）
            for d in groups:
                try:
                    d["_leader"] = corr.cluster_leader(
                        d["members"], since_corr, window_sec=120)
                except Exception as _e:  # noqa: BLE001
                    log.warning("lead_lag 计算失败(%s 地址): %s", len(d["members"]), _e)
                    d["_leader"] = None
        except Exception as e:  # noqa: BLE001 — 庄家集团识别失败不阻断主流程
            log.warning("庄家集团识别失败: %s", e)

        # 5) 多信号叠加共振（读本轮已写入的 consensus/divergence 等）
        confl = ConfluenceAggregator(self.store, cooldown_ms=0).scan(now_ms)

        # 5b) 正确性回顾闭环：本轮前瞻信号落 predictions，并评估上轮到期预测对比真实价
        price_of = _make_price_of(prices)
        self._record_predictions(flow, cons, divs, confl, price_of, now_ms)
        review_line = ""
        try:
            # 用「评估时刻」的新鲜时间戳：now_ms 在 cycle 开头捕获，但拉数据耗时数分钟，
            # 期间到期的预测应本轮即评估，否则要等下一轮(小时级 poll 下最多延迟 1h)(#53)。
            import time as _time
            eval_now = int(_time.time() * 1000)
            n_eval = self.review.evaluate_due(price_of, eval_now)
            rep = self.review.accuracy_report(eval_now - 86_400_000, eval_now)
            if rep.get("total_n", 0) > 0:
                review_line = "\n\n" + fmt_accuracy(rep)
                if n_eval:
                    review_line += f"\n  (本轮新评估 {n_eval} 条到期预测)"
        except Exception as e:  # noqa: BLE001 — 回顾失败不影响主监控
            log.warning("预测回顾失败: %s", e)

        # 6) 持仓面板
        panel = positioning(positions, prices, labels)

        digest = self._digest(whales, positions, changes, cons, confl, panel, flow, divs,
                              prev, now_ms, groups)
        digest += review_line
        # 6) 庄 PnL 动量(谁在变热/变冷) + 当前最火
        try:
            pnl_rows = pnl_rows_from(lb_rows, top_n=30)   # 复用本轮已拉取的排行榜
            wm = WhaleMomentum(self.store)
            mom = wm.momentum(pnl_rows, now_ms, window_ms=3_600_000)
            lines = [f"\n🔥 庄 PnL 动量 {len(mom)} 条："]
            lines += [f"  {e.fmt()}" for e in mom[:6]] or ["  (首轮快照/无显著变化)"]
            lines.append("🏆 当前最火(近24h PnL Top)：")
            for r in WhaleMomentum.hot_now(pnl_rows, 5):
                lines.append(f"  {r[0][:10]}… 近24h ${r[2]:,.0f} 账户 ${r[6]:,.0f}")
            wm.snapshot(pnl_rows, now_ms)
            digest += "\n" + "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            log.warning("PnL 动量失败: %s", e)
        # 7) 庄画像(Top3) + 高频对手方(地址关联)
        try:
            plines = ["\n🔍 庄画像(Top3):"]
            analyzer = AddressAnalyzer(self.store)
            async with HyperliquidInfo(self.cfg.hyperliquid.rest_url) as info2:
                for w in whales[:3]:
                    p = await analyzer.analyze(w.address, info2, now_ms)
                    if not p.get("perp_active", True):
                        # 排行榜按 spot+perp 聚合选中,但无永续持仓/成交 → 诚实标注,不报误导性 0 分
                        plines.append(f"  {w.label} (无永续活动·疑纯现货/休眠,排行榜 spot+perp 聚合口径)")
                    else:
                        plines.append(f"  {w.label} 评分{p['score']:.0f}/100 净敞口偏{p['net_bias']} "
                                      f"近期{p['n_trades']}单 胜率{p['win_rate']*100:.0f}%")
            cp = AddressCorrelation(self.store, cfg=self.cfg.correlation).counterparties(
                now_ms - 86_400_000, min_count=10, limit=3)
            if cp:
                plines.append("🤝 高频对手方(近24h):")
                plines += [f"  {a[:10]}…↔{b[:10]}… ×{c}" for a, b, c in cp]
            digest += "\n" + "\n".join(plines)
        except Exception as e:  # noqa: BLE001
            log.warning("画像/关联失败: %s", e)
        return digest

    def _record_predictions(self, flow, cons, divs, confl, price_of, now_ms,
                            horizons: tuple[int, ...] | None = None) -> int:
        """把本轮各前瞻信号落 predictions 表（每 (coin,kind) 对按 MTF 各记一条）。

        - 跟庄：近窗净流向越阈值 → 净买=long / 净卖=short
        - 共识/超级：信号 direction 已是 long/short
        - 背离：bullish=up / bearish=down（review 按 up/down 判方向对错）
        每个信号按 self.horizons（7 TF：5m/15m/30m/1h/4h/12h/1d）各落一条，
        使用 record_mtf 批量记录，便于后续按 TF 分解命中率，诊断信号在哪个时间尺度有 alpha。
        发出价用 price_of(coin)（HL mid，normalize 容错）；无有效价格时 record_mtf 自动跳过。
        返回实际尝试记录的「信号×水平线」条数。
        """
        hzs = list(horizons) if horizons is not None else list(self.horizons)
        seen: set[tuple[str, str]] = set()   # (normalize(coin), kind) 去重（避免同 coin+kind 重复记）
        total = 0

        def _rec(coin: str, kind: str, direction: str) -> None:
            nonlocal total
            key = (normalize(coin), kind)
            if key in seen:
                return
            seen.add(key)
            px = price_of(coin) or 0.0
            n = self.review.record_mtf(
                ts=now_ms, coin=coin, kind=kind, direction=direction,
                hl_px=px, bg_px=0.0, horizons_ms=hzs,
            )
            total += n

        # 跟庄：与 _digest 同一阈值，落「我们会推送的建仓信号」
        for coin, net in flow.items():
            if abs(net) >= self.min_flow_usd:
                _rec(coin, "跟庄", "long" if net > 0 else "short")
        for s in cons:
            _rec(s.coin, "共识", s.direction)
        for s in divs:
            # #176:逼空/分销分 kind 落表,实盘 by_kind 独立审判不对称 edge(#170)
            _rec(s.coin, pred_kind(s.direction), "up" if s.direction == "bullish" else "down")
        for s in confl:
            _rec(s.coin, "超级", s.direction)
        return total

    def _digest(self, whales, positions, changes, cons, confl, panel, flow, divs,
                prev, now_ms, groups: list | None = None) -> str:
        baseline = not prev
        lines = [f"📡 轮询监控 [{_hms(now_ms)}] · {len(whales)}庄 / {len(positions)}持仓"]
        if baseline:
            lines.append("  (首轮建立基线快照，下轮起检测换仓)")

        if confl:
            lines.append("\n🌟 多信号共振(超级信号)：")
            for s in confl:
                lines.append(f"  {s.fmt()}")

        lines.append(f"\n🔔 庄换仓 {len(changes)} 条：")
        for pc in changes:
            lines.append(f"  {pc.fmt()}")
        if not changes and not baseline:
            lines.append("  （本窗口无大额平仓/反手/减仓）")

        # 跟庄：近窗净流向越阈值
        builds = sorted([(c, n) for c, n in flow.items() if abs(n) >= self.min_flow_usd],
                        key=lambda x: abs(x[1]), reverse=True)
        # #186 操作化:轮询=高延迟路径→短线 edge 对延迟敏感(1h延迟4h归零),宜瞄 24h 长持(1h延迟仍+0.81%)
        lines.append(f"\n🐋 庄群近窗主动建仓 {len(builds)} 条（慢跟宜24h长持·短线edge延迟敏感#186）：")
        for coin, net in builds[:8]:
            d = "净买🟢" if net > 0 else "净卖🔴"
            lines.append(f"  {coin} {d} ${abs(net):,.0f}")
        if not builds:
            lines.append("  （无显著净建仓）")

        lines.append(f"\n🔀 三源背离 {len(divs)} 条：")
        for s in divs[:6]:
            lines.append(f"  {s.fmt()}")
        if not divs:
            lines.append("  （无）")

        lines.append(f"\n🤝 多庄共识 {len(cons)} 条：")
        for s in sorted(cons, key=lambda x: x.score, reverse=True)[:10]:
            lines.append(f"  {s.fmt()}")
        if not cons:
            lines.append("  （无）")

        lines.append("\n📋 庄持仓面板(净名义 Top)：")
        for p in panel[:8]:
            side = "净多🟢" if p.net_notional >= 0 else "净空🔴"
            lines.append(f"  {p.coin:<10} {side} ${abs(p.net_notional):,.0f} "
                         f"(多{p.n_long}/空{p.n_short})")

        # 庄家集团区块：仅当检测到跨币协同群时展示(无群不推空段)
        # 格式复用 app._periodic_correlation 风格，保持告警一致性
        if groups:
            lines.append(f"\n🕸️ 庄家集团识别(近30min·跨币协同) {len(groups)} 群：")
            for d in groups[:6]:
                g = d["members"]
                strength = f"跨{d['coins']}币·协同{d['events']}次·{d['links']}对"
                leader_info = d.get("_leader")
                leader_s = (f" 核心leader:{leader_info[0][:10]}…(领先{leader_info[1]}次)"
                            if leader_info else "")
                coins_s = f" 涉{','.join(d['coin_list'][:4])}" if d.get("coin_list") else ""
                addrs_s = "、".join(a[:10] + "…" for a in g[:6])
                lines.append(f"  🕸️庄家集团({len(g)}地址,{strength}): "
                             + addrs_s + coins_s + leader_s)

        return "\n".join(lines)
