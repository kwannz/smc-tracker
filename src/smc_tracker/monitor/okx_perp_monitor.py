"""OKXPerpMonitor：System 3 (OKX) 永续实时流监控（WS trades 净流向 + OI 异动）。

数据源：OKX V5 公共 WS（wss://ws.okx.com:8443/ws/v5/public，无 API key）。
职责：
  1. trades(带 side)：累计 per-coin 净主动流向（名义 USD = sz张 × ctVal × px；buy 正卖负）；
  2. open-interest：oiCcy(币数)/oiUsd(美元) OI，相对变化越 surge_pct 记异动 + 缓冲落库；
  3. tickers：维护最新价快照；
  4. flush() 批量 executemany 落 SQLite(okx_perp)。

ctVal 关键：OKX SWAP 的 trades.sz 单位是合约张数，名义须乘 ctVal（BTC=0.01/ETH=0.1/DOGE=1000），
否则净流向口径错 100×。ctVal 由 OKXClient.swap_meta() 提供。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

from ..okx.ws_client import OKXSub

log = logging.getLogger("monitor.okx_perp")

# OI 异动回调签名：on_surge(event: dict) -> None
SurgeCallback = Callable[[dict[str, Any]], Any]


class OKXPerpMonitor:
    """通过 OKX 公共 WS 实时跟踪永续 trades 净流向 + OI 异动。"""

    def __init__(
        self,
        inst_ids: list[str],
        inst_to_coin: dict[str, str],
        ct_val: dict[str, float],
        ws: Any,                          # OKXWSClient（duck-typed，便于测试注入假 WS）
        store: Any = None,                # Store（duck-typed；None 时不落库）
        surge_pct: float = 0.05,
        on_surge: SurgeCallback | None = None,
        flush_threshold: int = 100,
    ) -> None:
        self.inst_ids = list(inst_ids)
        self.inst_to_coin = dict(inst_to_coin)
        self.ct_val = dict(ct_val)
        self.ws = ws
        self.store = store
        self.surge_pct = surge_pct
        self.on_surge = on_surge
        self.flush_threshold = flush_threshold

        # 待落库缓冲：row = (inst_id, coin, oi_ccy, oi_usd, mark_px, funding, net_flow, ts)
        self._buffer: list[tuple] = []
        # coin → 净主动流向名义 USD（自启动累计；买正卖负）
        self._net_flow: dict[str, float] = defaultdict(float)
        # inst_id → 最新快照（oi_ccy/oi_usd/mark_px/funding/ts/coin）
        self._latest: dict[str, dict[str, Any]] = {}
        # inst_id → 上次用于比较的 OI(币数)，算异动
        self._prev_oi: dict[str, float] = {}
        # 统计
        self.trades_seen = 0
        self.surges_seen = 0

    # ---- 挂载 ----
    def attach(self) -> None:
        """为每个 inst 订阅 trades + open-interest + tickers + funding-rate。ws.run() 前后调用均可。"""
        for inst in self.inst_ids:
            self.ws.subscribe(OKXSub("trades", inst), self._on_trades)
            self.ws.subscribe(OKXSub("open-interest", inst), self._on_oi)
            self.ws.subscribe(OKXSub("tickers", inst), self._on_ticker)
            self.ws.subscribe(OKXSub("funding-rate", inst), self._on_funding)
        log.info("OKXPerpMonitor 已挂载 %d 个永续（trades+OI+tickers+funding-rate）", len(self.inst_ids))

    # ---- WS 回调 ----
    def _on_trades(self, arg: dict, data: list, recv_ns: int) -> None:
        """逐笔成交 → per-coin 净主动流向（名义 = sz张 × ctVal × px）。"""
        inst = arg.get("instId", "")
        coin = self.inst_to_coin.get(inst, inst)
        ctv = self.ct_val.get(inst, 1.0)
        for t in data:
            sz = _f(t.get("sz"))
            px = _f(t.get("px"))
            if sz <= 0 or px <= 0:
                continue
            notional = sz * ctv * px
            self.trades_seen += 1
            self._net_flow[coin] += notional if t.get("side") == "buy" else -notional

    def _on_oi(self, arg: dict, data: list, recv_ns: int) -> None:
        """open-interest 推送 → 更新快照 + 入缓冲 + 异动检测。"""
        inst = arg.get("instId", "")
        coin = self.inst_to_coin.get(inst, inst)
        for d in data:
            oi_ccy = _f(d.get("oiCcy"))
            oi_usd = _f(d.get("oiUsd"))
            ts = _i(d.get("ts"))
            if oi_ccy <= 0:
                continue  # 无效 OI，跳过（避免污染异动基准）
            snap = self._latest.setdefault(inst, {})
            snap.update(oi_ccy=oi_ccy, oi_usd=oi_usd, ts=ts, coin=coin)
            self._buffer.append((
                inst, coin, oi_ccy, oi_usd,
                _f(snap.get("mark_px")), _f(snap.get("funding")),
                self._net_flow.get(coin, 0.0), ts,
            ))
            prev = self._prev_oi.get(inst)
            if prev is not None and prev > 0:
                change = (oi_ccy - prev) / prev
                if abs(change) >= self.surge_pct:
                    self.surges_seen += 1
                    evt = {
                        "inst_id": inst, "coin": coin, "prev_oi": prev,
                        "oi_ccy": oi_ccy, "oi_usd": oi_usd, "change": change, "ts": ts,
                    }
                    log.info("OKX OI 异动 %s(%s) %s%.2f%%  OI %.0f→%.0f  ≈$%.0f",
                             inst, coin, "增" if change > 0 else "减",
                             change * 100.0, prev, oi_ccy, oi_usd)
                    if self.on_surge is not None:
                        try:
                            self.on_surge(evt)
                        except Exception:  # noqa: BLE001 — 回调异常不影响接收
                            log.exception("on_surge 回调出错")
            self._prev_oi[inst] = oi_ccy

    def _on_funding(self, arg: dict, data: list, recv_ns: int) -> None:
        """funding-rate 推送 → 更新快照 funding 字段（字符串 fundingRate → float）。"""
        inst = arg.get("instId", "")
        for d in data:
            rate = _f(d.get("fundingRate"))
            snap = self._latest.setdefault(inst, {})
            snap["funding"] = rate

    def _on_ticker(self, arg: dict, data: list, recv_ns: int) -> None:
        """tickers 推送 → 维护最新价快照（OKX tickers 字段 last）。"""
        inst = arg.get("instId", "")
        for tk in data:
            px = _f(tk.get("last") or tk.get("markPx"))
            if px <= 0:
                continue
            snap = self._latest.setdefault(inst, {})
            snap["mark_px"] = px

    # ---- 落库 ----
    def maybe_flush(self) -> int:
        if len(self._buffer) >= self.flush_threshold:
            return self.flush()
        return 0

    def flush(self) -> int:
        """缓冲批量落库，清空缓冲；返回落库行数。store=None 时仅清空缓冲。"""
        if not self._buffer:
            return 0
        rows = self._buffer
        self._buffer = []
        if self.store is not None:
            self.store.insert_okx_perp(rows)
        return len(rows)

    # ---- 查询 ----
    def net_flow(self, coin: str) -> float:
        return self._net_flow.get(coin, 0.0)

    def all_net_flows(self) -> dict[str, float]:
        return dict(self._net_flow)

    def latest(self, inst_id: str) -> dict[str, Any] | None:
        return self._latest.get(inst_id)

    def all_latest(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._latest.items()}


from ..util import to_float as _f  # 统一安全数值解析


def _i(x: Any, default: int = 0) -> int:
    """安全转 int（ts 为字符串 ms epoch）。"""
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default
