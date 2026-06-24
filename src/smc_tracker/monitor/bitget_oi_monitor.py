"""BitgetOIMonitor：System 2 (Bitget) 的 meme OI 实时流监控。

数据源：Bitget 公共 WS `ticker` 频道（wss://ws.bitget.com/v2/ws/public，无需 API key）。
职责：
  1. 为每个 meme 永续 symbol 订阅 ticker；
  2. 从每条 ticker 解析 OI(持仓量 holdingAmount) / 资金费 / 标记价 → 缓冲批量落 SQLite(bitget_oi)；
  3. 维护内存中 per-symbol 最新 OI 快照，提供查询；
  4. 与上次 OI 比较，相对变化超过 surge_pct 记「OI 异动」并打印 + 触发可选回调；
  5. flush() 把缓冲批量 executemany 落库；
  6. oi_window() 纯内存读取 OI 历史窗口（A2b：替换 _on_structure 热路径磁盘查询）。

WS ticker 字段（已实证，wss://ws.bitget.com/v2/ws/public，channel=ticker）：
  data[0] = {instId, symbol, lastPr, markPrice, indexPrice, fundingRate,
             holdingAmount(=OI 币数), nextFundingTime, ts, ...}（全部为字符串）
  字段名与 REST ticker 完全一致，故复用 BitgetREST.parse_oi_row 解析。
"""
from __future__ import annotations

import logging
import math
from collections import deque
from typing import Any, Callable

from ..bitget import BitgetREST, BitgetSub
from ..storage import Store

log = logging.getLogger("monitor.bitget_oi")

# OI 异动回调签名：on_surge(event: dict) -> None；event 见 _ingest 构造结构
SurgeCallback = Callable[[dict[str, Any]], Any]


