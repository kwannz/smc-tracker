"""现货 taker 主动流向：从成交列表计算主动买卖净压力（现货维度聪明钱信号）。

CEX 现货无地址级数据，但成交带 taker 方向（主动买/卖）→ 可算市场级主动买卖净压力。
纯函数，无状态/无网络，易测。
"""
from __future__ import annotations

from .. util import to_float as _f


def spot_taker_flow(trades: list[dict]) -> dict:
    """从成交列表计算主动买卖净流向。

    :param trades: [{px, sz, side}]，side='buy'=主动买/'sell'=主动卖（OKX trades 格式）
    :return: {buy_usd, sell_usd, net_usd(买-卖), flow_dir('long' if net>0 else 'short')}
    """
    buy_usd = 0.0
    sell_usd = 0.0
    for t in trades:
        px = _f(t.get("px"))
        sz = _f(t.get("sz"))
        if px <= 0 or sz <= 0:
            continue
        usd = px * sz
        if t.get("side") == "buy":
            buy_usd += usd
        elif t.get("side") == "sell":
            sell_usd += usd
    net = buy_usd - sell_usd
    return {"buy_usd": buy_usd, "sell_usd": sell_usd, "net_usd": net,
            "flow_dir": "long" if net > 0 else "short"}


def is_significant_flow(flow: dict, threshold_usd: float = 500_000.0) -> bool:
    """现货净流向绝对值是否达到显著阈值（默认 $50 万）。"""
    return abs(_f(flow.get("net_usd"))) >= threshold_usd
