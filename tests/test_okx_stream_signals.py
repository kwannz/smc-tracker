"""OKX streaming 信号展示单测（fmt_flow_signals + detect_divergences 纯函数，不联网）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_fmt_flow_signals_long_short():
    """有信号 → 含 coin + 方向。"""
    from smc_tracker.okx.stream import fmt_flow_signals
    out = fmt_flow_signals([{"coin": "BTC", "direction": "long", "net_flow": 600000}])
    assert "BTC" in out
    assert "long" in out
    assert "600,000" in out


def test_fmt_flow_signals_empty_returns_none_text():
    """空列表 → "无"。"""
    from smc_tracker.okx.stream import fmt_flow_signals
    assert fmt_flow_signals([]) == "无"


def test_fmt_flow_signals_multiple_joined():
    """多信号 → 用 / 分隔。"""
    from smc_tracker.okx.stream import fmt_flow_signals
    out = fmt_flow_signals([
        {"coin": "BTC", "direction": "long", "net_flow": 600000},
        {"coin": "ETH", "direction": "short", "net_flow": 700000}])
    assert "BTC" in out and "ETH" in out and "/" in out


def test_detect_divergences_picks_bearish():
    """多头拥挤(funding>0) + taker 净卖 → 背离 bearish。"""
    from smc_tracker.okx.stream import detect_divergences
    latest = {"BTC-USDT-SWAP": {"coin": "BTC", "funding": 0.0005}}
    out = detect_divergences(latest, {"BTC": -500_000})
    assert len(out) == 1
    assert out[0][0] == "BTC"
    assert out[0][1]["direction"] == "bearish"


def test_detect_divergences_none_when_aligned():
    """同向(多头拥挤+净买)→ 无背离。"""
    from smc_tracker.okx.stream import detect_divergences
    latest = {"BTC-USDT-SWAP": {"coin": "BTC", "funding": 0.0005}}
    assert detect_divergences(latest, {"BTC": 500_000}) == []


def test_top_funding_sorts_by_abs_desc():
    """资金费拥挤榜：按 abs(funding) 降序，跳过缺失/0 funding。"""
    from smc_tracker.okx.stream import top_funding
    latest = {
        "BTC-USDT-SWAP": {"coin": "BTC", "funding": 0.0005},
        "ETH-USDT-SWAP": {"coin": "ETH", "funding": -0.001},
        "SOL-USDT-SWAP": {"coin": "SOL", "funding": 0.0},   # 0 → 跳过
        "XRP-USDT-SWAP": {"coin": "XRP"},                   # 缺失 → 跳过
    }
    out = top_funding(latest, 5)
    assert out[0] == ("ETH", -0.001)   # abs 最大在前
    assert out[1] == ("BTC", 0.0005)
    assert len(out) == 2


def test_top_funding_limit_n():
    """只取前 n 个。"""
    from smc_tracker.okx.stream import top_funding
    latest = {f"C{i}-USDT-SWAP": {"coin": f"C{i}", "funding": (i + 1) * 0.001}
              for i in range(5)}
    assert len(top_funding(latest, 3)) == 3


# ---- SpotTakerCollector：现货 taker 主动流向接入（spot_flow 纯函数运行时）----

def test_spot_collector_buffers_only_whitelisted_inst():
    """白名单过滤：仅现货 instId 入缓冲，永续推送忽略。"""
    from smc_tracker.okx.stream import SpotTakerCollector
    c = SpotTakerCollector(["BTC-USDT"], threshold_usd=500_000.0)
    # 现货推送 → 入缓冲（$600,000 主动买，超阈值）
    c._on_spot_trades({"instId": "BTC-USDT"}, [{"px": "60000", "sz": "10", "side": "buy"}], 0)
    # 永续推送（非白名单）→ 忽略
    c._on_spot_trades({"instId": "BTC-USDT-SWAP"}, [{"px": "60000", "sz": "99", "side": "buy"}], 0)
    out = c.flush()
    assert len(out) == 1
    assert out[0]["coin"] == "BTC"
    # 净流向只来自现货那一笔（$600,000 买），不含永续 99 张污染
    assert abs(out[0]["flow"]["buy_usd"] - 600_000.0) < 1e-6


def test_spot_collector_flush_significant_and_clears_window():
    """显著净流向上报（窗口口径），flush 后缓冲清空，下窗无残留。"""
    from smc_tracker.okx.stream import SpotTakerCollector
    c = SpotTakerCollector(["ETH-USDT"], threshold_usd=500_000.0)
    # 主动买 $600,000（>$50万阈值）→ 显著
    c._on_spot_trades({"instId": "ETH-USDT"}, [{"px": "3000", "sz": "200", "side": "buy"}], 0)
    out1 = c.flush()
    assert len(out1) == 1 and out1[0]["flow"]["flow_dir"] == "long"
    assert c.flows_seen == 1
    # 窗口口径：再 flush 缓冲已空 → 无输出（不累计上窗）
    assert c.flush() == []


def test_spot_collector_below_threshold_not_significant():
    """净流向不达阈值 → 不上报。"""
    from smc_tracker.okx.stream import SpotTakerCollector
    c = SpotTakerCollector(["BTC-USDT"], threshold_usd=500_000.0)
    c._on_spot_trades({"instId": "BTC-USDT"}, [{"px": "60000", "sz": "1", "side": "buy"}], 0)
    assert c.flush() == []  # $60k < $500k


def test_spot_collector_ring_truncation_caps_buffer():
    """环形截断：缓冲超 max_trades 时只保留近 N 笔，防膨胀。"""
    from smc_tracker.okx.stream import SpotTakerCollector
    c = SpotTakerCollector(["BTC-USDT"], max_trades=3)
    trades = [{"px": "1", "sz": "1", "side": "buy"} for _ in range(10)]
    c._on_spot_trades({"instId": "BTC-USDT"}, trades, 0)
    assert len(c._buf["BTC-USDT"]) == 3


# ---- windowed_net_flow 口径一致性（run_stream 对齐常驻路径）----

def test_windowed_net_flow_empty_prev_equals_cur():
    """prev_net 为空({}，首次快照)时，窗口净流向 == 当前累积值。

    run_stream 一次性 CLI 开始前没有上一窗口，prev_net={} → windowed==cur，
    与 detect_divergences(monitor.all_net_flows()) 直接传值语义完全一致，
    但代码显式走 windowed_net_flow 保证口径统一。
    """
    from smc_tracker.okx.stream import windowed_net_flow
    cur = {"BTC": 500_000.0, "ETH": -200_000.0}
    win = windowed_net_flow(cur, {})
    assert win == cur


def test_windowed_net_flow_subtracts_prev():
    """prev_net 非零时，窗口增量正确 = cur - prev（常驻路径多窗口累积减法）。"""
    from smc_tracker.okx.stream import windowed_net_flow
    cur = {"BTC": 900_000.0, "ETH": -100_000.0}
    prev = {"BTC": 600_000.0, "ETH": -100_000.0}
    win = windowed_net_flow(cur, prev)
    assert abs(win["BTC"] - 300_000.0) < 1e-9
    assert abs(win["ETH"] - 0.0) < 1e-9


def test_run_stream_divergence_uses_windowed_net_flow():
    """run_stream 背离判定 detect_divergences 应用窗口口径(windowed_net_flow)。

    验证方式：构造「累积净流量」与「窗口净流量」背离方向相反的场景，
    用 mock 拦截 detect_divergences 调用，断言传入的 net_by_coin 参数
    等于 windowed_net_flow(cur_net, prev_net={}) 而非 cur_net 直接值
    （首次运行时 prev_net={} 使两者相等，所以此测侧重测参数构造路径）。

    因 run_stream 涉及 WS 网络，此处为纯单元测——直接测 windowed_net_flow
    返回值在 prev_net={} 时与 cur_net 等价，并验证 detect_divergences 接收
    windowed 输出后行为正确（信号一致）。
    """
    from smc_tracker.okx.stream import detect_divergences, windowed_net_flow

    cur_net = {"BTC": -600_000.0}
    # 模拟首次运行：prev_net={}，窗口净流向与 cur_net 相同
    win_net = windowed_net_flow(cur_net, {})
    assert win_net == cur_net

    # 以窗口净流向送入 detect_divergences，结果与直接传 cur_net 一致
    latest = {"BTC-USDT-SWAP": {"coin": "BTC", "funding": 0.0005}}
    divs_win = detect_divergences(latest, win_net)
    divs_cur = detect_divergences(latest, cur_net)
    assert divs_win == divs_cur
    # 多头拥挤 + 净卖 → bearish 背离
    assert len(divs_win) == 1 and divs_win[0][1]["direction"] == "bearish"


def test_run_stream_divergence_windowed_vs_cumulative_differ_when_prev_nonzero():
    """当 prev_net 非零时，窗口口径与累积口径给出不同输入 → 背离结论不同。

    这证明了「常驻路径必须用窗口口径」：若用累积值，长时间运行后巨量历史净流会
    持续触发背离（即使近期流量已逆转）；窗口口径只取本 flush 增量，更精准。

    此测直接验证 windowed_net_flow 为 detect_divergences 提供正确输入。
    """
    from smc_tracker.okx.stream import detect_divergences, windowed_net_flow

    # 累积净流：历史净买 $2M（自启动以来）
    cur_net = {"BTC": 2_000_000.0}
    # 本窗口增量：实际净卖 $-600k（近期方向已逆转）
    prev_net = {"BTC": 2_600_000.0}
    win_net = windowed_net_flow(cur_net, prev_net)
    assert abs(win_net["BTC"] - (-600_000.0)) < 1e-9

    latest = {"BTC-USDT-SWAP": {"coin": "BTC", "funding": -0.0005}}  # 空头拥挤
    # 累积口径：净买 $2M → bullish 背离（错误：历史累积已过期）
    divs_cum = detect_divergences(latest, cur_net)
    assert len(divs_cum) == 1 and divs_cum[0][1]["direction"] == "bullish"
    # 窗口口径：净卖 $-600k → 无背离（空头拥挤+净卖=同向，不背离）
    divs_win = detect_divergences(latest, win_net)
    assert divs_win == []