class BitgetOIMonitor:
    """通过 Bitget 公共 WS ticker 实时跟踪 meme 永续 OI 并检测异动。"""

    def __init__(
        self,
        symbols: list[str],
        symbol_to_coin: dict[str, str],
        ws: Any,                          # BitgetWSClient（duck-typed，便于测试注入假 WS）
        store: Store,
        surge_pct: float = 0.05,
        on_surge: SurgeCallback | None = None,
        flush_threshold: int = 100,
    ) -> None:
        self.symbols = list(symbols)
        self.symbol_to_coin = dict(symbol_to_coin)
        self.ws = ws
        self.store = store
        self.surge_pct = surge_pct
        self.on_surge = on_surge
        self.flush_threshold = flush_threshold

        # 待落库缓冲：row 顺序 = (symbol, coin, oi_size, oi_usd, mark_px, funding, ts)
        self._buffer: list[tuple] = []
        # symbol → 最新解析出的 OI 快照（含 oi_size/oi_usd/mark/funding/ts）
        self._latest: dict[str, dict[str, float | int]] = {}
        # symbol → 上一次用于比较的 OI(币数)，用于算异动
        self._prev_oi: dict[str, float] = {}
        # A2b：OI 历史窗口环形缓存（纯内存，替换 _on_structure 热路径磁盘 SELECT）
        # symbol → list[(ts_ms, oi_size)]；保留最近 1200s（2×600s 窗口）的数据点
        self._oi_window_data: dict[str, list[tuple[int, float]]] = {}
        # 超过此时长的 OI 历史点自动丢弃（节省内存，保留 2 倍窗口足够）
        self._oi_window_retain_ms: int = 1_200_000  # 1200s = 20 min
        # 统计
        self.ticks_seen = 0
        self.surges_seen = 0

    # ---- 挂载 ----
    def attach(self) -> None:
        """为每个 meme symbol 注册 ticker 订阅。ws.run() 前后调用均可。"""
        for symbol in self.symbols:
            self.ws.subscribe(
                BitgetSub(channel="ticker", inst_id=symbol), self._on_ticker
            )
        log.info("BitgetOIMonitor 已挂载 %d 个 meme symbol（ticker）", len(self.symbols))

    # ---- WS 回调 ----
    def _on_ticker(self, arg: dict, data: list, recv_ns: int) -> None:
        """data 为 list[ticker dict]；arg 含 instId/symbol。"""
        if not data:
            return
        symbol = arg.get("instId") or arg.get("symbol") or ""
        for tk in data:
            # ticker dict 自带 symbol/instId，优先用 data 内字段，回退到 arg
            sym = tk.get("symbol") or tk.get("instId") or symbol
            if not sym:
                continue
            self._ingest(sym, tk)

    def _ingest(self, symbol: str, tk: dict[str, Any]) -> None:
        """解析一条 ticker → 入缓冲 / 更新快照 / 检测异动。"""
        coin = self.symbol_to_coin.get(symbol, "")
        ts = _i(tk.get("ts"))
        # 复用 REST 的解析逻辑：字段名一致（holdingAmount/markPrice/lastPr/fundingRate）
        row = BitgetREST.parse_oi_row(symbol, coin, tk, ts)
        _, _, oi_size, oi_usd, mark_px, funding, _ = row
        if oi_size <= 0:
            return  # 无效 OI，跳过（避免污染异动基准）

        # 捕获最新价与 24h 涨跌幅（用于推送展示）
        last_px = _f(tk.get("lastPr"))
        chg24 = _f(tk.get("change24h"))

        self.ticks_seen += 1
        self._buffer.append(row)
        self._latest[symbol] = {
            "oi_size": oi_size,
            "oi_usd": oi_usd,
            "mark_px": mark_px,
            "funding": funding,
            "ts": ts,
            "last_px": last_px,   # 最新成交价（字符串转 float）
            "chg24": chg24,       # 24h 涨跌幅比率（如 0.00361 = +0.361%）
        }

        # OI 异动检测：与上次比较的相对变化
        prev = self._prev_oi.get(symbol)
        if prev is not None and prev > 0:
            change = (oi_size - prev) / prev
            if abs(change) >= self.surge_pct:
                self.surges_seen += 1
                evt = {
                    "symbol": symbol,
                    "coin": coin,
                    "prev_oi": prev,
                    "oi_size": oi_size,
                    "oi_usd": oi_usd,
                    "mark_px": mark_px,
                    "funding": funding,
                    "change": change,
                    "ts": ts,
                }
                dir_txt = "增" if change > 0 else "减"
                log.info(
                    "OI 异动 %s(%s) %s%.2f%%  OI %.0f→%.0f  OI≈$%.0f  funding=%+.4f%%",
                    symbol, coin, dir_txt, change * 100.0, prev, oi_size,
                    oi_usd, funding * 100.0,
                )
                if self.on_surge is not None:
                    try:
                        self.on_surge(evt)
                    except Exception:  # noqa: BLE001 — 回调异常不影响接收
                        log.exception("on_surge 回调出错")
        # 更新比较基准（无论是否触发异动）
        self._prev_oi[symbol] = oi_size

        # A2b：维护 OI 历史窗口（纯内存，供 oi_window() 热路径查询，替代磁盘 SELECT）
        if ts and oi_size > 0:
            window = self._oi_window_data.setdefault(symbol, [])
            window.append((ts, oi_size))
            # 剪裁过老数据（保留 retain_ms 以内的点，避免内存单调增长）
            if len(window) > 1:
                cutoff = ts - self._oi_window_retain_ms
                while window and window[0][0] < cutoff:
                    window.pop(0)

        # 注意：不在热路径内调用 maybe_flush()；落库由 app._periodic_flush(every=5s) 周期驱动

    # ---- 落库 ----
    def maybe_flush(self) -> int:
        """缓冲达阈值时落库；返回落库行数（0 表示未触发）。"""
        if len(self._buffer) >= self.flush_threshold:
            return self.flush()
        return 0

    def flush(self) -> int:
        """把缓冲批量 executemany 落库，清空缓冲；返回落库行数。"""
        if not self._buffer:
            return 0
        rows = self._buffer
        self._buffer = []
        self.store.insert_oi(rows)
        return len(rows)

    # ---- 查询（内存快照）----
    def latest(self, symbol: str) -> dict[str, float | int] | None:
        """某 symbol 内存中最新 OI 快照（oi_size/oi_usd/mark_px/funding/ts）。"""
        return self._latest.get(symbol)

    def latest_oi(self, symbol: str) -> float | None:
        """某 symbol 内存中最新 OI(币数)。"""
        snap = self._latest.get(symbol)
        return snap["oi_size"] if snap else None  # type: ignore[return-value]

    def all_latest(self) -> dict[str, dict[str, float | int]]:
        """全部 symbol 的最新 OI 快照（拷贝）。"""
        return {k: dict(v) for k, v in self._latest.items()}

    def price_change(self, symbol: str) -> tuple[float, float] | None:
        """某 symbol 的 (最新价, 24h 涨幅比率)；无数据返回 None。

        最新价优先取 lastPr，回退到 markPrice；比率如 0.00361 = +0.361%。
        """
        snap = self._latest.get(symbol)
        if not snap:
            return None
        px = snap.get("last_px") or snap.get("mark_px") or 0.0
        if px <= 0:
            return None
        return float(px), float(snap.get("chg24") or 0.0)

    def ticker(self, symbol: str) -> dict | None:
        """返回某 symbol 的行情快照字典；price<=0 或无快照时返回 None。

        返回 {"price": float, "chg24": float, "funding": float, "oi_usd": float}。
        price 优先取 last_px，回退到 mark_px。
        chg24/funding 是比率（如 0.0001=0.01%，0.0036=+0.36%）。
        """
        snap = self._latest.get(symbol)
        if not snap:
            return None
        price = float(snap.get("last_px") or snap.get("mark_px") or 0.0)
        if price <= 0:
            return None
        return {
            "price": price,
            "chg24": float(snap.get("chg24") or 0.0),
            "funding": float(snap.get("funding") or 0.0),
            "oi_usd": float(snap.get("oi_usd") or 0.0),
        }

    def board_rows(self) -> list[dict]:
        """遍历所有 symbol 的最新快照，返回行情板行列表，按 abs(chg24) 降序。

        每行结构：{"symbol", "coin", "price", "chg24", "funding", "oi_usd"}。
        price<=0 的 symbol 被过滤掉（无有效行情）。
        """
        rows: list[dict] = []
        for symbol, snap in self._latest.items():
            price = float(snap.get("last_px") or snap.get("mark_px") or 0.0)
            if price <= 0:
                continue
            rows.append({
                "symbol": symbol,
                "coin": self.symbol_to_coin.get(symbol, ""),
                "price": price,
                "chg24": float(snap.get("chg24") or 0.0),
                "funding": float(snap.get("funding") or 0.0),
                "oi_usd": float(snap.get("oi_usd") or 0.0),
            })
        # 按 abs(chg24) 降序排列，涨跌幅最大的排前
        rows.sort(key=lambda r: abs(r["chg24"]), reverse=True)
        return rows

    def oi_window(
        self, symbol: str, window_ms: int, now_ms: int
    ) -> tuple[float, float | None] | None:
        """纯内存 OI 窗口查询（A2b：替代 _on_structure 热路径磁盘 SELECT）。

        返回 (latest_oi, past_oi)：
          - latest_oi：最新一条 oi_size（_oi_window_data 最后一条）。
          - past_oi：窗口边界前（≤ now_ms - window_ms）最近一条 oi_size；无历史则为 None。
        无数据（未收到该 symbol 任何 tick）→ 返回 None。
        与旧 db.oi_change(symbol, window_ms, now_ms) 语义一致，可直接替换。
        守卫 `if chg and chg[1]` 兼容 past=None。
        """
        window = self._oi_window_data.get(symbol)
        if not window:
            return None

        # latest = 最后一个点
        latest_oi = window[-1][1]

        # past = 窗口边界前（ts ≤ now_ms - window_ms）最近一条（从右往左找）
        boundary = now_ms - window_ms
        past_oi: float | None = None
        for ts, oi in reversed(window):
            if ts <= boundary:
                past_oi = oi
                break

        return (latest_oi, past_oi)


from ..util import to_float as _f  # 统一安全数值解析


def _i(x: Any, default: int = 0) -> int:
    """安全转 int（ts 为字符串 ms epoch）。"""
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default
