"""共享工具：安全数值解析 + 时间格式（消除跨模块重复，统一数据质量）。"""
from __future__ import annotations

import math
import time
from typing import Any


def to_float(x: Any, default: float = 0.0) -> float:
    """安全转 float：非数/None/inf/NaN → default（提高数据质量）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def fmt_hms(ms: int = 0) -> str:
    """ms 时间戳 → 本地 HH:MM:SS（ms=0 用当前时间）。高频控制台行用，简洁。"""
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000 if ms else time.time()))


def fmt_ts(ms: int = 0) -> str:
    """ms 时间戳 → 完整本地时间「YYYY-MM-DD HH:MM:SS TZ」（ms=0 用当前时间）。

    用于推送告警/报表/回顾记录：带日期+时区，跨天/事后回顾时唯一可辨，不依赖阅读时的上下文。
    """
    lt = time.localtime(ms / 1000 if ms else time.time())
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", lt)
