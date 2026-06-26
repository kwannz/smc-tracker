"""Bitget 永续多周期谐波形态监控器。

照 bitget_bb_monitor.py 结构实现：
  - asyncio.Semaphore(≤8) 限流并发
  - 单币单周期异常 log.warning 吞掉
  - render 纯函数（接受 rows，不直接 I/O）
  - 价格全部 util.fmt_px（非科学计数法）

修复：
  - refresh 时每币每 tf completed/forming 各取 top 2（按 confidence 降序），降噪
  - render 时整卡 completed cap 8、forming cap 8，超出标注省略数
  - 卡片显示形态数为截断后实际展示数，不显示原始大数
  - completed 行显示「满足N腿」，forming 行显示「收敛N」（语义区分，T-3）
  - price≤0 的行跳过，不渲染无效卡片行（G-2）
  - 卡片副标题含枢轴滞后披露（T-1，CLAUDE.md 诚实）
  - completed 形态附 trade_setup（进场/止损/止盈/仓位/KNN）可执行推送
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..asset_class import asset_badge as _asset_badge
from ..bitget.rest import BitgetREST
from ..indicators.harmonic import analyze_candles
from ..indicators.harmonic_state import HarmonicState  # A3：增量状态机
from ..signals.forward_confirm import apply_forward as _apply_forward
from ..signals.orderflow_confirm import confirm_setup as _confirm_setup
from ..signals.trade_setup import TradeSetup, build_setups
from ..util import fmt_px, fmt_ts, to_float as _to_float

log = logging.getLogger("harmonic_monitor")


def _conf_tier(conf: float) -> str:
    """confidence → 样本外实测质量分层(#165:1.26万setup·价格先碰target1还是stop)。

    期望悬崖在 0.8：≥0.85(~0.9桶) OOS 83%胜率/+1.5R=🟢强；[0.75,0.85)(~0.8桶)+0.64R=🟡较强；
    <0.75(0.6-0.7桶,最常见但期望最低 +0.2R)=◆边际。让用户即知 raw 置信背后的真实期望档位。
    """
    if conf >= 0.85:
        return "🟢强"
    if conf >= 0.75:
        return "🟡较强"
    return "◆边际"

# 实证结论（A1）：在 _SEMA=4 下运行多轮 0 次 429；逐步提并发至 8/10 实测：
#   8 并发：多轮测试无 429（Bitget 公开 K 线接口速率宽松）；
#   10 并发：偶发 429（高负载大周期回填时触发）；
# 保守取 8：在实测无 429 的最高值，留有余量应对大周期回填场景。
_SEMA_LIMIT = 8          # 最大并发 Bitget 请求数（实证 ≤8 无 429，4→8 提速 ~2x）
_PER_COIN_TF_CAP = 2    # 每币每周期 completed/forming 各最多保留条数
_CARD_CAP = 8            # 整卡 completed/forming 各最多展示条数


def _fmt_qty(qty: float | None) -> str:
    """仓位数量 → 非科学计数字符串（避免 2.7e-3 等科学计数）。

    使用 fmt_px 路由（已处理小数动态精度）；qty=None 返回 '—'。
    """
    if qty is None:
        return "—"
    return fmt_px(qty)


class HarmonicMonitor:
    """多币种 × 多周期谐波形态监控器。

    Attributes:
        coin_to_symbol: {coin: bitget_symbol}，如 {"BTC": "BTCUSDT"}
        timeframes:     需要分析的 granularity 列表
        bars:           每个周期拉取根数
        order:          枢轴邻域大小（patterns.swing_highs/lows lookback）
        tol:            比率容差（默认 0.05）
        top_n:          最多监控前 N 个币
        account_usd:    仓位计算账户名义资金（USD）
        risk_pct:       单笔风险比例（如 0.01 = 1%）
        target_rr:      目标盈亏比（如 2.0）
    """
    __slots__ = (
        "coin_to_symbol", "timeframes", "bars", "order", "tol", "top_n",
        "account_usd", "risk_pct", "target_rr", "ob_provider", "store",
        "forward_provider",
        "_core_n", "_tail_shards", "_round",   # 分层调度状态（A2）
        "_states",    # A3: dict[(coin, tf) -> HarmonicState] 增量状态机
        "_state_n",   # A3: dict[(coin, tf) -> int] 已喂入 HarmonicState 的 K 线数
    )

    def __init__(
        self,
        coin_to_symbol: dict[str, str],
        timeframes: list[str],
        bars: int,
        order: int,
        tol: float,
        top_n: int,
        account_usd: float = 10_000.0,
        risk_pct: float = 0.01,
        target_rr: float = 2.0,
        ob_provider: Any | None = None,
        store: Any | None = None,
        forward_provider: Any | None = None,
        core_n: int = 60,
        tail_shards: int = 8,
    ) -> None:
        self.coin_to_symbol = coin_to_symbol
        self.timeframes = timeframes
        self.bars = bars
        self.order = order
        self.tol = tol
        self.top_n = top_n
        self.account_usd = account_usd
        self.risk_pct = risk_pct
        self.target_rr = target_rr
        # 订单流确认提供者（鸭子类型：需有 confirming_wall + book_imbalance）
        # None=无数据（HL l2Book 未覆盖的币），confirm_setup 返回 None，诚实不崩
        self.ob_provider = ob_provider
        # K 线缓存 store（实现 get_candles/upsert_candles 契约）
        # None=纯 live 模式（向后兼容）
        self.store = store
        # 前瞻信号提供者：callable (coin, direction) -> (CoinSignalProfile, flow_score,
        # funding_extreme) | None。None=无前瞻数据（forward_mult 不施加，置信不变，诚实）。
        # 对 completed + forming 都施加（解除旧 orderflow 的 completed 门控，QA 修复）。
        self.forward_provider = forward_provider
        # 分层调度配置（A2）：核心层 + 长尾分片 round-robin
        self._core_n: int = max(0, core_n)
        self._tail_shards: int = max(1, tail_shards)
        self._round: int = 0  # 每次 refresh 后递增，用于长尾分片轮次选择
        # A3：增量谐波状态机（(coin, tf) → HarmonicState），仅在 store 模式下用；
        # _state_n 记录各 (coin, tf) 已喂入 bars 数，用于增量追加逻辑。
        self._states: dict[tuple[str, str], HarmonicState] = {}
        self._state_n: dict[tuple[str, str], int] = {}  # (coin, tf) -> 已喂入 bar 数

    async def refresh(self, now_ms: int) -> list[dict]:
        """并发拉取所有币种 × 周期 K 线，分析谐波形态，返回有形态的行。

        每币每 tf 的 completed/forming 各取 top _PER_COIN_TF_CAP 条。

        Args:
            now_ms: 当前时间戳（毫秒），用于日志/标注

        Returns:
            list[dict] 每条: {coin, symbol, price, tf, completed:[...], forming:[...]}
            仅有形态（completed 或 forming 非空）的才进，按 max confidence 降序。
        """
        # ---------- 分层调度（A2）：核心层 + 长尾 round-robin ----------
        # 业界 hot/warm/cold tier 标准做法：高 vol 核心层每轮 refresh，长尾按片轮转。
        all_coins: list[tuple[str, str]] = list(self.coin_to_symbol.items())[:self.top_n]
        total = len(all_coins)

        # core_n >= 总币数 → 退化为全量每轮 refresh（向后兼容；无分层时 tail 为空）
        core_n_eff = min(self._core_n, total)
        core_coins: list[tuple[str, str]] = all_coins[:core_n_eff]
        tail_coins_all: list[tuple[str, str]] = all_coins[core_n_eff:]

        if tail_coins_all:
            # 长尾分片：按 round-robin 选本轮分片 idx = self._round % tail_shards
            shard_idx = self._round % self._tail_shards
            # 把 tail 均分 tail_shards 片（最后片可能稍多/少，ceil 保证覆盖）
            chunk = max(1, (len(tail_coins_all) + self._tail_shards - 1) // self._tail_shards)
            tail_shard: list[tuple[str, str]] = tail_coins_all[
                shard_idx * chunk: shard_idx * chunk + chunk
            ]
        else:
            tail_shard = []

        coins: list[tuple[str, str]] = core_coins + tail_shard
        # 递增轮次计数（在本轮 coins 确定后更新，不影响本轮选片）
        self._round += 1

        sema = asyncio.Semaphore(_SEMA_LIMIT)

        async def _fetch_tf(
            bg: BitgetREST, symbol: str, coin: str, tf: str
        ) -> tuple[str, str, str, dict | None]:
            """拉单币单周期 K 线并 analyze_candles，构建 trade_setup，返回 (coin, symbol, tf, result|None)。

            优先从 DB 读取 K 线（self.store 非 None 时）；DB 不足则回退到 live 网络拉取，
            并将 live 数据回填写入 DB（自愈）。store=None 时纯 live（向后兼容）。

            在有 candles 的上下文内调用 build_setups，将每条 completed/forming 形态的
            setup 直接注入对应 hit dict 的 "setup" 键（无 setup 则设 None，诚实不崩溃）。
            """
            async with sema:
                try:
                    # 谐波所需最小 K 线数（2*order+3 是 swing_highs/lows 最小窗口）
                    need_min: int = 2 * self.order + 3

                    # 优先 DB 读取
                    candles = self.store.get_candles(coin, tf, self.bars) if self.store is not None else []

                    if len(candles) < need_min:
                        # DB 不足，回退 live 拉取
                        candles = await bg.klines(symbol, tf, bars=self.bars, coin=coin)
                        # live 数据回填 DB（自愈：下次可直接用 DB，减少网络请求）
                        if self.store is not None and candles:
                            self.store.upsert_candles([
                                (coin, tf, k.open_time_ms, k.o, k.h, k.l, k.c, k.v)
                                for k in candles
                            ])
                    result = analyze_candles(candles, order=self.order, tol=self.tol)

                    # A3：增量 HarmonicState 维护（S6/S7）
                    # 每轮 refresh 增量喂入自上次以来的新 bar（仅 store 模式下有效，
                    # live 模式下 candles 每次全量拉取，无法确定"新"bar，跳过增量）。
                    # 正确性优先：若 snap != full，回退全量重算并 log.warning，绝不静默漂移。
                    if self.store is not None and candles:
                        state_key = (coin, tf)
                        hs = self._states.get(state_key)
                        n_prev = self._state_n.get(state_key, 0)
                        n_curr = len(candles)

                        if hs is None or n_prev > n_curr:
                            # 首次或 candle 数减少（DB limit 变化/重启）→ 重建，全量喂入
                            hs = HarmonicState(order=self.order, tol=self.tol)
                            for c in candles:
                                hs.update(c)
                            self._states[state_key] = hs
                            self._state_n[state_key] = n_curr
                        elif n_curr > n_prev:
                            # 增量：只喂入新增的 bar（candles 升序，后 n_curr-n_prev 根为新）
                            for c in candles[n_prev:]:
                                hs.update(c)
                            self._state_n[state_key] = n_curr
                        # 若 n_curr == n_prev：无新 bar，state 已是最新，不需喂入

                        # 正确性守卫：增量快照须与全量完全相等，否则回退全量 + warn
                        snap = hs.snapshot()
                        if snap != result:
                            log.warning(
                                "谐波 A3 增量 != 全量，%s/%s 回退全量重算并重置 HarmonicState",
                                coin, tf,
                            )
                            # 重置并重建（以全量 analyze_candles 为权威）
                            hs = HarmonicState(order=self.order, tol=self.tol)
                            for c in candles:
                                hs.update(c)
                            self._states[state_key] = hs
                            self._state_n[state_key] = n_curr
                            # result 维持全量 analyze_candles 结果（已计算，直接用）

                    if result is not None:
                        # 构建所有 setup，按 src_key 精确索引（🔴-1: 消除同名形态碰撞）
                        # build_setups 返回 completed 优先、置信降序列表
                        all_setups: list[TradeSetup] = build_setups(
                            coin, tf, candles, result,
                            account_usd=self.account_usd,
                            risk_pct=self.risk_pct,
                            target_rr=self.target_rr,
                        )
                        # 🔴-1: 建立索引：src_key → setup（精确匹配，不再用 tuple3 导致碰撞）
                        _setup_index: dict[str, TradeSetup] = {
                            s.src_key: s for s in all_setups
                        }

                        # 订单流确认：对每条 completed setup 注入 orderflow，
                        # confirmed 的 setup 置信 ×1.1（封顶 0.90）
                        # ob_provider=None 或无数据则跳过（诚实，不崩）
                        if self.ob_provider is not None:
                            for s in all_setups:
                                if s.completed:
                                    of = _confirm_setup(
                                        s.coin,
                                        s.direction,
                                        s.entry_lo,
                                        s.entry_hi,
                                        self.ob_provider,
                                    )
                                    s.orderflow = of
                                    if of is not None and of.confirmed:
                                        # 订单流确认 boost 置信（领先意图×PRZ，诚实封顶）
                                        s.confidence = min(0.90, s.confidence * 1.1)

                        # 前瞻确认：对 completed + forming **都**施加 forward_mult（解除 completed
                        # 门控，QA 修复）。provider=None 或无数据→不调整（诚实，缺数据=中性）。
                        if self.forward_provider is not None:
                            _apply_forward(all_setups, self.forward_provider)

                        # 🔴-1: 注入 setup 到每条 completed hit（按 src_key 精确匹配）
                        for hit in result.get("completed") or []:
                            pat = str(hit.get("pattern", ""))
                            dir_raw = hit.get("direction", "")
                            direction_str = "long" if dir_raw == "bull" else (
                                "short" if dir_raw == "bear" else dir_raw
                            )
                            # completed src_key: f"C|{pat}|{direction}|{D 点价格}"
                            hit_points = hit.get("points") or {}
                            d_info = hit_points.get("D")
                            if d_info and len(d_info) >= 2:
                                d_px = float(d_info[1])
                                src_key = f"C|{pat}|{direction_str}|{d_px}"
                            else:
                                # 无 D 点坐标兜底（退化为旧键，不崩溃）
                                src_key = f"C|{pat}|{direction_str}|None"
                            hit["setup"] = _setup_index.get(src_key)

                        # 🔴-1: 注入 setup 到每条 forming hit（按 src_key 精确匹配）
                        for hit in result.get("forming") or []:
                            pat = str(hit.get("pattern", ""))
                            dir_raw = hit.get("direction", "")
                            direction_str = "long" if dir_raw == "bull" else (
                                "short" if dir_raw == "bear" else dir_raw
                            )
                            # forming src_key: f"F|{pat}|{direction}|{round(prz_lo, 8)}"
                            prz = hit.get("prz") or (None, None)
                            prz_lo_hit = prz[0] if prz and len(prz) >= 1 else None
                            if prz_lo_hit is not None:
                                src_key = f"F|{pat}|{direction_str}|{round(float(prz_lo_hit), 8)}"
                            else:
                                src_key = f"F|{pat}|{direction_str}|None"
                            hit["setup"] = _setup_index.get(src_key)

                    return (coin, symbol, tf, result)
                except Exception as exc:  # noqa: BLE001
                    # repr(exc)：无消息异常(TimeoutError() 等)str() 为空无法诊断，repr 必含类型(§三-3)
                    log.warning("谐波数据拉取失败 %s/%s: %r", coin, tf, exc)
                    return (coin, symbol, tf, None)

        # 共享单一 BitgetREST session（T1：避免每 币×周期 新建会话的 N+1 握手/限流放大）
        async with BitgetREST() as bg:
            # 前瞻信号：取一次 tickers 快照（OI/funding 现成字段）更新 provider，
            # 供 _fetch_tf 内 apply_forward 对 completed+forming 施加前瞻乘子。
            # provider=None 或失败 → 跳过（apply_forward 不调用/缺数据=中性，诚实不崩、不阻塞）。
            if self.forward_provider is not None and hasattr(self.forward_provider, "update"):
                try:
                    tk_map = await bg.tickers()
                    parsed = {}
                    for coin, symbol in coins:
                        tk = tk_map.get(symbol) or {}
                        parsed[coin] = {
                            "symbol": symbol,
                            "oi": _to_float(tk.get("holdingAmount")),
                            "funding": _to_float(tk.get("fundingRate")),
                            "price": _to_float(tk.get("markPrice") or tk.get("lastPr")),
                        }
                    self.forward_provider.update(parsed, now_ms)
                except Exception as exc:  # noqa: BLE001
                    log.warning("谐波前瞻信号 tickers 更新失败: %s", exc)

            tasks = [
                _fetch_tf(bg, symbol, coin, tf)
                for coin, symbol in coins
                for tf in self.timeframes
            ]
            results_raw: list[tuple[str, str, str, dict | None]] = await asyncio.gather(*tasks)

        # 按 (coin, tf) 展开，筛选有形态的行
        rows: list[dict] = []
        for coin, symbol, tf, result in results_raw:
            if result is None:
                continue
            # 每币每 tf 各取 top _PER_COIN_TF_CAP（按 confidence 降序）
            completed = sorted(
                result.get("completed") or [],
                key=lambda r: r["confidence"], reverse=True
            )[:_PER_COIN_TF_CAP]
            forming = sorted(
                result.get("forming") or [],
                key=lambda r: r["confidence"], reverse=True
            )[:_PER_COIN_TF_CAP]
            if not completed and not forming:
                continue  # 无形态，跳过
            rows.append({
                "coin":      coin,
                "symbol":    symbol,
                "price":     result.get("price", 0.0),
                "tf":        tf,
                "completed": completed,
                "forming":   forming,
            })

        # 按最高 confidence 降序排列
        def _max_conf(row: dict) -> float:
            all_hits = row["completed"] + row["forming"]
            if not all_hits:
                return 0.0
            return max(r["confidence"] for r in all_hits)

        rows.sort(key=_max_conf, reverse=True)
        return rows

    def to_records(self, rows: list[dict], now_ms: int) -> list[tuple]:
        """把 refresh() 返回的 rows 展平成 harmonic_setups 表的 29 列 tuple 列表（纯函数）。

        列顺序（与 DB schema 对齐）：
          ts, coin, tf, kind, pattern, direction, price,
          entry_lo, entry_hi, stop, target1, target2,
          rr, confidence, knn, orderflow, fib_note,
          prz_lo, prz_hi,
          x_idx, x_px, a_idx, a_px, b_idx, b_px, c_idx, c_px, d_idx, d_px

        映射规则：
          - completed 有 setup → 用 setup 的精确进场/止损/目标/rr/confidence/fib_note。
          - completed 无 setup → entry/stop/target/rr/fib_note 全 NULL，prz 来自 hit.prz。
          - forming hit → stop/target1/target2/rr=NULL；entry_lo/hi 和 prz=hit.prz。
          - direction: bull→long, bear→short（forming 与 completed 统一映射）。
          - knn: True→'✓' / False→'✗' / None→'?'（无 setup 则 '?'）。
          - orderflow: confirmed→'✓bid{wall_usd}' / not confirmed→'✗' / None→''。
          - XABCD 点：completed hit 有 points → 从 hit["points"] 取各点 (idx, px)；
            forming hit 无 points → 全 10 列为 None。
        """
        result: list[tuple] = []
        for row in rows:
            coin: str = row.get("coin", "")
            tf: str = row.get("tf", "")
            price: float = float(row.get("price", 0.0) or 0.0)

            # ---- completed hits ----
            for hit in row.get("completed") or []:
                pat: str = str(hit.get("pattern", ""))
                dir_raw: str = hit.get("direction", "")
                direction: str = "long" if dir_raw == "bull" else (
                    "short" if dir_raw == "bear" else dir_raw
                )
                hit_conf: float = float(hit.get("confidence", 0.0) or 0.0)
                prz = hit.get("prz") or (None, None)
                prz_lo = float(prz[0]) if prz and prz[0] is not None else None
                prz_hi = float(prz[1]) if prz and len(prz) > 1 and prz[1] is not None else None

                setup = hit.get("setup")
                if setup is not None:
                    # 有 setup：用精确进场/止损/目标
                    entry_lo: float | None = float(setup.entry_lo)
                    entry_hi: float | None = float(setup.entry_hi)
                    stop: float | None = float(setup.stop)
                    target1: float | None = float(setup.target1)
                    target2: float | None = float(setup.target2)
                    rr: float | None = float(setup.rr)
                    confidence: float = float(setup.confidence)
                    fib_note: str | None = str(setup.fib_note) if setup.fib_note else None

                    # knn 映射
                    if setup.knn_supports is True:
                        knn: str = "✓"
                    elif setup.knn_supports is False:
                        knn = "✗"
                    else:
                        knn = "?"

                    # orderflow 映射
                    of = setup.orderflow
                    if of is None:
                        orderflow_str: str = ""
                    elif of.confirmed:
                        orderflow_str = f"✓bid{of.wall_usd}"
                    else:
                        orderflow_str = "✗"
                else:
                    # 无 setup：退化为 PRZ，止损/目标全 NULL
                    entry_lo = None
                    entry_hi = None
                    stop = None
                    target1 = None
                    target2 = None
                    rr = None
                    confidence = hit_conf
                    fib_note = None
                    knn = "?"
                    orderflow_str = ""

                # XABCD 点坐标（completed hit 有 points 时提取）
                hit_points = hit.get("points") or {}
                def _pt(key: str, idx: int) -> "int | float | None":
                    info = hit_points.get(key)
                    if info and len(info) >= 2:
                        return info[idx]
                    return None

                x_idx = _pt("X", 0)
                x_px  = _pt("X", 1)
                a_idx = _pt("A", 0)
                a_px  = _pt("A", 1)
                b_idx = _pt("B", 0)
                b_px  = _pt("B", 1)
                c_idx = _pt("C", 0)
                c_px  = _pt("C", 1)
                d_idx = _pt("D", 0)
                d_px  = _pt("D", 1)

                result.append((
                    now_ms, coin, tf, "completed", pat, direction, price,
                    entry_lo, entry_hi, stop, target1, target2,
                    rr, confidence, knn, orderflow_str, fib_note,
                    prz_lo, prz_hi,
                    x_idx, x_px, a_idx, a_px, b_idx, b_px, c_idx, c_px, d_idx, d_px,
                ))

            # ---- forming hits ----
            for hit in row.get("forming") or []:
                pat = str(hit.get("pattern", ""))
                dir_raw = hit.get("direction", "")
                direction = "long" if dir_raw == "bull" else (
                    "short" if dir_raw == "bear" else dir_raw
                )
                hit_conf = float(hit.get("confidence", 0.0) or 0.0)
                prz = hit.get("prz") or (None, None)
                prz_lo = float(prz[0]) if prz and prz[0] is not None else None
                prz_hi = float(prz[1]) if prz and len(prz) > 1 and prz[1] is not None else None

                result.append((
                    now_ms, coin, tf, "forming", pat, direction, price,
                    prz_lo, prz_hi,   # entry_lo/hi = prz 值
                    None, None, None,  # stop/target1/target2 = NULL
                    None, hit_conf, "?", "", None,  # rr/confidence/knn/orderflow/fib_note
                    prz_lo, prz_hi,
                    # XABCD 点：forming 未完成，全 None
                    None, None, None, None, None, None, None, None, None, None,
                ))

        return result

    def render(self, rows: list[dict], now_ms: int) -> str | None:
        """渲染谐波形态前瞻卡片（按币种分组，多周期并列）。

        新格式：
          - 按 coin 分组，每币一块（`━━ {coin}  {badge}  现价{price} ━━`）
          - 块内 completed 行用 ✅{tf} 前缀，forming 行用 🎯{tf} 前缀，多周期并列
          - 各 completed 行保留可执行 setup（进场/止损/目标/仓位/置信/KNN/订单流）
          - 各 forming 行保留 PRZ/置信/收敛
          - 按币最高 confidence 降序排列；每币块内 completed 在前（置信降序），forming 在后
          - 最多展示 _CARD_CAP 个币；每币块内 completed+forming 合计 ≤ _PER_COIN_ITEM_CAP 条
          - price≤0 的行跳过（G-2）
          - 副标题含枢轴滞后披露（T-1）

        Args:
            rows:   refresh() 返回值（或合成测试数据），每项 {coin, symbol, price, tf, completed, forming}
            now_ms: 当前时间戳（毫秒）

        Returns:
            格式化卡片字符串；rows 为空或全为 price≤0 时返回 None。
        """
        if not rows:
            return None

        ts = fmt_ts(now_ms)

        # ---------- 第一步：按 coin 聚合，跳过 price≤0 ----------
        # coin → {price, items: [(kind, tf, hit)]}
        # kind: "completed" 或 "forming"
        coin_data: dict[str, dict] = {}
        for r in rows:
            price = float(r.get("price", 0.0) or 0.0)
            if price <= 0.0:
                continue  # G-2：price=0 跳过
            coin: str = r["coin"]
            tf: str = r["tf"]

            if coin not in coin_data:
                coin_data[coin] = {"price": price, "items": []}
            else:
                # 用最新（或最大）有效 price（取后者确保稳定）
                if price > 0:
                    coin_data[coin]["price"] = price

            # 每行（每币每 tf）各取 top _PER_COIN_TF_CAP（按 confidence 降序）
            row_completed = sorted(
                r.get("completed") or [],
                key=lambda h: h["confidence"], reverse=True,
            )[:_PER_COIN_TF_CAP]
            row_forming = sorted(
                r.get("forming") or [],
                key=lambda h: h["confidence"], reverse=True,
            )[:_PER_COIN_TF_CAP]

            for h in row_completed:
                coin_data[coin]["items"].append(("completed", tf, h))
            for h in row_forming:
                coin_data[coin]["items"].append(("forming", tf, h))

        if not coin_data:
            return None

        # ---------- 第二步：各币按最高 confidence 降序排序 ----------
        _PER_COIN_ITEM_CAP = 6  # 每币块内 completed+forming 合计最多展示条数

        def _coin_max_conf(coin_key: str) -> float:
            items = coin_data[coin_key]["items"]
            if not items:
                return 0.0
            return max(h["confidence"] for _, _, h in items)

        sorted_coins: list[str] = sorted(
            coin_data.keys(), key=_coin_max_conf, reverse=True
        )[:_CARD_CAP]  # 最多 _CARD_CAP 个币

        # 统计总展示条数（用于副标题）
        total_shown = 0
        for coin in sorted_coins:
            items = coin_data[coin]["items"]
            # 排序：completed 在前，forming 在后；同类按 confidence 降序
            completed_items = sorted(
                [(k, tf, h) for k, tf, h in items if k == "completed"],
                key=lambda x: x[2]["confidence"], reverse=True,
            )
            forming_items = sorted(
                [(k, tf, h) for k, tf, h in items if k == "forming"],
                key=lambda x: x[2]["confidence"], reverse=True,
            )
            merged = completed_items + forming_items
            shown = merged[:_PER_COIN_ITEM_CAP]
            total_shown += len(shown)

        # ---------- 第三步：渲染卡片 ----------
        lines: list[str] = [
            f"🔷 谐波形态前瞻 [{ts}] (数据源: Bitget永续 · 谐波PRZ)",
            f"近窗 {len(sorted_coins)} 币（每币多周期并列；完整=入场触发 成形=前瞻PRZ；枢轴滞后~order根；订单流仅辅助·墙可能spoof）",
            "完整形态含订单流确认(领先意图×PRZ)；⚠订单流仅辅助，墙可能spoof/吸收≠必反转",
        ]

        for coin in sorted_coins:
            price = coin_data[coin]["price"]
            badge = _asset_badge(coin)
            items = coin_data[coin]["items"]

            # 排序：completed 在前（confidence降序），forming 在后（confidence降序）
            completed_items = sorted(
                [(k, tf, h) for k, tf, h in items if k == "completed"],
                key=lambda x: x[2]["confidence"], reverse=True,
            )
            forming_items = sorted(
                [(k, tf, h) for k, tf, h in items if k == "forming"],
                key=lambda x: x[2]["confidence"], reverse=True,
            )
            merged = completed_items + forming_items
            shown = merged[:_PER_COIN_ITEM_CAP]
            omit = len(merged) - len(shown)

            # 块头：━━ {coin}  {badge}  现价{price} ━━
            lines.append(f"━━ {coin}  {badge}  现价{fmt_px(price)} ━━")

            for kind, tf, hit in shown:
                pat   = hit["pattern"]
                dir_raw = hit.get("direction", "")
                dirn  = "看多" if dir_raw == "bull" else "看空"
                conf_pct = int(hit["confidence"] * 100)
                crab_note = "  ⚠Crab实测胜率偏低" if pat == "Crab" else ""

                if kind == "completed":
                    # ✅{tf} 前缀，渲染可执行 setup 或退化 PRZ
                    setup: TradeSetup | None = hit.get("setup")
                    if setup is not None:
                        # KNN 标志
                        if setup.knn_supports is True:
                            knn_flag = "✓"
                        elif setup.knn_supports is False:
                            knn_flag = "✗"
                        else:
                            knn_flag = "?"

                        qty_str = _fmt_qty(setup.position_qty)
                        notional_str = (
                            fmt_px(setup.position_notional)
                            if setup.position_notional is not None
                            else "—"
                        )

                        # 订单流标记（内联到同行）
                        of = setup.orderflow
                        if of is None:
                            of_inline = ""
                        elif of.confirmed:
                            side_label = "bid" if setup.direction == "long" else "ask"
                            of_inline = f" 📊订单流✓{side_label}{fmt_px(of.wall_usd)}"
                        else:
                            of_inline = " 📊订单流✗"

                        # T-3：completed 保留「满足N腿」fib 在附注行
                        line = (
                            f"  ✅{tf} {pat}({dirn})"
                            f" 进场{fmt_px(setup.entry_lo)}–{fmt_px(setup.entry_hi)}"
                            f" 止损{fmt_px(setup.stop)}"
                            f" 目标{fmt_px(setup.target1)}/{fmt_px(setup.target2)}"
                            f" rr{setup.rr:.1f}"
                            f" 仓位{qty_str}({notional_str})"
                            f" 置信{int(setup.confidence * 100)}%{_conf_tier(setup.confidence)}"
                            f" KNN{knn_flag}"
                            f"{of_inline}"
                            f"{crab_note}"
                        )
                        lines.append(line)
                        # fib_note 附注行（缩进）
                        lines.append(f"   {setup.fib_note}")
                        # 前瞻确认附注行（OI/funding/资金流加速度领先信号，QA 修复后真实接入；
                        # completed+forming 都施加，缺数据=中性，纯股票 funding=0 自动跳过）
                        if setup.forward:
                            lines.append(f"   🔮 {setup.forward}")
                        # 订单流详情附注行（仅有墙，追加失衡数据）
                        if of is not None and of.confirmed:
                            side_label = "bid" if setup.direction == "long" else "ask"
                            lines.append(
                                f"  📊订单流✓({side_label}墙{fmt_px(of.wall_usd)}"
                                f" 失衡{of.imbalance:+.2f})"
                            )
                        elif of is not None and not of.confirmed:
                            lines.append("  📊订单流✗(PRZ无同向墙)")
                    else:
                        # 无 setup → 退化为 PRZ 行（已过滤/无法计算风险）
                        prz = hit.get("prz") or (0.0, 0.0)
                        prz_lo, prz_hi = prz
                        conf_n = hit.get("confluence", 0)
                        d_info = hit.get("points", {}).get("D")
                        d_note = f" D@{fmt_px(d_info[1])}" if d_info else ""
                        line = (
                            f"  ✅{tf} {pat}({dirn}){d_note}"
                            f" PRZ {fmt_px(prz_lo)}–{fmt_px(prz_hi)}"
                            f"  置信{conf_pct}% 满足{conf_n}腿"
                            f"{crab_note}"
                        )
                        lines.append(line)

                else:  # forming
                    # 🎯{tf} 前缀，显示 PRZ/置信/收敛
                    prz = hit.get("prz") or (0.0, 0.0)
                    prz_lo, prz_hi = prz
                    conf_n = hit.get("confluence", 0)

                    # T-3：forming 显示「收敛N」语义
                    line = (
                        f"  🎯{tf} {pat}({dirn})"
                        f" PRZ{fmt_px(prz_lo)}–{fmt_px(prz_hi)}"
                        f" 置信{conf_pct}% 收敛{conf_n}"
                        f"{crab_note}"
                    )
                    lines.append(line)
                    # 前瞻确认附注行（forming 也接入领先信号，QA 修复解除 completed 门控）
                    fsetup: TradeSetup | None = hit.get("setup")
                    if fsetup is not None and fsetup.forward:
                        lines.append(f"   🔮 {fsetup.forward}")

            if omit > 0:
                lines.append(f"  …省略 {omit} 条（低 confidence）")

        return "\n".join(lines)
