"""斐波那契回撤/扩展。从一段摆动(swing high/low)计算回撤位与扩展位，含黄金口袋(OTE)。"""
from __future__ import annotations

RETRACE = (0.236, 0.382, 0.5, 0.618, 0.705, 0.786)
EXTEND = (1.272, 1.414, 1.618, 2.0, 2.618)


def fib_levels(high: float, low: float, direction: str = "up") -> dict[str, float]:
    """direction='up'：上涨段(low→high)的回撤位在下方、扩展位在上方；'down' 相反。

    返回 {ret_0.618:..., ext_1.618:..., golden_lo/golden_hi(0.618–0.786 黄金口袋)}。
    非法 direction 直接抛出 ValueError，不静默降级。
    """
    direction = direction.lower()
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
    rng = abs(high - low)
    if rng == 0:
        return {}
    out: dict[str, float] = {}
    if direction == "up":
        for r in RETRACE:
            out[f"ret_{r}"] = high - r * rng
        for e in EXTEND:
            out[f"ext_{e}"] = low + e * rng
        out["golden_hi"] = high - 0.618 * rng
        out["golden_lo"] = high - 0.786 * rng
    else:
        for r in RETRACE:
            out[f"ret_{r}"] = low + r * rng
        for e in EXTEND:
            out[f"ext_{e}"] = high - e * rng
        out["golden_lo"] = low + 0.618 * rng
        out["golden_hi"] = low + 0.786 * rng
    return out


def in_golden_pocket(price: float, high: float, low: float, direction: str = "up") -> bool:
    """价格是否落在 0.618–0.786 黄金口袋(最优入场区)。"""
    lv = fib_levels(high, low, direction)
    if "golden_lo" not in lv:
        return False
    lo, hi = sorted((lv["golden_lo"], lv["golden_hi"]))
    return lo <= price <= hi


def nearest_fib(price: float, high: float, low: float, direction: str = "up"
                ) -> tuple[str, float] | None:
    """离 price 最近的斐波那契位 (名称, 价格)。"""
    lv = fib_levels(high, low, direction)
    if not lv:
        return None
    name = min(lv, key=lambda k: abs(lv[k] - price))
    return name, lv[name]


def golden_pocket_zone(
    high: float, low: float, direction: str = "up"
) -> tuple[float, float]:
    """返回 0.618–0.786 黄金口袋区间 (lo, hi)。

    direction='up'（上涨段 low→high）：回撤区在 high 下方，
      golden_lo = high - 0.786 * rng, golden_hi = high - 0.618 * rng。
    direction='down'（下跌段 high→low）：反向，
      golden_lo = low + 0.618 * rng, golden_hi = low + 0.786 * rng。

    复用 fib_levels 确保与 in_golden_pocket/nearest_fib 数值一致。
    当 high==low（零振幅）时返回 (high, high)（退化情形，调用方应过滤）。
    """
    lv = fib_levels(high, low, direction)
    if not lv:
        # 零振幅退化：返回点区间
        return (high, high)
    lo = min(lv["golden_lo"], lv["golden_hi"])
    hi = max(lv["golden_lo"], lv["golden_hi"])
    return (lo, hi)


def intersect_zone(
    a_lo: float, a_hi: float, b_lo: float, b_hi: float
) -> tuple[float, float] | None:
    """计算两个区间 [a_lo, a_hi] 与 [b_lo, b_hi] 的交集。

    有交集返回 (lo, hi)；无交集返回 None。
    入参不要求 lo ≤ hi（内部自动 sort），兼容颠倒传入。
    """
    # 内部排序，兼容颠倒传入
    a_lo, a_hi = min(a_lo, a_hi), max(a_lo, a_hi)
    b_lo, b_hi = min(b_lo, b_hi), max(b_lo, b_hi)
    lo = max(a_lo, b_lo)
    hi = min(a_hi, b_hi)
    if lo > hi:
        return None
    return (lo, hi)
