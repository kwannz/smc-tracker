"""聪明钱(庄)地址自动发现 —— 抓庄的前提。

从 Hyperliquid 公开排行榜(stats-data.hyperliquid.xyz)挑出「真·聪明钱」：
账户规模够大(过滤散户/噪声) + 全期盈利可观 + 近月仍在盈利(还活跃且在赢)。
这些地址即监控目标，AddressMonitor 订阅其 userFills/webData2，实时抓他们的每一笔动作。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp
import orjson

from ..config import WatchAddress

log = logging.getLogger("whale")

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# 排行榜 JSON 体积大(已实测 ~16.8MB)且服务端慢且不稳定(实测 66s→148s,偶 >180s)：分离「连接超时」
# (快失败,真网络阻断 10s 内报错) 与「总读超时」(300s,容忍极慢大 payload)；请求 gzip 压缩。
# 历史:旧 60s→discover 必超时(#41);180s 偶仍被 148s+ 慢端点击穿致整轮 poll 报废(#56)→ 提至 300s + 缓存回退。
_LB_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=10)
_LB_HEADERS = {"User-Agent": "smc-tracker", "Accept-Encoding": "gzip"}
# 持久缓存(跨 cron 进程):排行榜慢/失败时回退上次成功结果,慢端点不再让整轮 poll 报废。
# 庄列表小时级稳定,回退陈旧数据可接受;cron 单次进程退出后内存缓存无效,故落盘。
_LB_CACHE = Path(__file__).resolve().parents[3] / "data" / "leaderboard_cache.json"


async def fetch_leaderboard_rows() -> list[dict[str, Any]]:
    """拉取 Hyperliquid 排行榜并返回 leaderboardRows（抓庄发现 + PnL 动量共用，去重）。

    成功即写盘缓存；失败(超时/网络/HTTP)回退到上次缓存(即便陈旧)，慢且不稳定的 stats-data
    端点不再让整轮 poll/discover 报废(#56)。仅当从未成功过(无缓存)才向上抛异常。
    """
    try:
        async with aiohttp.ClientSession(timeout=_LB_TIMEOUT) as s:
            async with s.get(LEADERBOARD, headers=_LB_HEADERS) as resp:
                resp.raise_for_status()
                raw = await resp.read()
        rows = orjson.loads(raw).get("leaderboardRows") or []
        try:                                            # 写盘缓存(best-effort,不影响主流程)
            _LB_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _LB_CACHE.write_bytes(orjson.dumps(rows))
        except Exception as e:  # noqa: BLE001
            log.debug("排行榜缓存写盘失败: %s", e)
        return rows
    except Exception as exc:  # noqa: BLE001 — 拉取失败回退缓存
        try:
            cached = orjson.loads(_LB_CACHE.read_bytes())
        except Exception:  # noqa: BLE001 — 无缓存可回退
            log.warning("排行榜拉取失败且无缓存可回退: %s", type(exc).__name__)
            raise exc
        log.warning("排行榜拉取失败(%s)，回退缓存 %d 行（庄列表小时级稳定，可接受）",
                    type(exc).__name__, len(cached))
        return cached


def _window(row: dict[str, Any], name: str) -> float:
    """取某时间窗的 pnl（P2简化：roi 被所有调用方丢弃，直接返回 float）。"""
    for w in row.get("windowPerformances", []):
        if w and len(w) >= 2 and w[0] == name:        # 长度守卫：防裸下标 w[1] 越界
            try:
                return float(w[1].get("pnl", 0))
            except (TypeError, ValueError, AttributeError, IndexError):
                return 0.0
    return 0.0


def rank_smart_money(
    rows: list[dict[str, Any]],
    top_n: int = 15,
    min_account_value: float = 300_000.0,
    min_alltime_pnl: float = 500_000.0,
    require_month_positive: bool = True,
    notional_alert_usd: float = 50_000.0,
) -> list[WatchAddress]:
    """纯筛选/排名：账户够大 + 全期盈利 + 近月仍盈利 → 按全期 PnL 降序取 top_n。"""
    cand: list[tuple[float, float, str]] = []
    for r in rows:
        addr = r.get("ethAddress")
        if not addr:
            continue
        try:
            av = float(r.get("accountValue", 0) or 0)
        except (TypeError, ValueError):
            continue
        at_pnl = _window(r, "allTime")
        m_pnl = _window(r, "month")
        if av < min_account_value or at_pnl < min_alltime_pnl:
            continue
        if require_month_positive and m_pnl <= 0:
            continue
        cand.append((at_pnl, av, addr))
    cand.sort(reverse=True)
    return [
        WatchAddress(address=addr, label=f"庄#{i}(PnL${pnl/1e6:.0f}M)",
                     notional_alert_usd=notional_alert_usd)
        for i, (pnl, av, addr) in enumerate(cand[:top_n], 1)
    ]


async def discover_smart_money(top_n: int = 15, **kw: Any) -> list[WatchAddress]:
    """拉取排行榜并返回排名靠前的聪明钱地址。"""
    rows = await fetch_leaderboard_rows()
    out = rank_smart_money(rows, top_n=top_n, **kw)
    log.info("发现聪明钱(庄)地址 %d 个（从 %d 行排行榜筛选）", len(out), len(rows))
    return out
