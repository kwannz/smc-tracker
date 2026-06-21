"""系统健康检查：数据新鲜度 + 验证闭环评估积压 + 表覆盖。

回答审计第一问「能否追踪数据」——一眼判断采集是否还活着、各表数据是否新鲜、
预测验证闭环是否在按期评估。纯 SQLite 查询（无网络），确定性可测。

被 CLI `health` 子命令（按需）+ app `_periodic_health`（周期，异常才推送）调用。
此前数据停滞 7h、cron 未跑、到期预测无人评估均需人工排查；本模块将其自动化为可推送告警。

HealthMonitor：绑定 TradingSystem app，提供 snapshot(now_ms)->dict 和 fmt(now_ms)->str，
聚合数据新鲜度（复用 system_health）+ WS 连接状态 + 延迟统计 + 内存累积器大小
→ 中文多行摘要（供周期推送/控制台）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .util import fmt_ts

if TYPE_CHECKING:
    # 避免循环导入：仅类型标注用
    from .app import TradingSystem

# (表, 时间列)：覆盖采集核心表（双所行情 + 聪明钱事件 + 共识 + 持仓 + PnL + 预测 + 钱包持仓）。
# 缺表/空表安全降级，不抛异常。各 ts 均为 epoch ms。
_FRESHNESS_TABLES: list[tuple[str, str]] = [
    ("bitget_oi", "ts"),
    ("hl_meme_trades", "time_ms"),
    ("sm_events", "ts"),
    ("consensus", "ts"),
    ("whale_positions", "ts"),
    ("whale_pnl_snapshots", "ts"),
    ("predictions", "ts"),
    ("wallet_positions_full", "ts"),
]


def _existing_tables(conn: Any) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _leaderboard_cache_status(now_ms: int, stale_after_s: float) -> dict:
    """排行榜缓存(抓庄发现源)新鲜度。stats-data 端点降速/挂时靠此缓存回退(#56)，
    缓存过旧=发现源已持续失败数小时，庄列表过时→追踪降级。信息性，不门控总体 ok。"""
    try:
        import os

        from .monitor.whale_discovery import _LB_CACHE
        if not _LB_CACHE.exists():
            return {"exists": False, "age_s": None, "stale": True}
        age_s = max(0.0, now_ms / 1000.0 - os.path.getmtime(_LB_CACHE))
        return {"exists": True, "age_s": age_s, "stale": age_s > stale_after_s}
    except Exception:  # noqa: BLE001 — 健康检查不应因附属探测失败而抛
        return {"exists": False, "age_s": None, "stale": True}


def system_health(store: Any, now_ms: int, stale_after_s: float = 7200.0) -> dict:
    """计算系统健康快照（纯 DB，无网络）。

    stale_after_s：超过此秒数未更新即判定 stale（默认 2h，匹配小时级轮询节奏留余量）。
    返回 dict：
      ok          总体健康（≥1 张核心表新鲜 且 无到期未评估预测）
      now_dt      生成时间（可读）
      freshness   [{table, exists, n, age_s, latest_dt, stale}]
      predictions {total, evaluated, pending, overdue}
    """
    conn = store.conn
    tables = _existing_tables(conn)

    freshness: list[dict] = []
    any_fresh = False
    for tbl, tcol in _FRESHNESS_TABLES:
        if tbl not in tables:
            freshness.append({"table": tbl, "exists": False, "n": 0,
                              "age_s": None, "latest_dt": None, "stale": True})
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        mx = conn.execute(f"SELECT MAX({tcol}) FROM {tbl}").fetchone()[0]
        if not mx:
            # 表存在但无（有效）时间戳：空表算非新鲜但不算缺失
            freshness.append({"table": tbl, "exists": True, "n": n,
                              "age_s": None, "latest_dt": None, "stale": n > 0})
            continue
        age_s = (now_ms - int(mx)) / 1000.0
        stale = age_s > stale_after_s
        if not stale:
            any_fresh = True
        freshness.append({"table": tbl, "exists": True, "n": n, "age_s": age_s,
                          "latest_dt": fmt_ts(int(mx)), "stale": stale})

    pred = {"total": 0, "evaluated": 0, "pending": 0, "overdue": 0}
    if "predictions" in tables:
        c = conn.execute
        pred["total"] = c("SELECT COUNT(*) FROM predictions").fetchone()[0]
        pred["evaluated"] = c(
            "SELECT COUNT(*) FROM predictions WHERE evaluated=1").fetchone()[0]
        pred["pending"] = c(
            "SELECT COUNT(*) FROM predictions WHERE evaluated=0").fetchone()[0]
        # 到期未评估：预测已过 horizon 但没人评估 → 轮询/评估管线停滞的强信号
        pred["overdue"] = c(
            "SELECT COUNT(*) FROM predictions WHERE evaluated=0 AND ?>=ts+horizon_ms",
            (now_ms,)).fetchone()[0]

    # 排行榜缓存(抓庄发现源)新鲜度：阈值更宽(庄列表小时级稳定)，信息性不门控 ok
    lb_cache = _leaderboard_cache_status(now_ms, stale_after_s=max(stale_after_s, 14_400.0))

    ok = any_fresh and pred["overdue"] == 0
    # overall 字符串：供 HealthMonitor/dashboard 判级（无 WS 信息，仅 DB 维度）。
    # 无任何新鲜表 → 'down'；有过期表或预测积压 → 'degraded'；否则 'ok'。
    any_stale = any(f["stale"] for f in freshness if f["exists"] and f["age_s"] is not None)
    if not any_fresh:
        overall_str = "down"
    elif any_stale or pred["overdue"] > 0:
        overall_str = "degraded"
    else:
        overall_str = "ok"
    return {
        "ok": ok,
        "overall": overall_str,
        "now_dt": fmt_ts(now_ms),
        "stale_after_s": stale_after_s,
        "freshness": freshness,
        "predictions": pred,
        "leaderboard_cache": lb_cache,
    }


class HealthMonitor:
    """绑定 TradingSystem app，周期聚合系统健康快照。

    设计原则：弱耦合（仅读 app 公开属性），不依赖私有实现；
    异常全部 try/except 吞掉（健康检查本身不能崩）。
    DB 新鲜度部分直接复用 system_health()（唯一真相源），避免重复逻辑。
    """

    def __init__(self, app: "TradingSystem") -> None:
        self._app = app

    def snapshot(self, now_ms: int) -> dict:
        """聚合完整健康快照 dict。

        包含：
          generated   生成时间（可读）
          db          system_health() 完整 DB 报告（freshness/predictions/leaderboard_cache/ok/overall）
          ws          WS 连接状态 {hl_connected, hl_running, bg_running}
          latency     {stage: stats} （来自 LatencyTracker.stats()）
          memory      {whale_acc, seen_clusters, candles_coins, bg_tasks}
          overall     'ok' / 'degraded' / 'down'（DB overall + WS 状态合并）
        """
        app = self._app

        # ---- DB 新鲜度（唯一真相源：复用 system_health）----
        db: dict = {}
        try:
            db = system_health(app.store, now_ms)
        except Exception:  # noqa: BLE001
            pass

        # ---- WS 连接状态 ----
        ws: dict[str, Any] = {"hl_connected": False, "hl_running": False, "bg_running": False}
        try:
            ws["hl_connected"] = bool(app.hl_ws._connected_evt.is_set())
            ws["hl_running"] = bool(app.hl_ws._running)
        except Exception:  # noqa: BLE001
            pass
        try:
            ws["bg_running"] = bool(app.bg_ws._running)
        except Exception:  # noqa: BLE001
            pass

        # ---- 延迟统计 ----
        latency: dict[str, Any] = {}
        try:
            for stage in ("接收→处理",):
                st = app.latency.stats(stage)
                if st is not None:
                    latency[stage] = st
        except Exception:  # noqa: BLE001
            pass

        # ---- 内存累积器 ----
        memory: dict[str, Any] = {
            "whale_acc": 0, "seen_clusters": 0, "candles_coins": 0, "bg_tasks": 0,
        }
        try:
            memory["whale_acc"] = len(app._whale_acc)
            memory["seen_clusters"] = len(app._seen_clusters)
            memory["candles_coins"] = len(app._candles)
            memory["bg_tasks"] = len(app._bg_tasks)
        except Exception:  # noqa: BLE001
            pass

        # ---- 综合判级（DB overall + WS）----
        db_overall = db.get("overall", "down")
        hl_degraded = ws.get("hl_running") and not ws.get("hl_connected")
        if db_overall == "down":
            overall = "down"
        elif db_overall == "degraded" or hl_degraded:
            overall = "degraded"
        else:
            overall = "ok"

        return {
            "generated": fmt_ts(now_ms),
            "db": db,
            "ws": ws,
            "latency": latency,
            "memory": memory,
            "overall": overall,
        }

    def fmt(self, now_ms: int) -> str:
        """中文多行摘要，供周期推送/控制台打印。带 emoji ✅/⚠️/🔴 区分状态。

        DB 段复用 fmt_health()（唯一渲染源），追加运行时（WS/延迟/内存）段。
        """
        snap = self.snapshot(now_ms)
        overall = snap["overall"]
        mark = {"ok": "✅", "degraded": "⚠️", "down": "🔴"}.get(overall, "⚠️")
        lines = [f"{mark} 系统健康[{overall}] {snap['generated']}"]

        # DB 新鲜度段（复用 fmt_health，去掉首行标题避免重复）
        db = snap.get("db", {})
        if db:
            db_text = fmt_health(db)
            # 跳过 fmt_health 的第一行（健康状态标题），我们自己已输出
            db_lines = db_text.splitlines()
            lines.extend(db_lines[1:] if len(db_lines) > 1 else db_lines)

        # WS 状态
        ws = snap["ws"]
        hl_ok = ws.get("hl_connected") and ws.get("hl_running")
        ws_mark = "✅" if hl_ok else "⚠️"
        lines.append(f"【WS】 {ws_mark} HL 连接={'✅' if ws.get('hl_connected') else '❌'} "
                     f"意图运行={'✅' if ws.get('hl_running') else '❌'} "
                     f"Bitget 运行={'✅' if ws.get('bg_running') else '❌'}")

        # 延迟
        lat = snap["latency"]
        if lat:
            for stage, st in lat.items():
                lines.append(f"【延迟】{stage} P50={st['p50']:.2f}ms P99={st['p99']:.2f}ms "
                             f"max={st['max']:.2f}ms n={st['n']}")
        else:
            lines.append("【延迟】尚无统计数据")

        # 内存
        mem = snap["memory"]
        lines.append(f"【内存】跟庄累积器{mem['whale_acc']}键 "
                     f"集群签名{mem['seen_clusters']}条 "
                     f"K线缓冲{mem['candles_coins']}币 "
                     f"后台任务{mem['bg_tasks']}个")

        return "\n".join(lines)


def fmt_health(report: dict) -> str:
    """把 system_health() 的 dict 渲染成中文文本（CLI 打印 / 频道推送）。"""
    ok = report.get("ok")
    lines = [("✅ 系统健康" if ok else "⚠️ 系统健康告警")
             + f" [{report.get('now_dt', '')}]"]

    lines.append("【数据新鲜度】")
    for f in report.get("freshness", []):
        tbl = f["table"]
        if not f["exists"]:
            lines.append(f"  ❌ {tbl}: 表不存在")
        elif f["age_s"] is None:
            lines.append(f"  ⚪ {tbl}: 空表（{f['n']}行）")
        else:
            mark = "⚠️" if f["stale"] else "✅"
            lines.append(f"  {mark} {tbl}: {f['n']}行 最新 {f['latest_dt']} "
                         f"({f['age_s'] / 3600:.1f}h前)")

    p = report.get("predictions", {})
    lines.append(
        f"【验证闭环】预测 {p.get('total', 0)} 条 · 已评估 {p.get('evaluated', 0)} · "
        f"待评估 {p.get('pending', 0)} · 到期未评 {p.get('overdue', 0)}")
    if p.get("overdue", 0) > 0:
        lines.append("  ⚠️ 有到期未评估预测 → 轮询/评估管线可能停滞（cron 未跑？）")

    lb = report.get("leaderboard_cache")
    if lb is not None:
        if not lb.get("exists"):
            lines.append("【抓庄发现源】⚪ 排行榜缓存未建立（尚未成功拉取过排行榜）")
        else:
            mark = "⚠️" if lb.get("stale") else "✅"
            age_h = (lb.get("age_s") or 0) / 3600
            lines.append(f"【抓庄发现源】{mark} 排行榜缓存 {age_h:.1f}h 前"
                         + ("（过旧→stats-data 端点可能持续失败，庄列表过时）" if lb.get("stale") else ""))

    return "\n".join(lines)
