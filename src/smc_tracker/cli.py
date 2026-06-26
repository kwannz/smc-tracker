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
from typing import Any

# 13 个独立子命令 handler 已拆到 cli_commands（build_parser/main + 测试经此名可达，向后兼容）
from .cli_commands import (  # noqa: F401
    _cmd_run, _cmd_report, _cmd_signals, _cmd_vol, _cmd_watch, _cmd_address,
    _cmd_discover, _cmd_bench, _cmd_llm, _cmd_wallet, _cmd_dashboard,
    _cmd_okx, _cmd_health, _cmd_backtest, _cmd_mtf,
)

# 项目根：src/smc_tracker/cli.py → parents[2] = repo 根
_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = str(_ROOT / "data" / "smc.db")
_DEFAULT_CONFIG = str(_ROOT / "config" / "config.yaml")


# ---------------------------------------------------------------------------
# 子命令处理器
# ---------------------------------------------------------------------------

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

    # ---- signals ----
    p_signals = sub.add_parser(
        "signals",
        help="打印近 N 小时全信号汇总（11 张信号表按类型分组，含证据摘要，无网络）",
    )
    p_signals.add_argument("--hours", type=float, default=24.0, metavar="H",
                           help="回看窗口小时数（默认 24）")
    p_signals.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                           help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_signals.set_defaults(handler=_cmd_signals)

    # ---- address ----
    p_addr = sub.add_parser("address", help="完整追踪单个地址(画像+持仓+协同+对手方+轨迹)")
    p_addr.add_argument("addr", metavar="ADDR", help="Hyperliquid 地址（0x…）")
    p_addr.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                        help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_addr.add_argument("--hours", type=float, default=24.0, metavar="H",
                        help="协同/对手方/轨迹回看窗口小时数（默认 24）")
    p_addr.set_defaults(handler=_cmd_address)

    # ---- watch（监控币种清单，驱动多周期采集，热载入）----
    p_watch = sub.add_parser("watch", help="监控币种清单增删查（驱动多周期采集，热载入）")
    watch_sub = p_watch.add_subparsers(dest="action", metavar="<add|rm|list>", required=True)
    _w_add = watch_sub.add_parser("add", help="加入币种（如 watch add BTC ETH）")
    _w_add.add_argument("coins", nargs="+", metavar="COIN", help="币种符号（如 BTC ETH）")
    _w_add.add_argument("--note", default="", metavar="N", help="可选备注（为什么加）")
    _w_add.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                        help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    _w_rm = watch_sub.add_parser("rm", help="移除币种（如 watch rm BTC）")
    _w_rm.add_argument("coins", nargs="+", metavar="COIN", help="币种符号")
    _w_rm.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                       help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    _w_list = watch_sub.add_parser("list", help="打印当前监控清单")
    _w_list.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                         help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_watch.set_defaults(handler=_cmd_watch)

    # ---- vol（实时波动追踪板）----
    p_vol = sub.add_parser("vol", help="实时波动追踪板（监控清单币按速度+加速度排序，读 DB 无网络）")
    p_vol.add_argument("--tf", default="15m", metavar="TF",
                       help="周期（逗号分隔多个，逐周期展示，默认 15m）")
    p_vol.add_argument("--top", type=int, default=15, metavar="N", help="最多展示 N 币（默认 15）")
    p_vol.add_argument("--skill", action="store_true",
                       help="生产 alpha 验证(#182):实测自己追踪币上 GARCH/EWMA 预测技巧(corr 预测vs已实现波动)")
    p_vol.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                       help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_vol.set_defaults(handler=_cmd_vol)

    p_bt = sub.add_parser("backtest", help="回测交易机器人(SMC结构信号·freqtrade式绩效·keyless无实盘·读DB无网络)")
    p_bt.add_argument("--tf", default="1H", metavar="TF", help="回测周期（默认 1H）")
    p_bt.add_argument("--bars", type=int, default=2000, metavar="N", help="回测 K 线数（默认 2000）")
    p_bt.add_argument("--rr", type=float, default=2.0, metavar="R", help="目标盈亏比（默认 2.0）")
    p_bt.add_argument("--require-zone", action="store_true", help="要求 OB/FVG 区域共振过滤")
    p_bt.add_argument("--require-sweep", action="store_true", help="要求流动性扫荡共振过滤")
    p_bt.add_argument("--harmonic", action="store_true", help="回测谐波 setup(no-repaint 增量重放,#165 edge)而非 SMC 结构")
    p_bt.add_argument("--min-conf", type=float, default=0.0, metavar="C",
                      help="谐波回测最低置信(对齐推送门控 0.75;默认 0=全收)")
    p_bt.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                      help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_bt.set_defaults(handler=_cmd_backtest)

    p_mtf = sub.add_parser("mtf", help="MTF 分层入场决策(顶12h+1d定向·中1h+4h确认·底5m+15m触发,读DB无网络)")
    p_mtf.add_argument("--db", default=_DEFAULT_DB, metavar="PATH",
                       help=f"SQLite 数据库路径（默认 {_DEFAULT_DB}）")
    p_mtf.set_defaults(handler=_cmd_mtf)

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

    # ---- okx ----
    p_okx = sub.add_parser("okx", help="OKX 永续实时 streaming：净流向 + OI 异动")
    p_okx.add_argument("--top", type=int, default=10, metavar="N",
                       help="按 OI 排名监控前 N 个永续（默认 10）")
    p_okx.add_argument("--secs", type=float, default=15.0, metavar="S",
                       help="订阅持续秒数（默认 15）")
    p_okx.set_defaults(handler=_cmd_okx)

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
