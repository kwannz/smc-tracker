"""util.fmt_px 单测：价格/数值统一格式化为**非科学计数法**完整数字（用户要求）。

覆盖服务器实测出现过科学计数法的真实值：挂单墙 6.387e+04(=63870)、行情板 FLOKI 2.533e-05、
SHIB 4.679e-06——这些必须显示成完整数字（63,870.00 / 0.00002533 / 0.000004679），绝不含 e±。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.util import fmt_px


def test_no_scientific_notation_anywhere():
    """跨大/中/小数量级，输出绝不含科学计数法 e±（核心诉求）。"""
    for v in (63870.0, 1_468_100.0, 1727.08, 0.1631, 0.004558,
              0.00002533, 0.000004679, 1e12, 1e-9, 12345.6789):
        s = fmt_px(v)
        assert "e" not in s.lower(), f"{v!r} → {s!r} 含科学计数法"


def test_large_price_thousands_separator():
    """大数：千分位 + 两位小数，完整可读（挂单墙 6.387e+04 的真实病例）。"""
    assert fmt_px(63870.0) == "63,870.00"
    assert fmt_px(1_468_100.0) == "1,468,100.00"


def test_mid_price_strip_trailing_zeros():
    """1~1000：4 位小数去末尾零。"""
    assert fmt_px(1727.08) == "1,727.08"
    assert fmt_px(5.0) == "5"
    assert fmt_px(2.5) == "2.5"


def test_small_price_keeps_significant_digits():
    """<1 的 meme 价：保 ~4 位有效数字、去末尾零、非科学（FLOKI/SHIB 真实病例）。"""
    assert fmt_px(0.00002533) == "0.00002533"
    assert fmt_px(0.000004679) == "0.000004679"
    assert fmt_px(0.1631) == "0.1631"


def test_zero_and_invalid_safe():
    """0/NaN/inf/None 安全（经 to_float 兜底为 0），不抛异常、不科学。"""
    assert fmt_px(0) == "0"
    assert fmt_px(float("nan")) == "0"
    assert fmt_px(float("inf")) == "0"
    assert fmt_px(None) == "0"
    assert fmt_px("not a number") == "0"


def test_negative_price():
    """负值（如净流向）保符号、非科学。"""
    s = fmt_px(-63870.0)
    assert s == "-63,870.00"
    assert "e" not in s.lower()
