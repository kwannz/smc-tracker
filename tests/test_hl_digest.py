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
    """多类事件 → 一张文本含各分类 section 标题 + 明细 + 总数统计（挂单墙走 add_wall 聚合）。"""
    d = HLDigest()
    d.add("whale", "庄#3 净做多 BTC $300,000 @ 64,610.00")
    d.add("whale", "庄#7 净做空 ETH $120,000 @ 1,742.70")
    d.add_wall("BTC", "bid", 1_468_100, 64610.0)
    d.add("pump", "FLOKI 暴涨 +12% @ 0.00002533")
    out = d.render(1_700_000_000_000)
    assert out is not None
    assert "🐋 跟庄信号" in out and "🧱 挂单墙" in out and "🚀 暴涨暴跌" in out
    assert "庄#3" in out and "0.00002533" in out
    assert "BTC" in out.split("🧱 挂单墙")[1]   # 挂单墙 section 含 BTC 聚合
    assert "4 条" in out  # 总数统计（whale 2 + wall 1 + pump 1）


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


def test_wall_section_aggregates_per_coin_not_raw_lines():
    """挂单墙不逐条列原始事件，按币聚合 bid/ask 净意图 + 整体分析（用户#：不要 6 条，要整体+单币总结）。"""
    d = HLDigest()
    # 用户真实 6 条墙
    d.add_wall("BTC", "bid", 1_497_623, 64138.0)
    d.add_wall("BTC", "ask", 2_308_574, 64126.0)
    d.add_wall("SOL", "ask", 278_445, 73.856)
    d.add_wall("ETH", "ask", 2_559_321, 1749.9)
    d.add_wall("SOL", "bid", 212_905, 73.831)
    d.add_wall("ETH", "bid", 2_450_694, 1746.9)
    out = d.render(1_700_000_000_000)
    assert out is not None and "🧱 挂单墙" in out
    wall_sec = out.split("🧱 挂单墙")[1]
    # 3 币各**一行**总结（不是 6 条原始事件）
    assert wall_sec.count("BTC") == 1 and wall_sec.count("ETH") == 1 and wall_sec.count("SOL") == 1
    # 每币净意图：BTC ask 2.31M > bid 1.50M → 净 ask 压制/分销
    assert "净" in wall_sec and "压制" in wall_sec
    # 整体分析行（全币 net ask）
    assert "整体" in wall_sec
    # spoof 提醒只在 header 出现一次（不再逐条重复刷屏）
    assert wall_sec.count("spoof") <= 1


def test_coin_bias_long_short_ratio():
    """按币种多空比例（用户#）：聚合各信号方向 → 每币 多/空计数 + 倾向 + 来源；挂单墙 bid/ask 计入。"""
    d = HLDigest()
    d.add_bias("TRUMP", True, "跟庄")
    d.add_bias("TRUMP", True, "背离")
    d.add_bias("TRUMP", True, "超级")
    d.add_bias("PEPE", False, "背离")
    d.add_bias("PEPE", False, "TA")
    d.add_bias("BTC", True, "SMC")
    d.add_bias("BTC", False, "持仓")
    d.add_wall("BTC", "bid", 2_000_000, 64000.0)     # bid 净 → 计入 BTC 多
    out = d.render(1_700_000_000_000)
    assert out is not None and "📊 币种多空比例" in out
    bias = out.split("📊 币种多空比例")[1].split("\n【")[0]   # 只取多空比例 section
    assert "TRUMP" in bias and "PEPE" in bias and "BTC" in bias
    assert "净多" in bias                              # TRUMP 全多 → 净多 100%
    assert "净空" in bias                              # PEPE 全空 → 净空 100%
    assert "跟庄" in bias and "背离" in bias            # 来源标注
    # 币种多空比例排在挂单墙等明细之前（头部总览）
    assert out.index("📊 币种多空比例") < out.index("🧱 挂单墙")


def test_bias_cleared_after_render():
    """多空比例 render 后清空，下周期从零。"""
    d = HLDigest()
    d.add_bias("BTC", True, "SMC")
    d.add("signal", "⚡ BTC 做多")
    assert d.render(1_700_000_000_000) is not None
    assert d.render(1_700_000_001_000) is None


def test_wall_only_digest_renders_and_clears():
    """仅挂单墙（无其它分类）也应产出（pending 计入 walls）；render 后清空。"""
    d = HLDigest()
    d.add_wall("BTC", "bid", 2_000_000, 64000.0)
    assert d.pending() >= 1
    out = d.render(1_700_000_000_000)
    assert out is not None and "BTC" in out
    assert d.render(1_700_000_001_000) is None       # 已清空


def test_category_order_core_signals_first():
    """分类顺序：核心抓庄信号（跟庄/超级）排在挂单墙/TA 之前（阅读优先级）。"""
    d = HLDigest()
    d.add("ta", "TA行")
    d.add("whale", "跟庄行")
    out = d.render(1_700_000_000_000)
    assert out.index("跟庄信号") < out.index("TA 信号")
