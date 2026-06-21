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
