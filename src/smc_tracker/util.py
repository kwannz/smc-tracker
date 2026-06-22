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


def fmt_px(px: Any) -> str:
    """价格/数值 → **非科学计数法**完整数字字符串（统一格式器，消除跨模块重复）。

    %g 对大数(≥1e4)/小数(≤1e-5)会切成科学计数法(6.387e+04 / 2.533e-05)，可读性差。
    本函数按量级自适应：大数千分位两位小数(63,870.00)、中数去末尾零(1,727.08)、
    小数动态小数位保 ~4 位有效数字(0.00002533)，**任何量级都不出现 e±**。
    NaN/inf/None/非数经 to_float 兜底为 0，热路径安全。
    """
    v = to_float(px)
    a = abs(v)
    if a == 0:
        return "0"
    if a >= 1000:
        return f"{v:,.2f}"                                   # 大数：千分位 + 2 位小数
    if a >= 1:
        return f"{v:,.4f}".rstrip("0").rstrip(".")           # 中数：去末尾零
    # <1：小数位 = 保 ~4 位有效数字（log10 定位首个有效位），去末尾零，绝不科学计数
    decimals = min(18, max(4, 3 - int(math.floor(math.log10(a)))))
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".")


def is_placeholder_addr(addr: str) -> bool:
    """占位/无效地址判别：空串 或 全零地址（0x0..0）→ True。

    示例配置常残留 `0x0000…0000`「填真实地址」占位项；这类非真实可追踪钱包，
    应在订阅/推送前跳过（避免空壳画像刷屏）。仅判全零，不误伤真实地址。
    """
    if not addr:
        return True
    a = addr.lower()
    if a.startswith("0x"):
        a = a[2:]
    return a == "" or set(a) <= {"0"}


def fmt_hms(ms: int = 0) -> str:
    """ms 时间戳 → 本地 HH:MM:SS（ms=0 用当前时间）。高频控制台行用，简洁。"""
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000 if ms else time.time()))


def fmt_ts(ms: int = 0) -> str:
    """ms 时间戳 → 完整本地时间「YYYY-MM-DD HH:MM:SS TZ」（ms=0 用当前时间）。

    用于推送告警/报表/回顾记录：带日期+时区，跨天/事后回顾时唯一可辨，不依赖阅读时的上下文。
    """
    lt = time.localtime(ms / 1000 if ms else time.time())
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", lt)
