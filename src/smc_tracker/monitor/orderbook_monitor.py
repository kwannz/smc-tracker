"""HLOrderbookMonitor：Hyperliquid l2Book WS 大额挂单墙动态监控（领先信号）。

第一性原理（前瞻 > 回看，CLAUDE.md #1）：
- 挂单是「尚未成交的意图」——大额挂单墙出现(build) = 资金已就位但还没动，**先于成交**。
- 抽单(pull) = 意图撤销/诱多诱空收网，亦是动态信号。

数据源：HL l2Book WS（wss://api.hyperliquid.xyz/ws）。
推送 data = {coin, time(ms), levels: [bids, asks]}，每档 = {px(str), sz(str), n(int 订单数)}，
每侧 20 档，sz 逐推变化（可追踪动态）。

HL l2Book 格式实证（2026-06-24，REST 验证）：
- px/sz 均为字符串，需 to_float 解析（不裸下标）。
- sz 单位：coin 数量（非 USD）。
- bids 降序（最高买价在前）；asks 升序（最低卖价在前）。

诚实定位（不夸大）：
- 挂单墙 = **意图告警**，可能是 spoof（虚挂诱导）/冰山，**非确定方向**。
- bid 墙 = 支撑/吸筹意图；ask 墙 = 压制/分销意图。仅供前瞻参考，须与成交/OI 交叉验证。
- C.5 抗 spoof：存活时间过滤（< min_lifetime_ms 不 emit）+ build/pull 横跳计数（≥ max_flap 标记 spoof）。

职责：
  1. detect_walls：纯函数，从单侧档位识别 notional 远超均值的大墙；
  2. _on_l2book：与上一帧对比，识别墙的出现(build)/抽单(pull) → 回调 + 缓冲落库；
     C.5: build 先记录首现 ts，存活 ≥ min_lifetime_ms 才 emit（防 spoof 瞬现）。
  3. book_imbalance：复用 orderbook_imbalance 维护每币最新挂单失衡（REST 降级）；
  4. book_intent：C.1 新增，复合盘口意图 = 0.5*ofi_norm + 0.3*queue_imb + 0.2*micro_tilt，
     单一出口防双计；WS 有帧时优先，无数据时返回 None（调用方降级到 orderbook_imbalance）；
  5. flush()：批量 executemany 落 SQLite(hl_orderbook_walls)。
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any, Callable

from ..hyperliquid.ws_client import HyperliquidWSClient, Subscription
from ..signals.flow_predictor import orderbook_imbalance
from ..signals.microprice import OFITracker, micro_price, queue_imbalance
from ..util import to_float as _f

log = logging.getLogger("monitor.orderbook")

# 挂单墙信号回调签名：on_wall_signal(event: dict) -> None
WallCallback = Callable[[dict[str, Any]], Any]


def detect_walls(
    levels: list[dict], mult: float = 3.0, depth: int = 20
) -> list[tuple[float, float, int]]:
    """从单侧档位识别大额挂单墙（纯函数）。

    - 每档 notional = px × sz（名义 USD）；取前 depth 档算均值；
    - 仅保留 notional ≥ mult × 均值 的档（远超周围 = 墙）；
    - 返回 [(px_float, notional, n), ...]，按 notional 降序。
    - 空/全零安全返回 []。

    参数:
      levels: [{'px','sz','n'}, ...]（HL l2Book 单侧档位，px/sz 为字符串）。
      mult:   墙阈值倍数（相对前 depth 档均值）。
      depth:  参与均值与扫描的档数。
    """
    if not levels:
        return []
    top = levels[:depth]
    # 每档名义 USD（safe 解析，拒 NaN/inf）
    notionals = [_f(lv.get("px")) * _f(lv.get("sz")) for lv in top]
    total = sum(notionals)
    if total <= 0.0:
        return []  # 全零/无效，无墙
    mean = total / len(notionals)
    if mean <= 0.0:
        return []
    thresh = mult * mean
    walls: list[tuple[float, float, int]] = []
    for lv, ntl in zip(top, notionals):
        if ntl >= thresh:
            px = _f(lv.get("px"))
            n = int(_f(lv.get("n")))  # 订单数（safe，缺失/非数→0）
            walls.append((px, ntl, n))
    walls.sort(key=lambda w: w[1], reverse=True)
    return walls


class HLOrderbookMonitor:
    """通过 HL l2Book WS 实时跟踪大额挂单墙的出现(build)/抽单(pull) 动态。

    C.1 新增：OFITracker + book_intent（复合盘口意图，单一出口防双计）。
    C.5 新增：spoof 过滤（存活时间 + build/pull 横跳计数）。
    """

    def __init__(
        self,
        coins: list[str],
        ws: HyperliquidWSClient,
        store: Any = None,                       # Store（duck-typed；None 时不落库）
        on_wall_signal: WallCallback | None = None,
        wall_mult: float = 3.0,
        min_wall_usd: float = 200_000.0,
        # C.5 抗 spoof 参数
        min_lifetime_ms: int = 3000,             # 墙需存活 ≥ 此值才 emit（过滤瞬现 spoof）
        max_flap: int = 4,                       # 近 flap_window_ms 内 build+pull ≥ 此值=spoof
        flap_window_ms: int = 30_000,            # flap 计数滑动窗口
    ) -> None:
        self.coins = list(coins)
        self.ws = ws
        self.store = store
        self.on_wall_signal = on_wall_signal
        self.wall_mult = wall_mult
        self.min_wall_usd = min_wall_usd
        self.min_lifetime_ms = min_lifetime_ms
        self.max_flap = max_flap
        self.flap_window_ms = flap_window_ms

        # coin → side("bid"/"ask") → {px_float: (notional, n)}（上一帧墙集，用于对比 build/pull）
        self._walls: dict[str, dict[str, dict[float, tuple[float, int]]]] = defaultdict(
            lambda: {"bid": {}, "ask": {}}
        )
        # coin → 最新挂单失衡 dict（imbalance/bid_usd/ask_usd + C.1 扩展字段）
        self._imbalance: dict[str, dict[str, float]] = {}

        # C.1: OFI 逐帧跟踪器
        self._ofi: OFITracker = OFITracker()
        # C.1: coin → 是否已收到至少一帧（有 WS 数据时 book_intent 才有意义）
        self._has_ws_frame: set[str] = set()

        # C.5: (coin, side, px) → 首现 ts（毫秒）
        self._wall_born: dict[tuple[str, str, float], int] = {}
        # C.5: (coin, side, px) → 近期 build/pull 事件 ts deque（maxlen=8，flap 计数）
        self._wall_flap: dict[tuple[str, str, float], deque] = defaultdict(
            lambda: deque(maxlen=8)
        )
        # C.5: (coin, side, px) → bool（是否被标记为 spoof）
        self._spoof_flag: dict[tuple[str, str, float], bool] = {}

        # 待落库墙事件缓冲：row = (ts, coin, side, kind, px, notional)
        self._buffer: list[tuple] = []
        # 统计
        self.frames_seen = 0
        self.walls_seen = 0

    # ---- 挂载 ----
    def attach(self) -> None:
        """为每个 coin 订阅 l2Book → _on_l2book。ws.run() 前后调用均可。"""
        for c in self.coins:
            self.ws.subscribe(Subscription(type="l2Book", coin=c), self._on_l2book)
        log.info("HLOrderbookMonitor 已挂载 %d 个币（l2Book 挂单墙动态）", len(self.coins))

    # ---- WS 回调 ----
    def _on_l2book(self, data: dict[str, Any], recv_ns: int) -> None:
        """l2Book 推送 → 两侧 detect_walls → 与上一帧对比识别 build/pull。

        签名与现有 HL handler 一致（data, recv_ns）。
        data = {coin, time(ms), levels: [bids, asks]}。
        """
        coin = data.get("coin")
        if not coin:
            return
        levels = data.get("levels") or [[], []]
        if len(levels) < 2:
            return
        bids = levels[0] or []
        asks = levels[1] or []
        ts = int(_f(data.get("time")))
        self.frames_seen += 1
        self._has_ws_frame.add(coin)

        # C.1: 维护 OFI + micro_price + queue_imbalance（扩展 _imbalance）
        try:
            ofi_val = self._ofi.update(coin, bids, asks, ts)  # noqa: F841
            qi = queue_imbalance(bids, asks)
            mp = micro_price(bids, asks)
            # REST 降级兼容：保留 imbalance/bid_usd/ask_usd 键（向后兼容）
            base = orderbook_imbalance(bids, asks)
            self._imbalance[coin] = {
                "imbalance": base["imbalance"],
                "bid_usd": base["bid_usd"],
                "ask_usd": base["ask_usd"],
                "queue_imb": qi,
                "micro_tilt": mp["tilt"],
                "micro": mp["micro"],
                "mid": mp["mid"],
                # ofi_norm 在 book_intent() 按窗口计算，逐帧不缓存
            }
        except Exception:  # noqa: BLE001 — 失衡计算异常不影响墙检测
            pass

        # 两侧分别检测墙，过滤 notional ≥ min_wall_usd
        for side, side_levels in (("bid", bids), ("ask", asks)):
            cur: dict[float, tuple[float, int]] = {}
            for px, ntl, n in detect_walls(side_levels, self.wall_mult):
                if ntl >= self.min_wall_usd:
                    cur[px] = (ntl, n)
            prev = self._walls[coin][side]

            # build：当前有、上一帧无的 px（新出现的墙 = 资金就位意图）
            for px, (ntl, n) in cur.items():
                key = (coin, side, px)
                if px not in prev:
                    # C.5: 记录首现 ts，不立即 emit（等存活时间检验）
                    if key not in self._wall_born:
                        self._wall_born[key] = ts
                    # C.5: 记录 build flap 事件
                    self._wall_flap[key].append(ts)
                    self._update_spoof_flag(key, ts)
                elif px in prev:
                    # 持续存在：检查是否已存活足够久可以 emit
                    born_ts = self._wall_born.get(key, ts)
                    if ts - born_ts >= self.min_lifetime_ms:
                        # 首次存活确认 emit（_wall_born 标记已 emit 避免重复：born=0）
                        if self._wall_born.get(key, -1) != 0:
                            self.walls_seen += 1
                            spoof = self._spoof_flag.get(key, False)
                            self._emit(coin, side, "build", px, ntl, ts, spoof=spoof)
                            self._wall_born[key] = 0  # 标记已 emit

            # pull：上一帧有、当前无的 px（抽单 = 意图撤销/收网）
            for px, (ntl, _n) in prev.items():
                if px not in cur:
                    key = (coin, side, px)
                    # C.5: 记录 pull flap 事件
                    self._wall_flap[key].append(ts)
                    self._update_spoof_flag(key, ts)
                    spoof = self._spoof_flag.get(key, False)
                    self._emit(coin, side, "pull", px, ntl, ts, spoof=spoof)
                    # 清理 born 记录（墙已消失）
                    self._wall_born.pop(key, None)

            # 更新状态（on_wall_signal=None 时不触发回调但仍更新状态）
            self._walls[coin][side] = cur

    def _update_spoof_flag(self, key: tuple[str, str, float], now_ts: int) -> None:
        """C.5: 根据近 flap_window_ms 内的 build/pull 次数更新 spoof 标记。"""
        flap_q = self._wall_flap[key]
        cutoff = now_ts - self.flap_window_ms
        recent_count = sum(1 for t in flap_q if t >= cutoff)
        if recent_count >= self.max_flap:
            self._spoof_flag[key] = True

    def _emit(self, coin: str, side: str, kind: str, px: float,
              notional: float, ts: int, *, spoof: bool = False) -> None:
        """缓冲墙事件落库 + 触发回调（回调 try/except，异常不影响接收）。"""
        self._buffer.append((ts, coin, side, kind, px, notional))
        if self.on_wall_signal is not None:
            try:
                self.on_wall_signal({
                    "coin": coin,
                    "side": side,         # "bid"(支撑/吸筹意图) / "ask"(压制/分销意图)
                    "kind": kind,         # "build"(出现) / "pull"(抽单)
                    "px": px,
                    "notional": notional,
                    "ts": ts,
                    "spoof": spoof,       # C.5: True=疑似 spoof（横跳频繁）
                })
            except Exception:  # noqa: BLE001 — 回调异常不影响接收循环
                log.exception("on_wall_signal 回调出错")

    # ---- 查询 ----
    def book_imbalance(self, coin: str) -> dict[str, float]:
        """返回该 coin 最新挂单失衡 {imbalance, bid_usd, ask_usd, ...}（无数据返回零）。
        向后兼容：imbalance 键始终存在。
        """
        return self._imbalance.get(coin, {
            "imbalance": 0.0, "bid_usd": 0.0, "ask_usd": 0.0,
            "queue_imb": 0.0, "micro_tilt": 0.0,
        })

    def book_intent(self, coin: str, now_ms: int) -> float | None:
        """C.1 复合盘口意图（单一出口，防双计）。

        = 0.5 * ofi_norm + 0.3 * queue_imb + 0.2 * micro_tilt ∈[-1,1]（近似）
        正 = 净买方意图（前瞻看涨）。

        无 WS 数据 → 返回 None（调用方降级到 REST orderbook_imbalance）。
        """
        if coin not in self._has_ws_frame:
            return None
        ofi_norm = self._ofi.normalized(coin, now_ms)
        imb = self._imbalance.get(coin, {})
        qi = imb.get("queue_imb", 0.0)
        mt = imb.get("micro_tilt", 0.0)
        return 0.5 * ofi_norm + 0.3 * qi + 0.2 * mt

    def all_walls(self) -> dict[str, dict[str, dict[float, tuple[float, int]]]]:
        """返回当前各币两侧墙集的深拷贝 {coin: {side: {px: (notional, n)}}}。"""
        return {
            coin: {side: dict(pxs) for side, pxs in sides.items()}
            for coin, sides in self._walls.items()
        }

    def confirming_wall(
        self, coin: str, price: float, side: str, tol_pct: float = 0.015
    ) -> dict | None:
        """返回 coin 在 side 侧、距 price 不超过 tol_pct 的**最大**挂单墙（确认 PRZ 的领先意图）。

        C.5: 跳过 spoof 标记墙 + 要求存活时间 ≥ min_lifetime_ms（存活墙才算确认意图）。

        side: "bid"（看多 setup 找支撑墙）/ "ask"（看空找压制墙）。
        无符合墙 → None。返回 {"px":float, "notional":float, "n":int, "dist_pct":float}。

        诚实定位：墙可能 spoof（虚挂）/吸收 ≠ 必反转，仅作 PRZ 确认层，非独立信号。
        price <= 0 → None（防止除零/负价格无意义查询）。
        """
        if price <= 0:
            return None

        side_walls = self._walls.get(coin, {}).get(side, {})
        if not side_walls:
            return None

        # 筛选距 price 不超过 tol_pct 的档位，按 notional 降序取最大
        best: dict | None = None
        best_notional = -1.0
        for px, (notional, n) in side_walls.items():
            dist_pct = abs(px - price) / price
            if dist_pct > tol_pct:
                continue
            key = (coin, side, px)
            # C.5: 跳过 spoof 标记墙
            if self._spoof_flag.get(key, False):
                continue
            # C.5: 存活时间检查——仅返回已通过 lifetime gate 的墙
            # _wall_born[key]=0 表示已 emit（存活确认）；>0 表示仍在等待确认；
            # key 不在 _wall_born 中表示无 WS 生命周期记录（直接注入或早期状态），视为已确认。
            born_ts = self._wall_born.get(key, 0)
            if born_ts > 0:
                # 尚未通过存活检验，不作为确认意图返回
                continue
            if notional > best_notional:
                best_notional = notional
                best = {"px": px, "notional": notional, "n": n, "dist_pct": dist_pct}

        return best

    def wall_quality(self, coin: str, side: str, px: float) -> dict:
        """C.5 只读：返回指定 px 墙的质量信息（供 dashboard/测试）。

        返回 {"born_ts": int|None, "flap_count": int, "spoof": bool}。
        """
        key = (coin, side, px)
        born_ts = self._wall_born.get(key)
        if born_ts == 0:
            born_ts = None  # 已 emit 过的标记为已确认
        flap_q = self._wall_flap.get(key, deque())
        return {
            "born_ts": born_ts,
            "flap_count": len(flap_q),
            "spoof": self._spoof_flag.get(key, False),
        }

    # ---- 落库 ----
    def flush(self) -> int:
        """墙事件缓冲批量落库，清空缓冲；返回落库行数。store=None 时仅清空缓冲。"""
        if not self._buffer:
            return 0
        rows = self._buffer
        self._buffer = []
        if self.store is not None:
            self.store.insert_orderbook_walls(rows)
        return len(rows)
