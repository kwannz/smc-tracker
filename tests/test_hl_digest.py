"""HLDigest 单测：HL 事件**分类聚合**成一张汇总卡片文本（降低即时刷屏）。

用户诉求「信息过多，核心还是 HL，分类集中在分类卡片汇总」——零散 HL 事件不再每条即时推，
按分类收集到缓冲，周期渲染成**一张**分类汇总卡片（每类一个 section），空类省略、超量截断标注。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.notify.digest import HLDigest


def test_empty_digest_renders_none():
    """无任何事件 → render 返回 None（不推空卡，避免无意义刷屏）。"""
    d = HLDigest()
    assert d.render(1_700_000_000_000) is None


def test_categories_grouped_with_counts():
    """多类事件 → 一张文本含各分类 section 标题 + 明细 + 总数统计。"""
    d = HLDigest()
    d.add("whale", "庄#3 净做多 BTC $300,000 @ 64,610.00")
    d.add("whale", "庄#7 净做空 ETH $120,000 @ 1,742.70")
    d.add("wall", "BTC 🟢bid墙 @ 64,610.00 $1,468,100")
    d.add("pump", "FLOKI 暴涨 +12% @ 0.00002533")
    out = d.render(1_700_000_000_000)
    assert out is not None
    assert "🐋 跟庄信号" in out and "🧱 挂单墙" in out and "🚀 暴涨暴跌" in out
    assert "庄#3" in out and "1,468,100" in out and "0.00002533" in out
    assert "4 条" in out  # 总数统计（2+1+1）


def test_unknown_category_ignored_safely():
    """未知分类 key 不抛异常（数据质量守卫）；仅已知分类进卡片。"""
    d = HLDigest()
    d.add("not_a_category", "脏数据")
    d.add("whale", "庄#1 BTC")
    out = d.render(1_700_000_000_000)
    assert out is not None and "庄#1" in out and "脏数据" not in out


def test_per_category_cap_keeps_latest():
    """单类超上限 → 只显示最新 N 条并标注被省略数量（防单类刷爆卡片）。"""
    d = HLDigest(max_per_cat=3)
    for i in range(10):
        d.add("ta", f"信号{i}")
    out = d.render(1_700_000_000_000)
    assert out is not None
    assert "信号9" in out and "信号8" in out and "信号7" in out  # 最新3条
    assert "信号0" not in out                                   # 旧的被省略
    assert "10 条" in out                                       # 真实总数仍统计


def test_render_clears_buffer():
    """render 后缓冲清空：下一周期从零开始（再 render 无残留 → None）。"""
    d = HLDigest()
    d.add("signal", "⚡ BTC 做多")
    assert d.render(1_700_000_000_000) is not None
    assert d.render(1_700_000_001_000) is None


def test_category_order_core_signals_first():
    """分类顺序：核心抓庄信号（跟庄/超级）排在挂单墙/TA 之前（阅读优先级）。"""
    d = HLDigest()
    d.add("ta", "TA行")
    d.add("whale", "跟庄行")
    out = d.render(1_700_000_000_000)
    assert out.index("跟庄信号") < out.index("TA 信号")
