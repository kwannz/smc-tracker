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

from ..bitget.rest import BitgetREST
from ..indicators.harmonic import analyze_candles
from ..signals.trade_setup import TradeSetup, build_setups
from ..util import fmt_px, fmt_ts

log = logging.getLogger("harmonic_monitor")

_SEMA_LIMIT = 4          # 最大并发 Bitget 请求数（降并发避 429；大周期回填放大请求量）
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
        "account_usd", "risk_pct", "target_rr",
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

    async def refresh(self, now_ms: int) -> list[dict]:
        """并发拉取所有币种 × 周期 K 线，分析谐波形态，返回有形态的行。

        每币每 tf 的 completed/forming 各取 top _PER_COIN_TF_CAP 条。

        Args:
            now_ms: 当前时间戳（毫秒），用于日志/标注

        Returns:
            list[dict] 每条: {coin, symbol, price, tf, completed:[...], forming:[...]}
            仅有形态（completed 或 forming 非空）的才进，按 max confidence 降序。
        """
        coins = list(self.coin_to_symbol.items())[:self.top_n]
        sema = asyncio.Semaphore(_SEMA_LIMIT)

        async def _fetch_tf(
            bg: BitgetREST, symbol: str, coin: str, tf: str
        ) -> tuple[str, str, str, dict | None]:
            """拉单币单周期 K 线并 analyze_candles，构建 trade_setup，返回 (coin, symbol, tf, result|None)。

            在有 candles 的上下文内调用 build_setups，将每条 completed/forming 形态的
            setup 直接注入对应 hit dict 的 "setup" 键（无 setup 则设 None，诚实不崩溃）。
            """
            async with sema:
                try:
                    candles = await bg.klines(symbol, tf, bars=self.bars, coin=coin)
                    result = analyze_candles(candles, order=self.order, tol=self.tol)
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
                    log.warning("谐波数据拉取失败 %s/%s: %s", coin, tf, exc)
                    return (coin, symbol, tf, None)

        # 共享单一 BitgetREST session（T1：避免每 币×周期 新建会话的 N+1 握手/限流放大）
        async with BitgetREST() as bg:
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

    def render(self, rows: list[dict], now_ms: int) -> str | None:
        """渲染谐波形态前瞻卡片。

        - 展平后 completed/forming 各 cap _CARD_CAP 条
        - 显示形态数为截断后实际展示数
        - 超出部分标注省略数
        - completed 行显示「满足N腿」，forming 行显示「收敛N」（T-3 语义区分）
        - price≤0 的行跳过（G-2）
        - 副标题含枢轴滞后披露（T-1）

        Args:
            rows:   refresh() 返回值（或合成测试数据）
            now_ms: 当前时间戳（毫秒）

        Returns:
            格式化卡片字符串；rows 为空或全为 price≤0 时返回 None。
        """
        if not rows:
            return None

        ts = fmt_ts(now_ms)

        # 展平：每行先取 top _PER_COIN_TF_CAP，再展平，全卡 cap _CARD_CAP
        # G-2：price<=0 的行跳过（拉取失败兜底，不渲染无意义价格）
        all_forming: list[tuple[dict, dict]] = []
        all_completed: list[tuple[dict, dict]] = []
        for r in rows:
            if r.get("price", 0.0) <= 0.0:
                continue  # G-2：price=0 跳过
            # 每行（每币每 tf）各取 top _PER_COIN_TF_CAP（按 confidence 降序）
            row_forming = sorted(r["forming"], key=lambda h: h["confidence"], reverse=True)
            row_completed = sorted(r["completed"], key=lambda h: h["confidence"], reverse=True)
            for h in row_forming[:_PER_COIN_TF_CAP]:
                all_forming.append((r, h))
            for h in row_completed[:_PER_COIN_TF_CAP]:
                all_completed.append((r, h))

        # 若过滤 price=0 后全空，返回 None
        if not all_forming and not all_completed:
            return None

        # 按 confidence 降序后截断
        all_forming.sort(key=lambda x: x[1]["confidence"], reverse=True)
        all_completed.sort(key=lambda x: x[1]["confidence"], reverse=True)

        omit_forming   = max(0, len(all_forming) - _CARD_CAP)
        omit_completed = max(0, len(all_completed) - _CARD_CAP)

        forming_rows   = all_forming[:_CARD_CAP]
        completed_rows = all_completed[:_CARD_CAP]

        # 统计实际展示数（截断后）
        n_forming   = len(forming_rows)
        n_completed = len(completed_rows)
        total = n_forming + n_completed

        lines: list[str] = [
            f"🔷 谐波形态前瞻 [{ts}] (数据源: Bitget永续 · 谐波PRZ)",
            f"近窗 {total} 个形态（完整形态含可执行进场/止损/止盈/仓位；成形=前瞻PRZ；枢轴需右确认，C/D 滞后~order根）",
        ]

        # ---- 成形中（前瞻预测）区块 ----
        if forming_rows:
            lines.append("【🎯 成形中(前瞻预测)】")
            for row, hit in forming_rows:
                coin     = row["coin"]
                tf       = row["tf"]
                price    = row["price"]
                pat      = hit["pattern"]
                dirn     = "看多" if hit["direction"] == "bull" else "看空"
                prz_lo, prz_hi = hit["prz"]
                conf_pct = int(hit["confidence"] * 100)
                conf_n   = hit.get("confluence", 0)

                # Crab 诚实警示（实测胜率偏低，CLAUDE.md §产品方向：诚实不夸大）
                crab_note = "  ⚠Crab实测胜率偏低" if pat == "Crab" else ""

                # T-3：forming 显示「收敛N」语义
                line = (
                    f"  • {coin} {tf} {pat}({dirn})"
                    f" PRZ {fmt_px(prz_lo)}–{fmt_px(prz_hi)}"
                    f"  置信{conf_pct}% 收敛{conf_n}"
                    f"  现价{fmt_px(price)}"
                    f"{crab_note}"
                )
                lines.append(line)
            if omit_forming > 0:
                lines.append(f"  …省略 {omit_forming} 条（低 confidence）")

        # ---- 完整形态（入场触发）区块 ----
        if completed_rows:
            lines.append("【✅ 完整形态(入场触发)】")
            for row, hit in completed_rows:
                coin     = row["coin"]
                tf       = row["tf"]
                pat      = hit["pattern"]
                dirn     = "看多" if hit["direction"] == "bull" else "看空"
                prz_lo, prz_hi = hit["prz"]
                conf_pct = int(hit["confidence"] * 100)
                conf_n   = hit.get("confluence", 0)
                crab_note = "  ⚠Crab实测胜率偏低" if pat == "Crab" else ""

                # 尝试渲染可执行 setup（新格式）
                setup: TradeSetup | None = hit.get("setup")
                if setup is not None:
                    # KNN 标志：True=✓ / False=✗ / None=?
                    if setup.knn_supports is True:
                        knn_flag = "✓"
                    elif setup.knn_supports is False:
                        knn_flag = "✗"
                    else:
                        knn_flag = "?"

                    # 仓位数量非科学计数，notional 用 fmt_px
                    qty_str = _fmt_qty(setup.position_qty)
                    notional_str = fmt_px(setup.position_notional) if setup.position_notional is not None else "—"

                    # T-3：completed 显示「满足N腿」语义 + 可执行进场/止损/目标/仓位/置信/KNN
                    line = (
                        f"  • {coin} {tf} {pat}({dirn})"
                        f" 进场{fmt_px(setup.entry_lo)}–{fmt_px(setup.entry_hi)}"
                        f" 止损{fmt_px(setup.stop)}"
                        f" 目标{fmt_px(setup.target1)}/{fmt_px(setup.target2)}"
                        f" rr{setup.rr:.1f}"
                        f" 仓位{qty_str}({notional_str})"
                        f" 置信{int(setup.confidence * 100)}%"
                        f" KNN{knn_flag}"
                        f"{crab_note}"
                    )
                    lines.append(line)
                    # fib_note 附注行（缩进）
                    lines.append(f"   {setup.fib_note}")
                else:
                    # 无 setup（劣质 setup 被 compute_risk 过滤）→ 退化为旧 PRZ 行
                    d_info = hit.get("points", {}).get("D")
                    d_note = f" D@{fmt_px(d_info[1])}" if d_info else ""
                    line = (
                        f"  • {coin} {tf} {pat}({dirn}){d_note}"
                        f" PRZ {fmt_px(prz_lo)}–{fmt_px(prz_hi)}"
                        f"  置信{conf_pct}% 满足{conf_n}腿"
                        f"{crab_note}"
                    )
                    lines.append(line)
            if omit_completed > 0:
                lines.append(f"  …省略 {omit_completed} 条（低 confidence）")

        return "\n".join(lines)
