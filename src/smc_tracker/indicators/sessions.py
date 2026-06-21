"""时间策略：交易时段 + SMC killzone（基于 UTC 小时）。"""
from __future__ import annotations

import time

# (起始UTC时, 结束UTC时, 名称)
_SESSIONS = [(0, 7, "亚洲"), (7, 13, "伦敦"), (13, 21, "纽约"), (21, 24, "盘后")]
_KILLZONES = [(0, 3, "亚洲开盘"), (7, 10, "伦敦开盘"), (12, 15, "纽约开盘"),
              (18, 20, "纽约午盘")]


def _utc_hour(ts_ms: int) -> int:
    return time.gmtime(ts_ms / 1000).tm_hour


def current_session(ts_ms: int) -> str:
    h = _utc_hour(ts_ms)
    for s, e, name in _SESSIONS:
        if s <= h < e:
            return name
    return "盘后"


def in_killzone(ts_ms: int) -> str | None:
    """是否处于 SMC killzone（高波动/高胜率时段），返回名称或 None。"""
    h = _utc_hour(ts_ms)
    for s, e, name in _KILLZONES:
        if s <= h < e:
            return name
    return None
