"""CLI 子命令处理器（从 cli.py 拆出的独立 one-shot handler，扁平模块）。

本模块只含与共享异步采集/评估管线无耦合的 13 个子命令 handler；
依赖方向 cli → cli_commands（本模块绝不 import cli，无循环）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 子命令处理器（独立 one-shot 命令）
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> None:
    """启动流式 app（WebSocket 实时监控）。"""
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        from . import app as _app_mod
        asyncio.run(_app_mod._amain(args.config))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[run] 启动失败：{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_report(args: argparse.Namespace) -> None:
    """打印 build_report 摘要（从本地 SQLite 聚合）。"""
    try:
        from .notify import build_report
        from .storage import Store

        store = Store(Path(args.db))
        now = int(time.time() * 1000)
        since = now - int(args.hours * 3_600_000)
        report = build_report(store, since, now)
        store.close()
        print(report)
    except Exception as exc:
        print(f"[report] 出错：{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_signals(args: argparse.Namespace) -> None:
    """打印 build_all_signals_report：11 张信号表按类型分组汇总（从本地 SQLite 聚合，无网络）。"""
    try:
        from .notify import build_all_signals_report
        from .storage import Store

        store = Store(Path(args.db))
        now = int(time.time() * 1000)
        since = now - int(args.hours * 3_600_000)
        report = build_all_signals_report(store, since, now)
        store.close()
        print(report)
    except Exception as exc:
        print(f"[signals] 出错：{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_vol(args: argparse.Namespace) -> None:
    """实时波动追踪板：监控清单币按速度+加速度领先信号排序（读 DB 已采 K 线，无网络）。"""
    try:
        from .storage import Store
        from .monitor.volatility_monitor import VolatilityMonitor, pick_coins

        store = Store(Path(args.db))
        # 与 dashboard 共用选币：有清单用清单，清单空则取近 24h 最剧烈币(消除两前端分叉，#141)
        coins = pick_coins(store)
        if not coins:
            print("[vol] 无可显示币（采集器尚未填 K 线；可 `watch add BTC ETH` 指定关注币）")
            store.close()
            return
        tfs = [t.strip() for t in args.tf.split(",") if t.strip()]
        mon = VolatilityMonitor(coins, tfs or ["15m"], store)
        now = int(time.time() * 1000)
        card = mon.render(mon.rank(now), now, top=args.top)
        print(card or "[vol] 暂无足够 K 线数据（采集器尚未填满，稍后再试）")
        store.close()
    except Exception as exc:
        print(f"[vol] 出错：{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_watch(args: argparse.Namespace) -> None:
    """监控币种清单增删查（写本地 SQLite，运行中监控进程周期对账热载入）。"""
    try:
        from .storage import Store

        store = Store(Path(args.db))
        if args.action == "add":
            now = int(time.time() * 1000)
            note = args.note or ""
            items = [(c.upper(), f"{c.upper()}USDT", now, note) for c in args.coins]
            store.add_monitored_coins(items)
            print(f"[watch] 已加入 {len(items)} 币: {', '.join(c.upper() for c in args.coins)}")
        elif args.action == "rm":
            n = store.remove_monitored_coins([c.upper() for c in args.coins])
            print(f"[watch] 已移除 {n} 币")
        else:  # list
            rows = store.list_monitored_coins()
            if not rows:
                print("[watch] 监控清单为空（用 `watch add BTC ETH` 添加）")
            else:
                print(f"[watch] 监控清单（{len(rows)} 币）:")
                for coin, sym, _ts, note in rows:
                    note_s = f"  # {note}" if note else ""
                    print(f"  {coin:<10} {sym:<14}{note_s}")
        store.close()
    except Exception as exc:
        print(f"[watch] 出错：{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_address(args: argparse.Namespace) -> None:
    """完整追踪单个 Hyperliquid 地址：画像 + 实时持仓 + 协同/对手方 + 轨迹 + PnL。"""
    try:
        from .hyperliquid import HyperliquidInfo
        from .monitor.address_dossier import build_dossier, fmt_dossier
        from .storage import Store

        store = Store(Path(args.db))

        async def _run() -> None:
            now_ms = int(time.time() * 1000)
            try:
                async with HyperliquidInfo() as info:
                    dossier = await build_dossier(
                        args.addr, info, store, now_ms, window_h=args.hours
                    )
                print(fmt_dossier(dossier))
            finally:
                store.close()

        asyncio.run(_run())
    except Exception as exc:
        print(f"[address] 出错：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_discover(args: argparse.Namespace) -> None:
    """从 Hyperliquid 排行榜自动发现聪明钱地址并打印。"""
    try:
        from .monitor.whale_discovery import discover_smart_money

        async def _run() -> None:
            whales = await discover_smart_money(top_n=args.top)
            print(f"发现聪明钱(庄)地址 {len(whales)} 个：")
            for w in whales:
                print(f"  {w.address}  {w.label}")

        asyncio.run(_run())
    except Exception as exc:
        print(f"[discover] 出错：{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_bench(args: argparse.Namespace) -> None:
    """信号计算链路延迟基准（无网络，确定性）。"""
    try:
        import numpy as np

        from .indicators import analyze as ta_analyze, compute_indicators
        from .models import Candle
        from .signals import FlowPredictor, PumpRadar, TASignal

        bars: int = args.bars
        iters: int = args.iters

        # 合成确定性 K 线（与 scripts/bench_latency.py 逻辑相同）
        rng = np.random.default_rng(7)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, bars)))
        t0 = 1_700_000_000_000
        candles: list[Candle] = []
        for i in range(bars):
            c = float(close[i])
            o = float(close[i - 1]) if i else c
            hi = max(o, c) * (1 + abs(rng.normal(0, 0.003)))
            lo = min(o, c) * (1 - abs(rng.normal(0, 0.003)))
            candles.append(
                Candle("BTC", "5m", t0 + i * 300_000, t0 + (i + 1) * 300_000,
                       o, hi, lo, c, float(rng.uniform(1e3, 5e3)),
                       int(rng.uniform(50, 500)))
            )

        now = candles[-1].close_time_ms
        ta = TASignal()
        pr = PumpRadar()
        fp = FlowPredictor()
        for i in range(60):  # 灌历史样本
            fp.push("BTC", float((-1) ** i) * 1e5, now - (60 - i) * 1000)

        def _bench(label: str, fn) -> None:
            fn()  # 预热
            samples = np.empty(iters)
            for i in range(iters):
                t = time.perf_counter_ns()
                fn()
                samples[i] = (time.perf_counter_ns() - t) / 1e6
            p50, p99, p999 = np.percentile(samples, [50, 99, 99.9])
            print(f"  {label:16} P50={p50:.3f}ms  P99={p99:.3f}ms  "
                  f"P99.9={p999:.3f}ms  max={samples.max():.3f}ms")

        print(f"信号计算链路延迟基准（{bars} 根 K 线 · {iters} 次迭代 · 单线程）")
        _bench("指标全计算", lambda: compute_indicators(candles))
        _bench("TA全景analyze", lambda: ta_analyze(candles, now))
        _bench("TA多因子信号", lambda: ta.evaluate(candles, None, now))
        _bench("暴涨雷达", lambda: pr.evaluate("BTC", candles, now))
        _bench("前瞻资金流预测", lambda: fp.predict("BTC", now, 0.3, 0.05))
        print("\n（纯计算，非阻塞 asyncio 热路径；"
              "端到端「接收→处理」延迟由 app 运行时埋点统计）")
    except Exception as exc:
        print(f"[bench] 出错：{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_llm(args: argparse.Namespace) -> None:
    """调用 Codex(GPT-5.4) 做一次 LLM 抓庄研判（需本机 `codex login`）。"""
    try:
        from .llm import CodexClient, MarketAnalyst
        from .notify import build_report
        from .storage import Store

        async def _run() -> None:
            now = int(time.time() * 1000)
            store = Store(Path(args.db))
            report = build_report(
                store,
                now - int(args.hours * 3_600_000),
                now,
                title=f"抓庄态势({args.hours:g}h)",
            )
            store.close()
            print("=" * 60, "\n态势摘要(喂给模型)：\n", report, "\n", "=" * 60, sep="")
            client = CodexClient(model=args.model)
            analyst = MarketAnalyst(client.complete, enabled=True)
            print("\n调用 Codex(GPT-5.4) 研判中…（首次可能数十秒）\n")
            verdict = await analyst.analyze(report)
            if verdict:
                print("LLM 抓庄研判：\n", verdict, sep="")
            else:
                print("研判失败/为空：确认已 `codex login`、模型名正确，或加大 timeout。")

        asyncio.run(_run())
    except Exception as exc:
        print(f"[llm] 出错：{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_wallet(args: argparse.Namespace) -> None:
    """打印单地址完整持仓画像（拉 clearinghouseState，不联网存库）。

    --history N>0 时额外用 user_fills_by_time 拉近 N 天历史成交，打印各 coin 开仓时间重建结果。
    """
    try:
        from .config import WatchAddress
        from .hyperliquid.info_client import HyperliquidInfo
        from .monitor.position_lifecycle import fmt_hold, reconstruct as _reconstruct
        from .monitor.wallet_portfolio import WalletPortfolio
        from .storage import Store

        store = Store(Path(args.db))

        async def _run() -> None:
            now_ms = int(time.time() * 1000)
            wp = WalletPortfolio(store, args.rest_url)
            wa = WatchAddress(args.addr, args.label or "")
            snaps = await wp.refresh([wa], now_ms)
            if snaps:
                print(wp.fmt(snaps[0], top=args.top))
            else:
                print(f"[wallet] 无法拉取 {args.addr} 的持仓（网络错误或地址无效）")

            # --history N：拉历史成交，重建开仓时间
            history_days = getattr(args, "history", 0)
            if history_days and history_days > 0:
                start_ms = now_ms - history_days * 86_400_000
                print(f"\n[wallet] 拉取近 {history_days} 天历史成交重建开仓时间…")
                try:
                    async with HyperliquidInfo(args.rest_url) as info:
                        fills = await info.user_fills_by_time(args.addr, start_ms)
                    lifecycles = _reconstruct(fills, now_ms)
                    if lifecycles:
                        print(f"  共 {len(fills)} 笔成交，重建 {len(lifecycles)} 个 coin 生命周期：")
                        for coin, lc in sorted(lifecycles.items()):
                            hold = fmt_hold(lc.open_ms, now_ms)
                            open_str = (
                                time.strftime("%m-%d %H:%M", time.localtime(lc.open_ms / 1000))
                                if lc.open_ms > 0 else "—"
                            )
                            print(f"  {coin:8s} {lc.current_dir:5s} 开仓{open_str} 持仓{hold} "
                                  f"段内{lc.n_segment_fills}笔")
                    else:
                        print("  无成交记录")
                except Exception as exc:  # noqa: BLE001
                    print(f"  [wallet --history] 历史成交拉取失败：{exc}", file=sys.stderr)

            store.close()

        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[wallet] 出错：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_dashboard(args: argparse.Namespace) -> None:
    """启动 Web 仪表盘（dashboard 模块延迟导入，避免未安装时影响其他子命令）。"""
    try:
        # 延迟 import：dashboard 模块由并行 agent 创建；不影响其他子命令的解析/运行
        from .dashboard import serve  # type: ignore[import]
        asyncio.run(serve(args.db, args.host, args.port))
    except KeyboardInterrupt:
        pass
    except ImportError as exc:
        print(f"[dashboard] dashboard 模块尚不可用：{exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[dashboard] 出错：{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_okx(args: argparse.Namespace) -> None:
    """OKX 永续实时 streaming：按 OI 排名选 top_n 永续，订阅 trades/OI → 打印净流向。"""
    try:
        from .okx.stream import run_stream
        asyncio.run(run_stream(args.top, args.secs))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[okx] 出错：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_health(args: argparse.Namespace) -> None:
    """打印系统健康快照（数据新鲜度 + 验证闭环积压，纯 DB 无网络）。"""
    try:
        from .health import fmt_health, system_health
        from .storage import Store

        store = Store(Path(args.db))
        rep = system_health(store, int(time.time() * 1000),
                            stale_after_s=args.stale_after)
        store.close()
        print(fmt_health(rep))
        sys.exit(0 if rep.get("ok") else 2)   # 非健康返回码 2，便于 cron/监控脚本判别
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[health] 出错：{exc}", file=sys.stderr)
        sys.exit(1)
