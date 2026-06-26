"""统一 K 线摄入门面（candle ingest facade）。

设计目标：
  - WS 增量推送与 REST 回填经**同一清洗 + 落库路径**，消除双写不一致。
  - 复用 candle_collector._clean_candles（import，绝不复制清洗逻辑）。
  - gap 检测：按 GRANULARITY_MS 估算缺口，按需触发 backfill。

公开 API（三个函数）：
  backfill(bg, coin, symbol, tf, bars, store) -> int
  detect_and_fill_gap(bg, coin, symbol, tf, store) -> int
  ingest_ws_closed_bar(candle, store) -> bool
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ..bitget.rest import GRANULARITY_MS
from ..models import Candle
from .candle_collector import _clean_candles  # 复用已有清洗逻辑，严禁复制

if TYPE_CHECKING:
    from ..storage.db import Store

log = logging.getLogger(__name__)

# REST 回填默认根数（库为空时）
_DEFAULT_BARS: int = 300


async def backfill(
    bg: Any,
    coin: str,
    symbol: str,
    tf: str,
    bars: int,
    store: "Store",
) -> int:
    """拉取 REST K 线并落库，返回实际写入根数。

    Args:
        bg:     BitgetREST 实例（async with 上下文内）
        coin:   Candle.coin 标签（如 "BTC"）
        symbol: Bitget 交易对（如 "BTCUSDT"）
        tf:     K 线周期，需在 GRANULARITY_MS 中
        bars:   目标拉取根数
        store:  实现了 upsert_candles() 的存储对象

    Returns:
        实际写入根数（清洗后）；拉取/清洗全空则返回 0。
    """
    if bars <= 0:
        return 0

    try:
        # 调用 BitgetREST.klines（含分页回填 + 429 重试）
        candles: list[Candle] = await bg.klines(symbol, tf, bars=bars, coin=coin)
    except Exception as exc:  # noqa: BLE001
        log.warning("candle_ingest.backfill: coin=%s tf=%s 拉取失败: %r", coin, tf, exc)
        return 0

    if not candles:
        return 0

    # 复用 candle_collector._clean_candles 做同样清洗（NaN/inf/负价/h<l/ts去重）
    candles = _clean_candles(candles)
    if not candles:
        return 0

    # 组装落库行格式：(coin, tf, open_ms, o, h, l, c, v)
    rows = [
        (c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v)
        for c in candles
    ]
    store.upsert_candles(rows)
    log.debug("candle_ingest.backfill: coin=%s tf=%s 写入 %d 根", coin, tf, len(rows))
    return len(rows)


async def detect_and_fill_gap(
    bg: Any,
    coin: str,
    symbol: str,
    tf: str,
    store: "Store",
) -> int:
    """检测并回填 K 线缺口，返回实际写入根数（无缺口返回 0）。

    算法：
      1. 查 store.latest_candle_ms(coin, tf) 取最新 open_ms。
      2. 若 None（库为空）→ backfill 拉 _DEFAULT_BARS 根初始化。
      3. 否则：按 GRANULARITY_MS[tf] 估算 latest 到"当前最近收盘 bar"之间
         缺了几根；缺口 >=1 → backfill 覆盖缺口；无缺口返回 0。

    Args:
        bg:     BitgetREST 实例（async with 上下文内）
        coin:   Candle.coin 标签
        symbol: Bitget 交易对
        tf:     K 线周期
        store:  实现了 latest_candle_ms() + upsert_candles() 的存储对象

    Returns:
        实际写入根数；无缺口时为 0。
    """
    if tf not in GRANULARITY_MS:
        log.warning("detect_and_fill_gap: 未知 tf=%r，跳过", tf)
        return 0

    gran_ms: int = GRANULARITY_MS[tf]
    latest_ms: int | None = store.latest_candle_ms(coin, tf)

    # 库为空 → 初始化回填
    if latest_ms is None:
        log.debug("detect_and_fill_gap: coin=%s tf=%s 库为空，初始化回填 %d 根", coin, tf, _DEFAULT_BARS)
        return await backfill(bg, coin, symbol, tf, _DEFAULT_BARS, store)

    # 缺口计数**锚定 latest_ms**（交易所返回的真实对齐 open），与交易所 bar 边界时区无关。
    # 修正原纪元对齐 bug：(now//gran)*gran 假设 bar 对齐 1970 纪元(周四)，但 Bitget 按 UTC+8
    # 日界对齐(1D=16:00 UTC、1W=周日 16:00 UTC)，对 1D/1W/4H 等系统性错位。
    # latest_ms 之后第 j 根(open=latest+j*gran)已收盘 ⟺ latest+(j+1)*gran <= now。
    # 已收盘缺口数 = floor((now-latest)/gran) - 1（j>=1 的已收盘根数；对齐无关）。
    now_ms: int = int(time.time() * 1000)
    gap_count: int = (now_ms - latest_ms) // gran_ms - 1
    if gap_count <= 0:
        # 已是最新（latest 即最近已收盘 bar，或时钟回拨），无缺口
        return 0

    # 拉取足够覆盖缺口的根数（+1 保证包含边界）
    fill_bars: int = gap_count + 1
    log.debug(
        "detect_and_fill_gap: coin=%s tf=%s 检测到缺口 %d 根，回填 %d 根",
        coin, tf, gap_count, fill_bars,
    )
    return await backfill(bg, coin, symbol, tf, fill_bars, store)


def ingest_ws_closed_bar(candle: Candle, store: "Store") -> bool:
    """WS 收盘单根 Candle → 清洗 → 落库。

    与 backfill 路径完全一致（复用 _clean_candles），确保 WS 增量与
    REST 回填经同一清洗逻辑，消除双写不一致。

    Args:
        candle: WS 推送的已收盘 Candle
        store:  实现了 upsert_candles() 的存储对象

    Returns:
        True  → 清洗通过且落库成功；
        False → 脏数据（NaN/inf/负价/h<l），已被 _clean_candles 过滤，不落库。
    """
    # 对单元素列表做同样清洗（与 backfill 路径一致）
    cleaned = _clean_candles([candle])
    if not cleaned:
        log.debug(
            "ingest_ws_closed_bar: 脏数据已过滤 coin=%s tf=%s open_ms=%d",
            candle.coin, candle.interval, candle.open_time_ms,
        )
        return False

    c = cleaned[0]
    store.upsert_candles([(c.coin, c.interval, c.open_time_ms, c.o, c.h, c.l, c.c, c.v)])
    return True
