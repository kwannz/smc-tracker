"""持仓生命周期重建：开仓时间、平仓时间、持仓时长（纯函数，可测，不联网）。

业界标准 position-netting + HL dir 语义：
  - 从 Fill 列表重建每个 coin 的当前持仓段（当前方向/开仓时间/最近平仓时间）。
  - 优先用 dir 语义（HL 明确标注 Open/Close/反手），辅以 start_position+signed_sz 交叉校验。
  - 完全平仓(running≈0) → current_dir='flat'，open_ms 清 0。
  - 反手（'Long > Short'/'Short > Long'）→ 方向翻转，open_ms 重置为该笔时间。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..util import to_float as _f

if TYPE_CHECKING:
    from ..models import Fill

log = logging.getLogger("position_lifecycle")

# flat 判定：当前 running_size 的绝对值低于该段最大绝对值的 1%
_FLAT_REL_EPS = 0.01
# 绝对值最小档（即使段 max 很小，也不低于这个档被误判为 flat）
_FLAT_ABS_EPS = 1e-9


@dataclass(slots=True)
class PositionLifecycle:
    """单地址单 coin 当前持仓段生命周期。

    Attributes:
        coin:              币种
        open_ms:           当前持仓段开仓时间 ms（flat 时为 0）
        last_close_ms:     最近一次平仓动作时间 ms（含部分平仓；从未平仓则为 0）
        last_action_ms:    最后一笔成交时间 ms
        n_segment_fills:   当前段内累积成交笔数（开/加仓；反手/完全平后重置）
        current_dir:       当前方向 'long'/'short'/'flat'
    """

    coin: str
    open_ms: int
    last_close_ms: int
    last_action_ms: int
    n_segment_fills: int
    current_dir: str   # 'long' / 'short' / 'flat'


def _is_open_dir(dir_str: str) -> bool:
    """HL dir 是否为开仓方向（开多/开空）。"""
    s = dir_str.lower()
    return "open" in s


def _is_close_dir(dir_str: str) -> bool:
    """HL dir 是否为平仓动作（含反手中的平仓部分）。"""
    s = dir_str.lower()
    return "close" in s or ">" in s   # 'Long > Short' / 'Short > Long' 含反手


def _is_reversal(dir_str: str) -> bool:
    """HL dir 是否为反手操作。"""
    return ">" in dir_str   # 'Long > Short' 或 'Short > Long'


def reconstruct(fills: list["Fill"], now_ms: int) -> dict[str, PositionLifecycle]:
    """从 Fill 列表重建每个 coin 的当前持仓段生命周期。

    算法概要：
    1. 按 coin 分组，按 time_ms 升序遍历。
    2. 维护 running_signed_size（+多/-空），同时跟踪当前段 max_abs_size。
    3. 段起点判定：
       - 从 flat 进入非 flat（首笔成交或完全平仓后第一笔重开）。
       - 反手（'Long > Short'/'Short > Long'）→ 方向翻转，新段从该笔开始。
    4. 平仓动作（dir 含 'Close' 或 '>'）→ 更新 last_close_ms；
       若 running 回到 ≈0 → current_dir='flat'，open_ms=0。
    5. 同向加仓：不改 open_ms，只累加 n_segment_fills + 更新 running。

    :param fills:   某地址的 Fill 列表（可多 coin 混合）
    :param now_ms:  当前时间 ms（保留供日后扩展）
    :return:        coin → PositionLifecycle 的字典（仅含有记录的 coin）
    """
    # 按 coin 分桶
    by_coin: dict[str, list["Fill"]] = {}
    for f in fills:
        by_coin.setdefault(f.coin, []).append(f)

    result: dict[str, PositionLifecycle] = {}

    for coin, coin_fills in by_coin.items():
        # 按时间升序处理
        coin_fills = sorted(coin_fills, key=lambda f: f.time_ms)

        # 持仓段状态
        running: float = 0.0           # 当前带符号仓位（+多/-空）
        open_ms: int = 0               # 当前段开仓时间
        last_close_ms: int = 0         # 最近平仓动作时间
        last_action_ms: int = 0        # 最后一笔成交时间
        n_fills: int = 0               # 当前段成交笔数
        current_dir: str = "flat"      # 当前方向
        seg_max_abs: float = 0.0       # 当前段最大 abs(running)

        for fill in coin_fills:
            last_action_ms = fill.time_ms
            dir_str = fill.dir or ""
            # BUY(side B) = +sz；SELL(side A) = -sz
            signed_sz = _f(fill.sz) if fill.side.name == "BUY" else -_f(fill.sz)

            # 数据质量守卫
            if _f(fill.sz) <= 0:
                continue

            is_reversal = _is_reversal(dir_str)
            is_close = _is_close_dir(dir_str)

            if is_reversal:
                # 反手：先平当前方向，再以反方向重开
                # 平仓部分：更新 last_close_ms
                last_close_ms = fill.time_ms
                # 新方向由 signed_sz 决定（净增量）
                running_new = running + signed_sz
                # 判断新方向
                if running_new > _FLAT_ABS_EPS:
                    new_dir = "long"
                elif running_new < -_FLAT_ABS_EPS:
                    new_dir = "short"
                else:
                    new_dir = "flat"
                # 重置新段
                running = running_new
                current_dir = new_dir
                seg_max_abs = abs(running)
                if new_dir != "flat":
                    open_ms = fill.time_ms
                    n_fills = 1
                else:
                    open_ms = 0
                    n_fills = 0

            elif is_close:
                # 平仓动作（含部分平仓）
                last_close_ms = fill.time_ms
                running += signed_sz
                # 平仓后检查是否 flat
                flat_threshold = max(seg_max_abs * _FLAT_REL_EPS, _FLAT_ABS_EPS)
                if abs(running) < flat_threshold:
                    running = 0.0
                    current_dir = "flat"
                    open_ms = 0
                    n_fills = 0
                    seg_max_abs = 0.0
                else:
                    # 缺陷1修复：超量平仓使 running 穿越0变号时，按新符号更新方向
                    # 例：long 100 → Close Long 150 → running=-50 → current_dir 应为 short
                    prev_dir = current_dir
                    if running > _FLAT_ABS_EPS:
                        current_dir = "long"
                    elif running < -_FLAT_ABS_EPS:
                        current_dir = "short"
                    # P1 越零路径：平仓后方向翻转（超量平仓穿越零），重置新段
                    # 镜像 is_reversal 分支：open_ms/n_fills/seg_max_abs 从该笔重新起算
                    if current_dir != prev_dir:
                        open_ms = fill.time_ms
                        n_fills = 1
                        seg_max_abs = abs(running)
                # 平仓笔在未越零时不计入开仓段笔数（n_fills 不变）

            else:
                # 开仓 / 加仓 / 裸 'Buy'/'Sell'（HL dir 无 Open/Close/> 标注时）
                was_flat = current_dir == "flat"

                if was_flat:
                    # 从 flat 新开仓：重置段（含裸 Buy/Sell 首笔建仓）
                    running += signed_sz
                    open_ms = fill.time_ms
                    n_fills = 1
                    seg_max_abs = abs(running)
                    current_dir = "long" if running > _FLAT_ABS_EPS else "short" if running < -_FLAT_ABS_EPS else "flat"
                else:
                    # 缺陷2修复：区分减仓（异号）与同向加仓（同号）
                    # 减仓：signed_sz 与当前 running 方向相反（如 long 仓位收到 SELL）
                    is_reduce = (running > 0 and signed_sz < 0) or (running < 0 and signed_sz > 0)
                    if is_reduce:
                        # 减仓处理：更新 last_close_ms，不增加段笔数
                        last_close_ms = fill.time_ms
                        running += signed_sz
                        flat_threshold = max(seg_max_abs * _FLAT_REL_EPS, _FLAT_ABS_EPS)
                        if abs(running) < flat_threshold:
                            # 减仓到 flat：重置段
                            running = 0.0
                            current_dir = "flat"
                            open_ms = 0
                            n_fills = 0
                            seg_max_abs = 0.0
                        else:
                            # 部分减仓：按符号更新方向（也处理异号穿越）
                            if running > _FLAT_ABS_EPS:
                                current_dir = "long"
                            elif running < -_FLAT_ABS_EPS:
                                current_dir = "short"
                    else:
                        # 同向加仓（含裸 Buy/Sell 加仓）
                        running += signed_sz
                        n_fills += 1
                        seg_max_abs = max(seg_max_abs, abs(running))
                        # current_dir 由 running 符号决定（防止浮点异号）
                        if running > _FLAT_ABS_EPS:
                            current_dir = "long"
                        elif running < -_FLAT_ABS_EPS:
                            current_dir = "short"
                        else:
                            current_dir = "flat"
                            open_ms = 0
                            n_fills = 0

        result[coin] = PositionLifecycle(
            coin=coin,
            open_ms=open_ms,
            last_close_ms=last_close_ms,
            last_action_ms=last_action_ms,
            n_segment_fills=n_fills,
            current_dir=current_dir,
        )

    return result


def fmt_hold(open_ms: int, now_ms: int) -> str:
    """持仓时长人类可读字符串。

    open_ms <= 0 → '—'（未持仓或未知）。
    格式：
      < 1h  → 'Xm'（分钟）
      < 24h → 'XhYm'
      ≥ 24h → 'XdYh'

    :param open_ms: 开仓时间 ms
    :param now_ms:  当前时间 ms
    :return:        时长字符串
    """
    if open_ms <= 0 or now_ms <= open_ms:
        return "—"
    elapsed_s = (now_ms - open_ms) // 1000
    if elapsed_s < 60:
        return f"{elapsed_s}s"
    minutes = elapsed_s // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    mins_rem = minutes % 60
    if hours < 24:
        return f"{hours}h{mins_rem}m" if mins_rem else f"{hours}h"
    days = hours // 24
    hrs_rem = hours % 24
    return f"{days}d{hrs_rem}h" if hrs_rem else f"{days}d"
