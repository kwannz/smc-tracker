"""统一 CLI 入口 —— 把所有分散脚本收拢成子命令。

用法示例：
  python -m smc_tracker run
  python -m smc_tracker poll --loop --interval 3600
  python -m smc_tracker report --hours 24
  python -m smc_tracker address 0xABCD...
  python -m smc_tracker discover --top 15
  python -m smc_tracker bench 300 3000
  python -m smc_tracker llm --hours 6
  python -m smc_tracker dashboard --port 8787
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# 项目根：src/smc_tracker/cli.py → parents[2] = repo 根
_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = str(_ROOT / "data" / "smc.db")
_DEFAULT_CONFIG = str(_ROOT / "config" / "config.yaml")


# ---------------------------------------------------------------------------
# 子命令处理器
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


async def _poll_once_async(cfg, store) -> str:
    """采集一轮：构造 PollMonitor 运行 run_once，返回 digest 文本（无推送）。

    供 _cmd_poll 和 _cmd_cycle 复用，避免重复代码。
    """
    from .monitor.poll_monitor import PollMonitor
    now = int(time.time() * 1000)
    mon = PollMonitor(cfg, store)
    return await mon.run_once(now)


# cron 前瞻失衡阈值：订单簿买卖挂单失衡绝对值超过此值才产生信号
# 说明：cron 前瞻仅用订单簿挂单意图(领先未成交信号)；不含流式 app 的资金流加速度(2阶导)，
# 因加速度计算需要多轮时序采样，而 cron 模式无常驻进程，无法积累流向历史。
_FORECAST_IMB_THRESHOLD: float = 0.25
# cron 前瞻每次最多拉取 coin 数（避免 REST 请求过多）
_FORECAST_MAX_COINS: int = 24


async def _forecast_once_async(store: Any, info: Any, mids: dict[str, str]) -> int:
    """cron 订单簿挂单意图前瞻：对 meme_markets 拉 l2Book 计算失衡，强失衡时落库并记录 review。

    参数
    ----
    store  : Store 实例（复用调用方已打开的连接）
    info   : HyperliquidInfo 实例（调用方已 async with 打开，复用连接避免重新握手）
    mids   : allMids 价格字典（调用方已拉取，复用避免重复联网）

    返回产生的前瞻信号数。

    局限说明（诚实标注）：
    - 仅用订单簿挂单意图(l2Book)，这是单次快照领先信号；
    - 不含流式 app 的资金流加速度(2阶导时序)，后者需常驻进程积累流向历史；
    - vel/accel 固定填 0.0，如实反映 cron 无时序信息的现实，不造假。
    """
    import time as _time

    from .review import PredictionReview
    from .signals.flow_predictor import orderbook_imbalance
    from .util import to_float

    # 读 meme_markets.yaml 获取 coin 列表
    try:
        import yaml
        _cfg_path = _ROOT / "config" / "meme_markets.yaml"
        with open(_cfg_path, encoding="utf-8") as _f:
            _meme_cfg = yaml.safe_load(_f)
        coins: list[str] = (_meme_cfg.get("meme_markets") or [])[:_FORECAST_MAX_COINS]
    except Exception:
        coins = []

    if not coins:
        return 0

    # 并发限流：避免一次性发出过多 REST 请求
    sem = asyncio.Semaphore(6)
    now = int(_time.time() * 1000)
    review = PredictionReview(store)
    n_signals = 0

    async def _fetch_coin(coin: str) -> None:
        nonlocal n_signals
        try:
            async with sem:
                l2 = await info._post({"type": "l2Book", "coin": coin})
            lv = (l2 or {}).get("levels") or [[], []]
            imb = orderbook_imbalance(lv[0], lv[1])["imbalance"]
        except Exception:  # noqa: BLE001
            return  # 单 coin 失败不中断整体

        if abs(imb) < _FORECAST_IMB_THRESHOLD:
            return  # 失衡不足，不产生信号

        direction = "long" if imb > 0 else "short"
        px = to_float(mids.get(coin, "0"), 0.0)
        if px <= 0:
            return  # 无有效价格，跳过（record 需价格做事后交叉验证）

        # vel/accel 填 0.0：cron 无时序流速/加速度，诚实标注而非伪造
        store.insert_flow_prediction((now, coin, direction, imb, 0.0, 0.0, imb))
        review.record(
            ts=now,
            coin=coin,
            kind="前瞻",
            direction=direction,
            hl_px=px,
            bg_px=0.0,
            note="cron挂单意图",
        )
        store.conn.commit()
        n_signals += 1

    await asyncio.gather(*[_fetch_coin(c) for c in coins])
    return n_signals


async def _evaluate_once_async(store, hours: float) -> tuple[int, str]:
    """评估一次到期预测并产生准确率报告，返回 (已评估条数, 格式化摘要文本)。

    供 _cmd_evaluate 和 _cmd_cycle 复用，避免重复代码。
    需在 async 上下文中调用（内部含 asyncio 子任务）。
    """
    from .hyperliquid.info_client import HyperliquidInfo
    from .review import PredictionReview, fmt_accuracy
    from .util import to_float

    async with HyperliquidInfo() as info:
        mids: dict[str, str] = await info.all_mids()

    def price_of(coin: str) -> float | None:
        """从 allMids 取当前价，to_float 校验 >0 才返回，否则 None。"""
        raw = mids.get(coin, "")
        px = to_float(raw, 0.0)
        return px if px > 0 else None

    review = PredictionReview(store)
    now = int(time.time() * 1000)
    n = review.evaluate_due(price_of, now)
    store.conn.commit()
    since_ms = now - int(hours * 3_600_000)
    rep = review.accuracy_report(since_ms, now)
    summary = fmt_accuracy(rep)
    return n, summary


async def _evaluate_once_async_with_mids(
    store, hours: float, mids: dict[str, str]
) -> tuple[int, str]:
    """评估到期预测，复用调用方已拉取的 mids 价格字典（cycle 避免重复联网）。

    供 _cmd_cycle 使用：allMids 已在 cycle 顶部拉取并传入，不再二次拉取。
    逻辑与 _evaluate_once_async 完全一致，仅跳过 allMids 网络请求。
    mids 为空时（allMids 拉取失败）仍正常执行，evaluate_due 会跳过无价格的条目。
    """
    from .review import PredictionReview, fmt_accuracy
    from .util import to_float

    def price_of(coin: str) -> float | None:
        """从 allMids 取当前价，to_float 校验 >0 才返回，否则 None。"""
        raw = mids.get(coin, "")
        px = to_float(raw, 0.0)
        return px if px > 0 else None

    review = PredictionReview(store)
    now = int(time.time() * 1000)
    n = review.evaluate_due(price_of, now)
    store.conn.commit()
    since_ms = now - int(hours * 3_600_000)
    rep = review.accuracy_report(since_ms, now)
    summary = fmt_accuracy(rep)
    return n, summary


def _cmd_poll(args: argparse.Namespace) -> None:
    """轮询监控：单次或 --loop 持续运行。"""
    try:
        from .config import Config
        from .notify import build_notifier
        from .storage import Store

        cfg_path = Path(args.config)
        cfg = Config.load(cfg_path) if cfg_path.exists() else Config()
        store = Store(Path(args.db))
        notifier = build_notifier(cfg)

        async def _one_cycle() -> None:
            digest = await _poll_once_async(cfg, store)
            print(digest, flush=True)
            if notifier.enabled:
                # 完整推送：notifier 内部按各渠道上限分段全发(不截断,#59)
                now = int(time.time() * 1000)
                ok = await notifier.send(digest, now)
                print(f"[{time.strftime('%H:%M:%S')}] [webhook] {'已推送' if ok else '失败'}",
                      flush=True)
            else:
                print("[webhook] 未配置 output.webhook_url（不推送）", flush=True)

        async def _run() -> None:
            if args.loop:
                print(f"动态监控启动：每 {args.interval:g}s 一轮，推送渠道={notifier.channels}",
                      flush=True)
                while True:
                    try:
                        await _one_cycle()
                    except Exception as exc:  # noqa: BLE001
                        # 带类型：TimeoutError 等的 str(exc) 为空，只打消息会丢失信息(#56)
                        print(f"[轮询出错] {type(exc).__name__}: {exc}", flush=True)
                    await asyncio.sleep(args.interval)
            else:
                await _one_cycle()
            store.close()

        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[poll] 出错：{type(exc).__name__}: {exc}", file=sys.stderr)
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


def _cmd_evaluate(args: argparse.Namespace) -> None:
    """拉取当前价评估所有到期未评估的前瞻预测，输出准确率回顾（含市场中性纯 alpha）。

    可 cron 化运行，无需常驻 app；解决「评估管线停滞」运维问题，持续积累 alpha 诊断数据。
    复用 _evaluate_once_async 共享 helper（与 cycle 子命令共用同一评估管线，去重）。
    """
    try:
        from .storage import Store

        store = Store(args.db)

        async def _run() -> None:
            n, summary = await _evaluate_once_async(store, args.hours)
            print(f"评估了 {n} 条到期预测")
            print(summary)

            # --push：若配置了推送渠道则发送准确率摘要
            if args.push:
                try:
                    from .config import Config
                    from .notify import build_notifier
                    cfg_path = Path(args.config)
                    cfg = Config.load(cfg_path) if cfg_path.exists() else Config()
                    notifier = build_notifier(cfg)
                    now = int(time.time() * 1000)
                    if notifier.enabled:
                        await notifier.send(summary, now)
                        print(f"[{time.strftime('%H:%M:%S')}] [evaluate] 准确率摘要已推送")
                    else:
                        print("[evaluate] --push 已设但推送渠道未配置（跳过）")
                except Exception as push_exc:  # noqa: BLE001
                    print(f"[evaluate] 推送失败（不影响评估结果）：{push_exc}", file=sys.stderr)

        asyncio.run(_run())
        store.close()

    except Exception as exc:
        print(f"[evaluate] 出错：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_cycle(args: argparse.Namespace) -> None:
    """单次闭环 cycle：采集(poll) + 前瞻(forecast) + 评估(evaluate) + 合并推送，cron 友好。

    让一条 crontab 条目即可驱动整个抓庄闭环常态运转：
      1. 采集：PollMonitor.run_once → 庄持仓/换仓/共识/背离/PnL 动量摘要。
      2. 前瞻：_forecast_once_async → 订单簿挂单意图前瞻，落库 flow_predictions + predictions。
      3. 评估：拉 HL allMids → evaluate_due → accuracy_report → fmt_accuracy。
      4. 推送（--push 且渠道已配置）：poll digest + 准确率摘要合并推送。

    allMids 只拉一次，前瞻和评估共享同一价格快照，避免重复网络请求。
    复用 _poll_once_async / _evaluate_once_async / _forecast_once_async，零重复实现。
    """
    try:
        from .config import Config
        from .hyperliquid.info_client import HyperliquidInfo
        from .notify import build_notifier
        from .storage import Store
        from .util import to_float

        cfg_path = Path(args.config)
        cfg = Config.load(cfg_path) if cfg_path.exists() else Config()
        store = Store(Path(args.db))
        notifier = build_notifier(cfg)

        async def _run() -> None:
            # 1) 采集
            try:
                digest = await _poll_once_async(cfg, store)
                print(digest, flush=True)
            except Exception as poll_exc:  # noqa: BLE001
                digest = f"[cycle] 采集失败：{type(poll_exc).__name__}: {poll_exc}"
                print(digest, flush=True)

            # 拉 allMids 一次，前瞻和评估共享（避免重复联网，去重）
            mids: dict[str, str] = {}
            try:
                async with HyperliquidInfo(cfg.hyperliquid.rest_url) as _info:
                    mids = await _info.all_mids()

                    # 2) 前瞻：订单簿挂单意图（复用已打开的 _info 连接）
                    try:
                        n_forecast = await _forecast_once_async(store, _info, mids)
                        print(f"前瞻产生 {n_forecast} 条订单簿挂单意图信号", flush=True)
                    except Exception as fc_exc:  # noqa: BLE001
                        print(f"[cycle] 前瞻失败（不影响评估）：{type(fc_exc).__name__}: {fc_exc}",
                              flush=True)
            except Exception as mids_exc:  # noqa: BLE001
                print(f"[cycle] allMids 拉取失败：{type(mids_exc).__name__}: {mids_exc}",
                      flush=True)

            # 3) 评估（复用已拉的 mids，避免重复联网）
            eval_summary = ""
            try:
                n_eval, eval_summary = await _evaluate_once_async_with_mids(
                    store, args.hours, mids
                )
                print(f"评估了 {n_eval} 条到期预测", flush=True)
                print(eval_summary, flush=True)
            except Exception as eval_exc:  # noqa: BLE001
                eval_summary = f"[cycle] 评估失败：{type(eval_exc).__name__}: {eval_exc}"
                print(eval_summary, flush=True)

            # 4) 推送：合并 poll digest + 准确率摘要
            if args.push:
                now = int(time.time() * 1000)
                if notifier.enabled:
                    # 合并两段内容，用分隔线区分；notifier 内部按渠道限制分段全发
                    combined = digest
                    if eval_summary:
                        combined = combined + "\n\n" + eval_summary
                    ok = await notifier.send(combined, now)
                    print(f"[{time.strftime('%H:%M:%S')}] [cycle] {'已推送' if ok else '推送失败'}",
                          flush=True)
                else:
                    print("[cycle] --push 已设但推送渠道未配置（跳过）", flush=True)

            store.close()

        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[cycle] 出错：{type(exc).__name__}: {exc}", file=sys.stderr)
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


# ---------------------------------------------------------------------------
# 构建 parser（独立函数，便于测试）
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """构建并返回 CLI argparse.ArgumentParser（含全部子命令）。"""
    ap = argparse.ArgumentParser(
        prog="smc_tracker",
        description="SMC 聪明钱抓庄系统 — 统一 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m smc_tracker run\n"
            "  python -m smc_tracker poll --loop --interval 3600\n"
            "  python -m smc_tracker report --hours 6\n"
            "  python -m smc_tracker address 0xABCD... --db data/smc.db\n"
            "  python -m smc_tracker discover --top 20\n"
            "  python -m smc_tracker bench 300 3000\n"
            "  python -m smc_tracker llm --hours 6 --model gpt-5.4\n"
            "  python -m smc_tracker dashboard --port 8787\n"
            "  python -m smc_tracker health\n"
            "  python -m smc_tracker evaluate --hours 168\n"
            "  python -m smc_tracker cycle --push\n"
        ),
    )

    sub = ap.add_subparsers(dest="cmd", metavar="<子命令>")

    # ---- run ----
    p_run = sub.add_parser("run", help="启动流式 WebSocket 实时监控 app")
    p_run.add_argument(
        "--config", default=_DEFAULT_CONFIG, metavar="PATH",
        help=f"配置文件路径（默认 {_DEFAULT_CONFIG}）",
    )
    p_run.set_defaults(handler=_cmd_run)

    # ---- poll ----
    p_poll = sub.add_parser("poll", help="轮询监控（单次或 --loop 持续）")
    p_poll.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                        help="配置文件路径")
    p_poll.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                        help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_poll.add_argument("--loop", action="store_true", help="持续运行")
    p_poll.add_argument("--interval", type=float, default=3600.0, metavar="N",
                        help="轮询周期秒数（默认 3600）")
    p_poll.set_defaults(handler=_cmd_poll)

    # ---- report ----
    p_report = sub.add_parser("report", help="打印近 N 小时摘要日报")
    p_report.add_argument("--hours", type=float, default=24.0, metavar="H",
                          help="回看窗口小时数（默认 24）")
    p_report.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                          help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_report.set_defaults(handler=_cmd_report)

    # ---- address ----
    p_addr = sub.add_parser("address", help="完整追踪单个地址(画像+持仓+协同+对手方+轨迹)")
    p_addr.add_argument("addr", metavar="ADDR", help="Hyperliquid 地址（0x…）")
    p_addr.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                        help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_addr.add_argument("--hours", type=float, default=24.0, metavar="H",
                        help="协同/对手方/轨迹回看窗口小时数（默认 24）")
    p_addr.set_defaults(handler=_cmd_address)

    # ---- discover ----
    p_disc = sub.add_parser("discover", help="从排行榜自动发现聪明钱地址")
    p_disc.add_argument("--top", type=int, default=15, metavar="N",
                        help="最多返回 N 个地址（默认 15）")
    p_disc.set_defaults(handler=_cmd_discover)

    # ---- bench ----
    p_bench = sub.add_parser("bench", help="信号计算链路延迟基准（无网络）")
    p_bench.add_argument("bars", type=int, nargs="?", default=300,
                         help="K 线数量（默认 300）")
    p_bench.add_argument("iters", type=int, nargs="?", default=3000,
                         help="迭代次数（默认 3000）")
    p_bench.set_defaults(handler=_cmd_bench)

    # ---- llm ----
    p_llm = sub.add_parser("llm", help="LLM(Codex GPT-5.4) 抓庄研判（需 codex login）")
    p_llm.add_argument("--hours", type=float, default=6.0, metavar="H",
                       help="回看窗口小时数（默认 6）")
    p_llm.add_argument("--model", default="", metavar="M",
                       help="覆盖模型名（默认使用 codex 配置）")
    p_llm.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                       help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_llm.set_defaults(handler=_cmd_llm)

    # ---- dashboard ----
    p_dash = sub.add_parser("dashboard", help="启动 Web 仪表盘")
    p_dash.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                        help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_dash.add_argument("--host", default="127.0.0.1", metavar="H",
                        help="监听地址（默认 127.0.0.1）")
    p_dash.add_argument("--port", type=int, default=8787, metavar="P",
                        help="监听端口（默认 8787）")
    p_dash.set_defaults(handler=_cmd_dashboard)

    # ---- wallet ----
    p_wallet = sub.add_parser("wallet", help="打印单地址完整持仓画像（实时拉取）")
    p_wallet.add_argument("addr", metavar="ADDR", help="Hyperliquid 地址（0x…）")
    p_wallet.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                          help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_wallet.add_argument("--label", default="", metavar="LABEL",
                          help="自定义标签（可选）")
    p_wallet.add_argument("--top", type=int, default=12, metavar="N",
                          help="展示前 N 个持仓（默认 12）")
    p_wallet.add_argument("--rest-url", default="https://api.hyperliquid.xyz",
                          dest="rest_url", metavar="URL",
                          help="Hyperliquid REST URL（默认主网）")
    p_wallet.add_argument("--history", type=int, default=0, metavar="N",
                          help="拉取近 N 天历史成交并重建开仓时间（0=不拉历史，默认 0）")
    p_wallet.set_defaults(handler=_cmd_wallet)

    # ---- evaluate ----
    p_eval = sub.add_parser(
        "evaluate",
        help="评估所有到期前瞻预测并输出准确率回顾（可 cron 化，无需常驻 app）",
    )
    p_eval.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                        help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_eval.add_argument("--hours", type=float, default=168.0, metavar="H",
                        help="准确率报告回看窗口小时数（默认 168=7天）")
    p_eval.add_argument("--push", action="store_true",
                        help="若配置了推送渠道，推送准确率摘要")
    p_eval.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                        help=f"配置文件路径（--push 时使用，默认 {_DEFAULT_CONFIG}）")
    p_eval.set_defaults(handler=_cmd_evaluate)

    # ---- health ----
    p_health = sub.add_parser("health", help="系统健康检查（数据新鲜度+验证闭环，无网络）")
    p_health.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                          help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_health.add_argument("--stale-after", type=float, default=7200.0, metavar="S",
                          dest="stale_after",
                          help="超过 S 秒未更新判定 stale（默认 7200=2h）")
    p_health.set_defaults(handler=_cmd_health)

    # ---- cycle ----
    p_cycle = sub.add_parser(
        "cycle",
        help="单次闭环：采集(poll)+评估(evaluate)+合并推送，cron 友好无常驻进程",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "crontab 示例（每15分钟采集+评估+推送，驱动抓庄闭环）：\n"
            "  */15 * * * * cd \"/Volumes/ROG ESD-S1C Media/smc\" && "
            "PYTHONPATH=src ./.venv/bin/python -m smc_tracker cycle --push "
            ">> data/cycle.log 2>&1\n\n"
            "详细 crontab 配置见 scripts/crontab.example"
        ),
    )
    p_cycle.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                         help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_cycle.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                         help=f"配置文件路径（默认 {_DEFAULT_CONFIG}）")
    p_cycle.add_argument("--hours", type=float, default=168.0, metavar="H",
                         help="准确率报告回看窗口小时数（默认 168=7天）")
    p_cycle.add_argument("--push", action="store_true",
                         help="若配置了推送渠道，推送 poll digest + 准确率摘要合并消息")
    p_cycle.set_defaults(handler=_cmd_cycle)

    return ap


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI 入口：解析参数并 dispatch 到对应 handler。"""
    ap = build_parser()
    args = ap.parse_args()
    if args.cmd is None:
        ap.print_help()
        sys.exit(0)
    args.handler(args)


if __name__ == "__main__":
    main()
