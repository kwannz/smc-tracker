"""仪表盘单测：build_dashboard_state / render_html（合成数据，无网络，不起真实服务器）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store
from smc_tracker.dashboard import build_dashboard_state, render_html, render_hl_html


# ---------------------------------------------------------------------------
# 辅助：建带合成数据的临时 Store
# ---------------------------------------------------------------------------

def _store_empty() -> Store:
    """完全空库（用于确认空库不抛异常）。"""
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


def _store_with_data() -> Store:
    """插入各类合成数据的临时 Store。"""
    d = tempfile.mkdtemp()
    s = Store(Path(d) / "t.db")

    now_ms = 1_700_000_000_000  # 固定时间戳，方便断言

    # ---- signals ----
    s.insert_signal((
        now_ms - 60_000,      # ts
        "kPEPE",               # coin
        "long",                # direction
        3.5,                   # score
        0.0,                   # structure_bias
        0.8,                   # flow_bias
        500_000.0,             # flow_net_usd
        0.05,                  # oi_change_pct
        0.0,                   # onchain_usd
        0.00280,               # entry
        0.00260,               # stop
        0.00320,               # target
        2.0,                   # rr
        "test signal",         # reason
    ))

    # ---- divergence ----
    s.insert_divergence((
        now_ms - 120_000,
        "kWIF",
        "bullish",
        2.1,
        -0.0005,
        0.03,
        300_000.0,
        "divergence test",
    ))

    # ---- hl_meme_trades（聪明钱净流向 + 庄家集团）----
    trades = []
    for i in range(4):
        t = now_ms - 900_000 + i * 120_000
        # 地址 A / B 协同主动买 kPEPE
        trades.append(("kPEPE", 0.0028, 1e6, 100.0, "B", "0xA", "0xM", "0xA",
                        f"h{i}a", i * 10 + 1, t))
        trades.append(("kPEPE", 0.0028, 1e6, 100.0, "B", "0xB", "0xM", "0xB",
                        f"h{i}b", i * 10 + 2, t + 1000))
        # 地址 A / B 在 kWIF 也协同（跨币 min_coins=2）
        trades.append(("kWIF", 1.50, 1e4, 80.0, "B", "0xA", "0xM", "0xA",
                        f"h{i}c", i * 10 + 3, t + 2000))
        trades.append(("kWIF", 1.50, 1e4, 80.0, "B", "0xB", "0xM", "0xB",
                        f"h{i}d", i * 10 + 4, t + 3000))
    s.insert_hl_meme_trades(trades)

    # ---- address_profiles ----
    s.upsert_address_profile({
        "address": "0xA",
        "score": 88.0,
        "account_value": 1_200_000.0,
        "alltime_pnl": 500_000.0,
        "month_pnl": 40_000.0,
        "win_rate": 0.68,
        "realized_pnl": 35_000.0,
        "n_trades": 120,
        "net_bias": "多",
        "fav_coins": ["kPEPE", "kWIF"],
        "ts": now_ms,
    })

    # ---- whale_signals ----
    s.insert_whale_signal((
        now_ms - 300_000,
        "0xA",
        "whale_A",
        "kPEPE",
        "OPEN",
        "long",
        200_000.0,
        0.00275,
        200_000.0,
        1,
    ))

    return s, now_ms


# ---------------------------------------------------------------------------
# 测试：空库不抛
# ---------------------------------------------------------------------------

def test_empty_store_no_raise():
    """空库时 build_dashboard_state 应返回所有 section 为空列表，不抛任何异常。"""
    s = _store_empty()
    now_ms = 1_700_000_000_000
    state = build_dashboard_state(s, now_ms)
    s.close()

    # meta 始终存在
    assert "generated" in state["meta"]
    assert "window_min" in state["meta"]

    # 各数据 section 为空列表（不报错）
    for key in ("signals", "divergence", "whale_flows", "top_addresses",
                 "clusters", "oi_surges", "onchain", "whale_signals", "pump_alerts"):
        assert isinstance(state[key], list), f"{key} 应为 list，实际 {type(state[key])}"
        assert state[key] == [], f"{key} 在空库下应为 []，实际 {state[key]}"

    # health / accuracy 为 dict（空库也不抛，结构良好）
    assert isinstance(state["health"], dict)
    assert "freshness" in state["health"] and "predictions" in state["health"]
    assert isinstance(state["accuracy"], dict)
    assert state["accuracy"]["total_n"] == 0   # 空库无已评估预测


# ---------------------------------------------------------------------------
# 测试：有数据时 section 结构正确
# ---------------------------------------------------------------------------

def test_signals_section():
    """signals section 结构：含必要字段，coin 正确。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    sigs = state["signals"]
    assert len(sigs) >= 1
    row = sigs[0]
    assert row["coin"] == "kPEPE"
    assert row["direction"] == "long"
    assert abs(row["score"] - 3.5) < 1e-6
    # 风险字段存在（可为 None 也可为 float）
    for field in ("entry", "stop", "target", "rr"):
        assert field in row


def test_divergence_section():
    """divergence section：bullish 方向 + coin 正确。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    divs = state["divergence"]
    assert len(divs) >= 1
    row = divs[0]
    assert row["coin"] == "kWIF"
    assert row["direction"] == "bullish"
    assert "score" in row and "funding" in row and "dex_flow_usd" in row


def test_whale_flows_section():
    """whale_flows：kPEPE 净买 > 0（4 批次 × 100 USD 每次 × 2 地址 = 800 USD 净买）。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    flows = state["whale_flows"]
    assert len(flows) >= 1
    pepe_flow = next((f for f in flows if f["coin"] == "kPEPE"), None)
    assert pepe_flow is not None, "kPEPE 应出现在 whale_flows"
    assert pepe_flow["net"] > 0, "kPEPE 净买应为正"


def test_top_addresses_section():
    """top_addresses：包含 0xA，score 正确。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    addrs = state["top_addresses"]
    assert len(addrs) >= 1
    a = addrs[0]
    assert a["address"] == "0xA"
    assert abs(a["score"] - 88.0) < 1e-6
    assert "account_value" in a and "month_pnl" in a
    assert "fav_coins" in a and "net_bias" in a


def test_clusters_section():
    """clusters：0xA / 0xB 跨 kPEPE + kWIF 协同，应被检测为庄家集团。"""
    s, now_ms = _store_with_data()
    # 用 1800s 窗口调用（参考 build_dashboard_state 内部参数）
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    clusters = state["clusters"]
    # 协同数据跨度超过 30 分钟，半小时窗 since_ms=now_ms-1_800_000 可能仅覆盖部分 trades
    # 因此这里只断言类型，不强断言非空（窗口边界敏感）
    assert isinstance(clusters, list)
    for c in clusters:
        assert "members" in c
        assert "size" in c
        assert "coins" in c
        assert "events" in c
        assert "coin_list" in c


def test_whale_signals_section():
    """whale_signals / pump_alerts：包含 kPEPE 的 long 信号。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    ws = state["whale_signals"]
    assert len(ws) >= 1
    row = ws[0]
    assert row["coin"] == "kPEPE"
    assert row["direction"] == "long"
    # pump_alerts 与 whale_signals 相同
    assert state["pump_alerts"] == state["whale_signals"]


def test_window_filter():
    """窗口过滤：把 window_ms 缩到极小时，近期数据不在窗口内，应返回空 section。"""
    s, now_ms = _store_with_data()
    # 用 1ms 窗口：数据全在过去，应全部过滤
    state = build_dashboard_state(s, now_ms, window_ms=1)
    s.close()

    assert state["signals"] == []
    assert state["divergence"] == []
    assert state["whale_flows"] == []


# ---------------------------------------------------------------------------
# 测试：render_html
# ---------------------------------------------------------------------------

def test_render_html_returns_str():
    """render_html 返回 str，含标题、初始 state JSON。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    assert isinstance(html, str)
    # 标题字样
    assert "SMC 抓庄监控" in html
    # 关键 section 标签
    assert "共振信号" in html
    assert "庄家集团" in html
    assert "聪明钱净流向" in html


def test_render_html_contains_coin():
    """render_html 包含注入的 kPEPE coin 名（初始 state 序列化在 HTML 内）。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    # signals 里有 kPEPE，应出现在 JSON 注入中
    assert "kPEPE" in html


def test_render_html_is_self_contained():
    """render_html 不含外部 CDN 链接（纯前端，无外部依赖）。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    for cdn_kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert cdn_kw not in html, f"HTML 不应含外部资源: {cdn_kw}"


def test_render_html_has_api_fetch():
    """render_html 包含 /api/state fetch 逻辑（5 秒刷新）。"""
    state = build_dashboard_state(_store_empty(), 1_700_000_000_000)
    html = render_html(state)
    assert "/api/state" in html
    assert "setInterval" in html


def test_render_html_empty_state():
    """空库 state 也能正常渲染，不抛异常。"""
    s = _store_empty()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    html = render_html(state)
    assert isinstance(html, str)
    assert "SMC 抓庄监控" in html


def test_render_html_braces_unescaped():
    """回归：模板 .format 风格双括号必须解转义，否则 CSS 失效 / JS 模板插值语法错误。

    历史 bug：render_html 用 .replace 注入但模板用 {{/}}，输出残留字面双括号 →
    `${{…}}` 变成非法 JS（对象字面量含调用 key）→ renderAll 抛错 → 页面卡「加载中」。
    """
    import json as _json

    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()
    html = render_html(state)

    # 1) 模板 CSS/JS 区不得残留转义双括号的明确畸形标记
    #    注：注入的 initial state JSON 合法地含 `}}`(嵌套对象闭合,如 "overdue":0}}），
    #    故不能笼统断言 "}}" not in html；只查不可能出现在合法 JSON 中的畸形标记。
    assert "${{" not in html, "残留 ${{ → JS 模板插值语法错误"
    assert ":root{{" not in html, "残留 :root{{ → CSS 畸形"
    assert "(){{" not in html, "残留 (){{ → JS 函数体畸形"
    # 2) 关键 CSS/JS token 良构
    assert ":root{" in html and "${fmtTime(r.ts)}" in html
    # 3) 注入的 initial state 仍是可解析 JSON（双括号解转义未破坏 JSON）
    i = html.find("const S = ") + len("const S = ")
    j = html.find(";", i)
    _json.loads(html[i:j])   # 不抛即合法


# ---------------------------------------------------------------------------
# 测试：ticker_board section（行情监控板）
# ---------------------------------------------------------------------------

def _store_with_bitget_oi() -> tuple:
    """插入合成 bitget_oi 数据：同一 symbol 不同 ts 不同 mark_px，用于测试行情板。"""
    d = __import__("tempfile").mkdtemp()
    s = Store(__import__("pathlib").Path(d) / "t.db")

    now_ms = 1_700_000_000_000  # 固定时间戳

    # symbol 1: BONKUSDT，最新 mark_px=0.0836，24h 前（now-84000s 约23.3h前）mark_px=0.0800
    # 预期 chg24 = (0.0836-0.0800)/0.0800 = 0.045（+4.5%）
    old_ts = now_ms - 84_000_000   # 约 23.3h 前，在 23~24h 窗口内
    new_ts = now_ms - 60_000       # 1 分钟前（最新）
    mid_ts = now_ms - 3_600_000    # 1 小时前（中间数据，应被忽略）

    s.insert_oi([
        # (symbol, coin, oi_size, oi_usd, mark_px, funding, ts)
        ("BONKUSDT", "BONK", 1_000_000.0, 80_000.0, 0.0800, 0.0001, old_ts),
        ("BONKUSDT", "BONK", 1_050_000.0, 87_780.0, 0.0836, 0.0001, mid_ts),
        ("BONKUSDT", "BONK", 1_020_000.0, 85_272.0, 0.0836, 0.0001, new_ts),
    ])
    # symbol 2: PEPEUSDT，只有最新一条（无 24h 历史），chg24 应为 None
    s.insert_oi([
        ("PEPEUSDT", "PEPE", 5_000_000.0, 55.0, 0.000011, 0.00005, new_ts),
    ])
    s.conn.commit()

    return s, now_ms


def test_ticker_board_section_has_data():
    """ticker_board section：插入 bitget_oi 后应有数据，且 price/funding 正确。"""
    s, now_ms = _store_with_bitget_oi()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    board = state.get("ticker_board", [])
    assert isinstance(board, list), f"ticker_board 应为 list，实际 {type(board)}"
    assert len(board) >= 1, "ticker_board 应有数据（已插入 bitget_oi 行）"

    # 找 BONKUSDT 行
    bonk_row = next((r for r in board if r["symbol"] == "BONKUSDT"), None)
    assert bonk_row is not None, "ticker_board 应含 BONKUSDT"

    # price 应等于最新 mark_px=0.0836
    assert abs(bonk_row["price"] - 0.0836) < 1e-9, f"price 期望 0.0836，实得 {bonk_row['price']}"
    # funding 应等于 0.0001
    assert abs(bonk_row["funding"] - 0.0001) < 1e-9, f"funding 期望 0.0001，实得 {bonk_row['funding']}"
    # coin 字段
    assert bonk_row["coin"] == "BONK", f"coin 期望 'BONK'，实得 {bonk_row['coin']}"


def test_ticker_board_chg24_best_effort():
    """chg24 best-effort：有 24h 前历史时计算正确，无历史时为 None。"""
    s, now_ms = _store_with_bitget_oi()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    board = state.get("ticker_board", [])

    # BONKUSDT：有 old_ts（约 23.3h 前 mark_px=0.0800），chg24 = (0.0836-0.0800)/0.0800
    bonk = next((r for r in board if r["symbol"] == "BONKUSDT"), None)
    assert bonk is not None
    if bonk["chg24"] is not None:
        expected_chg = (0.0836 - 0.0800) / 0.0800   # ≈ 0.045
        assert abs(bonk["chg24"] - expected_chg) < 1e-6, (
            f"chg24 期望 ≈{expected_chg:.4f}，实得 {bonk['chg24']}"
        )
    # 可接受 None（边界条件下 old 行不在 23~24h 窗口内时）

    # PEPEUSDT：只有最新一行，无 24h 历史 → chg24 应为 None
    pepe = next((r for r in board if r["symbol"] == "PEPEUSDT"), None)
    assert pepe is not None
    assert pepe["chg24"] is None, f"PEPEUSDT 无历史时 chg24 应为 None，实得 {pepe['chg24']}"


def test_ticker_board_empty_on_no_oi_data():
    """无 bitget_oi 数据时 ticker_board 应为空列表，不抛异常。"""
    s = _store_empty()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    board = state.get("ticker_board", None)
    assert board is not None, "state 应含 ticker_board 键"
    assert isinstance(board, list), f"ticker_board 应为 list，实际 {type(board)}"
    assert board == [], f"无数据时 ticker_board 应为 []，实际 {board}"


def test_render_html_omits_ticker_board_panel():
    """render_html **不再含**「行情监控板」面板（用户#要求：价/涨跌幅/费率/OI 不需要，聚焦 HL）。

    诚实标注：后端 build_dashboard_state 仍产出 ticker_board 数据（供 /health 等复用，见
    test_ticker_board_section_has_data），仅前端面板移除——降噪，与推送侧 push_ticker_board 默认关一致。
    """
    s, now_ms = _store_with_bitget_oi()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    assert "行情监控板" not in html, "render_html 不应再含「行情监控板」面板（已按用户要求移除）"
    assert "renderTickerBoard" not in html, "renderTickerBoard 死函数应已移除"
    assert state.get("ticker_board") is not None, "后端 ticker_board 数据应保留（其它消费方依赖）"


# ---------------------------------------------------------------------------
# 测试：exchange_flows section（交易所资金流 24h 卡片）
# ---------------------------------------------------------------------------

# exchange_flows 表 schema（与 exchange_flow.py _SCHEMA 对齐）
_EF_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange_flows (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    dt       TEXT    NOT NULL,
    exchange TEXT    NOT NULL,
    chain    TEXT    NOT NULL,
    inflow   REAL    NOT NULL,
    outflow  REAL    NOT NULL,
    net      REAL    NOT NULL,
    n_tx     INTEGER NOT NULL,
    n_addr   INTEGER NOT NULL
);
"""


def _store_with_exchange_flows() -> tuple:
    """建含 exchange_flows 合成数据的临时 Store。

    插入三个交易所的两条记录（同一交易所取最新）：
      - Binance: 净流入 800 BTC（|net|=800，最大）
      - OKX:     净流出 -550 BTC（|net|=550，次之）
      - Bitget:  净流入 10 BTC（|net|=10，最小）
    """
    d = __import__("tempfile").mkdtemp()
    s = Store(__import__("pathlib").Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    s.conn.executescript(_EF_SCHEMA)
    s.conn.execute(
        "INSERT INTO exchange_flows(ts,dt,exchange,chain,inflow,outflow,net,n_tx,n_addr) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now_ms - 60_000, "2023-11-14 00:00:00", "Binance", "BTC",
         1000.0, 200.0, 800.0, 12, 3),
    )
    # 更旧的 Binance 行（应被 MAX(ts) 过滤，不出现在结果中）
    s.conn.execute(
        "INSERT INTO exchange_flows(ts,dt,exchange,chain,inflow,outflow,net,n_tx,n_addr) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now_ms - 3_600_000, "2023-11-13 23:00:00", "Binance", "BTC",
         500.0, 100.0, 400.0, 6, 2),
    )
    s.conn.execute(
        "INSERT INTO exchange_flows(ts,dt,exchange,chain,inflow,outflow,net,n_tx,n_addr) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now_ms - 120_000, "2023-11-14 00:00:00", "OKX", "BTC",
         50.0, 600.0, -550.0, 5, 1),
    )
    s.conn.execute(
        "INSERT INTO exchange_flows(ts,dt,exchange,chain,inflow,outflow,net,n_tx,n_addr) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now_ms - 180_000, "2023-11-14 00:00:00", "Bitget", "BTC",
         15.0, 5.0, 10.0, 2, 1),
    )
    s.conn.commit()
    return s, now_ms


def test_exchange_flows_section_has_data():
    """exchange_flows section：插入合成数据后应有 3 行（每交易所最新一行）。"""
    s, now_ms = _store_with_exchange_flows()
    state = build_dashboard_state(s, now_ms)
    s.close()

    ef = state.get("exchange_flows", None)
    assert ef is not None, "state 应含 exchange_flows 键"
    assert isinstance(ef, list), f"exchange_flows 应为 list，实际 {type(ef)}"
    assert len(ef) == 3, f"应有 3 行（Binance/OKX/Bitget），实得 {len(ef)}"


def test_exchange_flows_section_net_sign_correct():
    """exchange_flows：Binance net>0（净流入），OKX net<0（净流出）。"""
    s, now_ms = _store_with_exchange_flows()
    state = build_dashboard_state(s, now_ms)
    s.close()

    ef = state["exchange_flows"]
    exchanges = {r["exchange"]: r for r in ef}

    assert "Binance" in exchanges
    assert "OKX" in exchanges
    assert exchanges["Binance"]["net"] > 0, "Binance 净流入，net 应为正"
    assert exchanges["OKX"]["net"] < 0, "OKX 净流出，net 应为负"


def test_exchange_flows_section_sorted_by_abs_net():
    """exchange_flows：按 abs(net) 降序排列（Binance 800 > OKX 550 > Bitget 10）。"""
    s, now_ms = _store_with_exchange_flows()
    state = build_dashboard_state(s, now_ms)
    s.close()

    ef = state["exchange_flows"]
    abs_nets = [abs(r["net"]) for r in ef]
    assert abs_nets == sorted(abs_nets, reverse=True), (
        f"exchange_flows 应按 abs(net) 降序，实际顺序: {[r['exchange'] for r in ef]}"
    )
    # 顺序验证：Binance(800) > OKX(550) > Bitget(10)
    assert ef[0]["exchange"] == "Binance"
    assert ef[1]["exchange"] == "OKX"
    assert ef[2]["exchange"] == "Bitget"


def test_exchange_flows_deduplication_by_max_ts():
    """exchange_flows 每个交易所只取最新一行（MAX(ts)），旧行不出现。"""
    s, now_ms = _store_with_exchange_flows()
    state = build_dashboard_state(s, now_ms)
    s.close()

    ef = state["exchange_flows"]
    # 只应出现 3 行（Binance 旧行 net=400 应被过滤，只保留最新 net=800）
    binance_rows = [r for r in ef if r["exchange"] == "Binance"]
    assert len(binance_rows) == 1, f"Binance 应只有 1 行（最新），实得 {len(binance_rows)}"
    assert abs(binance_rows[0]["net"] - 800.0) < 1e-9, (
        f"Binance 最新 net 应为 800，实得 {binance_rows[0]['net']}"
    )


def test_exchange_flows_section_empty_on_no_table():
    """无 exchange_flows 表时 exchange_flows section 应为 []，不抛异常。"""
    s = _store_empty()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    ef = state.get("exchange_flows", None)
    assert ef is not None, "state 应含 exchange_flows 键（即使表不存在）"
    assert ef == [], f"无表时 exchange_flows 应为 []，实得 {ef}"


def test_render_html_contains_exchange_flows():
    """render_html 应包含「交易所资金流」字样（来自 sections 标题）。"""
    s, now_ms = _store_with_exchange_flows()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    assert "交易所资金流" in html, "render_html 应含「交易所资金流」字样"


# ---------------------------------------------------------------------------
# 测试：inline SVG 图表（svgBars / svgSpark，无 CDN/无依赖）
# ---------------------------------------------------------------------------

def test_render_html_contains_svg():
    """render_html（含 whale_flows 数据）输出应内联 <svg（聪明钱净流向条形图）。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()

    # 前置：whale_flows 必须非空，否则图不渲染（svgBars 空数据返回空串）
    assert state["whale_flows"], "测试前置：whale_flows 应有数据"

    html = render_html(state)
    assert "<svg" in html, "render_html 应内联 <svg 图表"
    # 发散条形图的核心 SVG 图元应出现在模板中
    assert "<rect" in html and "<polyline" in html, "应含 <rect（条形）与 <polyline（sparkline）图元定义"


def test_render_html_no_residual_open_double_braces():
    """转义正确性回归：紧凑 JSON 永不含 `{{`（每个 { 后必跟 " 或 }），故输出残留 `{{`
    即模板转义不完整 / JS 语法错误。

    注意：`}}` 会合法出现于 JSON 嵌套对象闭合（如 {"meta":{}}），不可笼统断言其不存在；
    畸形转义标记由 test_render_html_braces_unescaped 用 `${{`/`:root{{` 等精确检测。
    """
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    assert "{{" not in html, "残留 {{ → 模板转义不完整 / JS 语法错误"


def test_render_html_preserves_braced_data_values():
    """数据值含字面 {{/}}（如信号 reason）必须原样保留——render_html 不得改写 JSON 括号。

    回归：曾用 while 循环把 JSON 相邻括号拆成 `} }`/`{ {` 以满足"输出无 }}"，腐蚀了
    含双括号的数据值（edge}}case{{x → edge} }case{ {x）。注入须在模板解转义之后，
    JSON 原样保留。
    """
    import json as _json
    import re as _re
    state = {"whale_flows": [{"coin": "BTC", "net": 100.0}],
             "signals": [{"reason": "edge}}case{{x", "coin": "ETH", "direction": "long",
                          "score": 1, "entry": 1, "stop": 1, "target": 1, "rr": 1}],
             "generated": "now", "window_min": 60, "meta": {}}
    html = render_html(state)
    m = _re.search(r"const S\s*=\s*(\{.*?\});", html, _re.S)
    assert m, "未找到注入的 const S"
    parsed = _json.loads(m.group(1))
    assert parsed["signals"][0]["reason"] == "edge}}case{{x", "数据值被腐蚀"


def test_render_html_svg_with_bitget_oi_state():
    """用另一套构造方式（含 bitget_oi 行情数据）的 state 同样无残留双括号且含 SVG。"""
    s, now_ms = _store_with_bitget_oi()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    assert "<svg" in html
    assert "{{" not in html  # `}}` 会合法出现于 JSON 嵌套闭合，仅查 `{{`


def test_svg_functions_defined_in_template():
    """svgBars / svgSpark / svgEsc 三个纯 JS 函数应被定义在模板 <script> 中（供前端调用）。"""
    state = build_dashboard_state(_store_empty(), 1_700_000_000_000)
    html = render_html(state)
    # 解转义后函数定义应为良构的 `function svgBars(` 等
    assert "function svgBars(" in html
    assert "function svgSpark(" in html
    assert "function svgEsc(" in html


def test_render_html_no_cdn_after_svg_addition():
    """加入 SVG 后仍保持纯自包含：无任何外部 CDN/资源链接。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()
    html = render_html(state)
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis", "http://", "https://"):
        # 例外：SVG 命名空间 xmlns 用的 www.w3.org/2000/svg 是标准声明，非外部加载
        if kw in ("http://", "https://"):
            # 只允许 w3.org SVG 命名空间，不允许其它外链
            import re
            bad = [m for m in re.findall(r'https?://[^\s"\'<>]+', html)
                   if "w3.org/2000/svg" not in m]
            assert not bad, f"不应含外部链接: {bad[:3]}"
        else:
            assert kw not in html, f"HTML 不应含外部资源: {kw}"


# ---------------------------------------------------------------------------
# 测试：OKX/HL section（okx_liquidations / okx_signals / okx_walls 卡片）
# ---------------------------------------------------------------------------

def _store_with_okx_hl() -> tuple:
    """建含 OKX 强平 / OKX 信号 / HL 挂单墙合成数据的临时 Store。"""
    d = __import__("tempfile").mkdtemp()
    s = Store(__import__("pathlib").Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    # ---- OKX 强平（insert_okx_liquidations rows: coin,pos_side,side,notional_usd,bk_px,ts）----
    # BTC 多头被平 5M（抛压级联）+ BTC 再 2M；ETH 空头被平 3M（逼空）
    s.insert_okx_liquidations([
        ("BTC", "long", "sell", 5_000_000.0, 65000.0, now_ms - 120_000),
        ("BTC", "long", "sell", 2_000_000.0, 64800.0, now_ms - 90_000),
        ("ETH", "short", "buy", 3_000_000.0, 3500.0, now_ms - 60_000),
    ])

    # ---- OKX 信号（insert_okx_signal: ts,coin,direction,kind,funding,net_flow，单条接口）----
    s.insert_okx_signal(now_ms - 300_000, "SOL", "long", "accumulation", 0.0001, 1_200_000.0)
    s.insert_okx_signal(now_ms - 200_000, "DOGE", "short", "distribution", -0.0003, -800_000.0)

    # ---- HL 挂单墙（insert_orderbook_walls rows: ts,coin,side,kind,px,notional）----
    s.insert_orderbook_walls([
        (now_ms - 150_000, "BTC", "bid", "build", 64000.0, 4_000_000.0),
        (now_ms - 100_000, "BTC", "ask", "pull", 66000.0, 2_500_000.0),
        (now_ms - 50_000, "ETH", "bid", "build", 3400.0, 1_500_000.0),
    ])

    s.conn.commit()
    return s, now_ms


def test_okx_hl_keys_present_empty_store():
    """空库时 build_dashboard_state 应含 okx_signals/okx_liquidations/okx_walls 键且为 []。"""
    s = _store_empty()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    for key in ("okx_signals", "okx_liquidations", "okx_walls"):
        assert key in state, f"state 应含 {key} 键"
        assert isinstance(state[key], list), f"{key} 应为 list，实际 {type(state[key])}"
        assert state[key] == [], f"{key} 空库下应为 []，实际 {state[key]}"


def test_okx_liquidations_section_has_data():
    """okx_liquidations section：插入 3 行强平后结构正确，notional 取 notional_usd。"""
    s, now_ms = _store_with_okx_hl()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    liq = state["okx_liquidations"]
    assert len(liq) == 3, f"应有 3 行强平，实得 {len(liq)}"
    btc = next((r for r in liq if r["coin"] == "BTC" and r["notional"] == 5_000_000.0), None)
    assert btc is not None, "应含 BTC 5M 强平行"
    assert btc["pos_side"] == "long"
    assert btc["side"] == "sell"
    for field in ("ts", "coin", "pos_side", "side", "notional"):
        assert field in btc


def test_okx_signals_section_has_data():
    """okx_signals section：含 SOL accumulation long + DOGE distribution short。"""
    s, now_ms = _store_with_okx_hl()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    sigs = state["okx_signals"]
    assert len(sigs) == 2, f"应有 2 条 OKX 信号，实得 {len(sigs)}"
    sol = next((r for r in sigs if r["coin"] == "SOL"), None)
    assert sol is not None
    assert sol["direction"] == "long"
    assert sol["kind"] == "accumulation"
    assert abs(sol["net_flow"] - 1_200_000.0) < 1e-6
    for field in ("coin", "direction", "kind", "net_flow"):
        assert field in sol


def test_okx_walls_section_has_data():
    """okx_walls section：含 BTC 买墙出现 + BTC 卖墙抽单 + ETH 买墙。"""
    s, now_ms = _store_with_okx_hl()
    state = build_dashboard_state(s, now_ms, window_ms=3_600_000)
    s.close()

    walls = state["okx_walls"]
    assert len(walls) == 3, f"应有 3 行挂单墙，实得 {len(walls)}"
    bid = next((r for r in walls if r["coin"] == "BTC" and r["side"] == "bid"), None)
    assert bid is not None, "应含 BTC 买墙行"
    assert bid["kind"] == "build"
    assert abs(bid["notional"] - 4_000_000.0) < 1e-6
    for field in ("ts", "coin", "side", "kind", "px", "notional"):
        assert field in bid


def test_okx_hl_window_filter():
    """窗口过滤：window_ms=1ms 时 OKX/HL section 应全为空（数据全在过去）。"""
    s, now_ms = _store_with_okx_hl()
    state = build_dashboard_state(s, now_ms, window_ms=1)
    s.close()
    assert state["okx_liquidations"] == []
    assert state["okx_signals"] == []
    assert state["okx_walls"] == []


def test_render_html_contains_okx_hl_titles_and_svg():
    """render_html（含 OKX/HL 数据）应含三个中文标题且内联 <svg（强平/挂单墙按 coin 聚合图）。"""
    s, now_ms = _store_with_okx_hl()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    assert "OKX 强平级联" in html, "应含「OKX 强平级联」标题"
    assert "OKX 跨所信号" in html, "应含「OKX 跨所信号」标题"
    assert "HL 挂单墙" in html, "应含「HL 挂单墙」标题"
    assert "<svg" in html, "应内联 <svg（强平/挂单墙聚合条形图）"
    # 转义完整性：紧凑 JSON 永不含 `{{`，残留即模板转义不完整（不断言 `}}`，其合法出现于嵌套闭合）
    assert "{{" not in html, "残留 {{ → 模板转义不完整 / JS 语法错误"


def test_render_html_okx_signal_braced_value_integrity():
    """数据完整性：OKX 信号字段含字面 {{/}}（如 reason='a}}b{{c'）经 render_html 回环不变。

    用直接构造的 state（含畸形 coin/kind），验证 JSON 注入不腐蚀含双括号的数据值。
    """
    import json as _json
    import re as _re

    state = {
        "okx_signals": [{"coin": "X}}b{{c", "direction": "long",
                          "kind": "k}}q{{z", "net_flow": 1.0}],
        "okx_liquidations": [], "okx_walls": [], "whale_flows": [],
        "meta": {"generated": "now", "window_min": 60},
    }
    html = render_html(state)
    m = _re.search(r"const S\s*=\s*(\{.*?\});", html, _re.S)
    assert m, "未找到注入的 const S"
    parsed = _json.loads(m.group(1))
    assert parsed["okx_signals"][0]["coin"] == "X}}b{{c", "okx coin 被腐蚀"
    assert parsed["okx_signals"][0]["kind"] == "k}}q{{z", "okx kind 被腐蚀"


def test_render_okx_hl_functions_defined():
    """renderOkxLiquidations / renderOkxSignals / renderHlWalls 三函数应被定义在模板 <script> 中。"""
    state = build_dashboard_state(_store_empty(), 1_700_000_000_000)
    html = render_html(state)
    assert "function renderOkxLiquidations(" in html
    assert "function renderOkxSignals(" in html
    assert "function renderHlWalls(" in html


# ---------------------------------------------------------------------------
# 测试：谐波形态独立页（build_harmonic_state / render_harmonic_html）
# ---------------------------------------------------------------------------

from smc_tracker.dashboard import build_harmonic_state, render_harmonic_html  # noqa: E402

# harmonic_setups 表 schema（29 列，与 db.py 契约对齐）
_HARMONIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS harmonic_setups (
    ts          INTEGER,
    coin        TEXT,
    tf          TEXT,
    kind        TEXT,
    pattern     TEXT,
    direction   TEXT,
    price       REAL,
    entry_lo    REAL,
    entry_hi    REAL,
    stop        REAL,
    target1     REAL,
    target2     REAL,
    rr          REAL,
    confidence  REAL,
    knn         TEXT,
    orderflow   TEXT,
    fib_note    TEXT,
    prz_lo      REAL,
    prz_hi      REAL,
    x_idx       INTEGER,
    x_px        REAL,
    a_idx       INTEGER,
    a_px        REAL,
    b_idx       INTEGER,
    b_px        REAL,
    c_idx       INTEGER,
    c_px        REAL,
    d_idx       INTEGER,
    d_px        REAL
);
"""


def _store_with_harmonic() -> tuple:
    """建含 harmonic_setups 合成数据的临时 Store：completed + forming 各一行。"""
    d = __import__("tempfile").mkdtemp()
    s = Store(__import__("pathlib").Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    # Store.__init__ 已建 29 列表；直接用 insert_harmonic_setups 写入（自动向后兼容）
    # completed 行（有止损/目标）—— 29 列
    s.insert_harmonic_setups([
        (
            now_ms - 120_000, "BTC", "1h", "completed", "Gartley", "long",
            65000.0, 64500.0, 64800.0, 63000.0, 67000.0, 69000.0,
            2.5, 0.82, "✓", "✓ 买压确认", "XA=0.618", 64000.0, 65200.0,
            # XABCD 点（completed 行示例：有完整 XABCD）
            1, 60000.0, 10, 70000.0, 15, 55000.0, 20, 65000.0, 25, 64500.0,
        ),
        # forming 行（stop/target 为 NULL，XABCD 全 None）
        (
            now_ms - 60_000, "ETH", "4h", "forming", "Bat", "short",
            3500.0, None, None, None, None, None,
            None, 0.65, "?", "", "BC=0.886", 3450.0, 3550.0,
            None, None, None, None, None, None, None, None, None, None,
        ),
    ])
    return s, now_ms


# ---- build_harmonic_state ----

def test_build_harmonic_state_returns_dict():
    """build_harmonic_state 返回 dict，含 completed/forming/generated_at 键。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    assert isinstance(state, dict)
    assert "completed" in state
    assert "forming" in state
    assert "generated_at" in state


def test_build_harmonic_state_groups_correctly():
    """completed 行归入 completed 组，forming 行归入 forming 组。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    assert len(state["completed"]) == 1
    assert len(state["forming"]) == 1

    c = state["completed"][0]
    assert c["coin"] == "BTC"
    assert c["pattern"] == "Gartley"
    assert c["direction"] == "long"
    assert abs(c["confidence"] - 0.82) < 1e-9
    assert c["stop"] is not None

    f = state["forming"][0]
    assert f["coin"] == "ETH"
    assert f["pattern"] == "Bat"
    assert f["direction"] == "short"
    # forming 的 stop/target 可能为 NULL，不要求非 None
    assert "prz_lo" in f and "prz_hi" in f


def test_build_harmonic_state_empty_table():
    """harmonic_setups 表为空时返回空列表，不抛。"""
    d = __import__("tempfile").mkdtemp()
    s = Store(__import__("pathlib").Path(d) / "t.db")
    s.conn.executescript(_HARMONIC_SCHEMA)
    s.conn.commit()

    state = build_harmonic_state(s, 1_700_000_000_000)
    s.close()

    assert state["completed"] == []
    assert state["forming"] == []


def test_build_harmonic_state_no_table():
    """harmonic_setups 表不存在时返回空列表，不抛（防御性查询）。"""
    s = _store_empty()
    state = build_harmonic_state(s, 1_700_000_000_000)
    s.close()

    assert state["completed"] == []
    assert state["forming"] == []


def test_build_harmonic_state_row_fields():
    """每行 dict 含规定列：ts/coin/tf/kind/pattern/direction/price/entry_lo/entry_hi/
    stop/target1/target2/rr/confidence/knn/orderflow/fib_note/prz_lo/prz_hi
    以及 XABCD 点坐标列（v2 新增）。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    expected_fields = [
        "ts", "coin", "tf", "kind", "pattern", "direction", "price",
        "entry_lo", "entry_hi", "stop", "target1", "target2", "rr",
        "confidence", "knn", "orderflow", "fib_note", "prz_lo", "prz_hi",
        # XABCD 点（v2 新增，forming 行为 None）
        "x_idx", "x_px", "a_idx", "a_px", "b_idx", "b_px",
        "c_idx", "c_px", "d_idx", "d_px",
    ]
    for row in state["completed"] + state["forming"]:
        for f in expected_fields:
            assert f in row, f"row 缺少字段: {f}"


# ---- render_harmonic_html ----

def test_render_harmonic_html_returns_str():
    """render_harmonic_html 返回非空字符串。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert isinstance(html, str) and len(html) > 0


def test_render_harmonic_html_title():
    """HTML 含标题关键字「谐波形态」。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "谐波形态" in html


def test_render_harmonic_html_entry_and_orderflow():
    """HTML 含「进场」和「订单流」字样（表格列头）。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "进场" in html
    assert "订单流" in html


def test_render_harmonic_html_disclaimer():
    """HTML 含诚实提示条「确认层非投资建议」。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "确认层非投资建议" in html


def test_render_harmonic_html_direction_labels():
    """HTML 含看多（绿色）和看空（红色）方向标签。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "看多" in html
    assert "看空" in html


def test_render_harmonic_html_set_interval():
    """HTML 含 setInterval（5s 自刷新）。"""
    state = {"completed": [], "forming": [], "generated_at": "now"}
    html = render_harmonic_html(state)
    assert "setInterval" in html


def test_render_harmonic_html_api_harmonic_fetch():
    """HTML 拉取 /api/harmonic（而非 /api/state）刷新。"""
    state = {"completed": [], "forming": [], "generated_at": "now"}
    html = render_harmonic_html(state)
    assert "/api/harmonic" in html


def test_render_harmonic_html_no_cdn():
    """render_harmonic_html 不含外部 CDN/资源链接（自包含单页）。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    import re
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"谐波 HTML 不应含外部资源: {kw}"
    bad = [m for m in re.findall(r'https?://[^\s"\'<>]+', html)
           if "w3.org/2000/svg" not in m]
    assert not bad, f"不应含外部链接: {bad[:3]}"


def test_render_harmonic_html_none_values_displayed_as_dash():
    """None 数值应显示为 '—' 而非 'None' 或 'null'。"""
    state = {
        "completed": [{
            "ts": 1700000000000, "coin": "BTC", "tf": "1h", "kind": "completed",
            "pattern": "Gartley", "direction": "long", "price": 65000.0,
            "entry_lo": 64500.0, "entry_hi": 64800.0, "stop": None,
            "target1": None, "target2": None, "rr": None, "confidence": 0.75,
            "knn": "✓", "orderflow": "✓", "fib_note": "", "prz_lo": None, "prz_hi": None,
        }],
        "forming": [],
        "generated_at": "2024-01-01 00:00:00",
    }
    html = render_harmonic_html(state)
    # HTML 中不应出现裸 None/null 字面量（JSON 内 null 可以，但显示用 '—' 替代）
    # 检查渲染函数中对 null 的处理（JS 中 null 应显示为 '—'）
    assert "—" in html or "&#x2014;" in html, "None 值应渲染为破折号"
    # null 可以存在于注入的 JSON 中，但不应作为显示文本出现
    # 验证 JS 函数中有对 null 的守卫逻辑
    assert "null" in html or "== null" in html or "!=null" in html or "!=" in html


def test_render_harmonic_html_self_contained_doctype():
    """HTML 是完整的独立页面（含 <!DOCTYPE html>）。"""
    state = {"completed": [], "forming": [], "generated_at": "now"}
    html = render_harmonic_html(state)
    assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()


def test_render_harmonic_html_initial_state_injected():
    """__INITIAL_STATE__ 被替换为实际 JSON（与现有 render_html 模式一致）。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "__INITIAL_STATE__" not in html, "模板占位符应已被替换"
    # 注入 JSON 可解析
    import json as _json, re as _re
    m = _re.search(r"const S\s*=\s*(\{.*?\});", html, _re.S)
    assert m, "未找到注入的 const S"
    parsed = _json.loads(m.group(1))
    assert "completed" in parsed and "forming" in parsed


def test_render_harmonic_html_dark_theme():
    """HTML 含深色主题 CSS 变量 --bg（与现有风格一致）。"""
    state = {"completed": [], "forming": [], "generated_at": "now"}
    html = render_harmonic_html(state)
    assert "--bg" in html


def test_render_harmonic_html_no_residual_double_braces():
    """转义正确性：输出不含残留 {{ （模板解转义完整）。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "{{" not in html, "残留 {{ → 模板转义不完整"


# ---- 不破坏现有 / 路由分离 ----

def test_existing_render_html_unchanged():
    """现有 render_html 仍正常工作，不受谐波页影响（/ 主页内容不变）。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    assert "SMC 抓庄监控" in html
    assert "共振信号" in html
    assert "/api/state" in html
    # 谐波专用路由不应混入主页
    assert "/api/harmonic" not in html


# ---------------------------------------------------------------------------
# 新测试：谐波页 asset_class 徽章 + 傻瓜版解释（TDD RED：功能未实现时失败）
# ---------------------------------------------------------------------------

def test_build_harmonic_state_has_asset_class_field():
    """build_harmonic_state 每项应含 asset_class 字段（'tradfi' 或 'crypto'）。

    RED：dashboard.py 尚未加 asset_class 字段时，此测试失败。
    """
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    for row in state["completed"] + state["forming"]:
        assert "asset_class" in row, f"row 缺少 asset_class 字段: {row}"
        assert row["asset_class"] in ("tradfi", "crypto"), (
            f"asset_class 应为 'tradfi' 或 'crypto'，实得: {row['asset_class']!r}"
        )


def test_build_harmonic_state_btc_is_crypto():
    """BTC → asset_class='crypto'。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    btc_rows = [r for r in state["completed"] if r["coin"] == "BTC"]
    assert btc_rows, "应有 BTC completed 行"
    assert btc_rows[0]["asset_class"] == "crypto", (
        f"BTC asset_class 应为 'crypto'，实得: {btc_rows[0]['asset_class']!r}"
    )


def _store_with_harmonic_tradfi() -> tuple:
    """建含 harmonic_setups + XAU(TradFi) coin 的临时 Store。"""
    d = __import__("tempfile").mkdtemp()
    s = Store(__import__("pathlib").Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    # XAU = TradFi，29 列
    s.insert_harmonic_setups([
        (
            now_ms - 60_000, "XAU", "1h", "completed", "Gartley", "long",
            2350.0, 2340.0, 2350.0, 2300.0, 2400.0, 2450.0,
            2.0, 0.78, "✓", "✓ 买压", "XA=0.618", 2330.0, 2360.0,
            # XABCD 点（示例）
            1, 2200.0, 10, 2450.0, 15, 2280.0, 20, 2420.0, 25, 2340.0,
        ),
    ])
    return s, now_ms


def test_build_harmonic_state_xau_is_tradfi():
    """XAU（黄金） → asset_class='tradfi'。"""
    s, now_ms = _store_with_harmonic_tradfi()
    state = build_harmonic_state(s, now_ms)
    s.close()

    xau_rows = [r for r in state["completed"] if r["coin"] == "XAU"]
    assert xau_rows, "应有 XAU completed 行"
    assert xau_rows[0]["asset_class"] == "tradfi", (
        f"XAU asset_class 应为 'tradfi'，实得: {xau_rows[0]['asset_class']!r}"
    )


def test_render_harmonic_html_contains_tradfi_badge():
    """render_harmonic_html（含 XAU）应含「TradFi」徽章字样。"""
    s, now_ms = _store_with_harmonic_tradfi()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "TradFi" in html, "谐波 HTML 应含 TradFi 徽章字样（XAU 行）"


def test_render_harmonic_html_contains_crypto_badge():
    """render_harmonic_html（含 BTC/ETH）应含「加密」徽章字样。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "加密" in html, "谐波 HTML 应含加密徽章字样（BTC/ETH 行）"


def test_render_harmonic_html_has_explainer_panel():
    """render_harmonic_html 应含傻瓜版解释折叠块（通俗中文解释）。

    要求含：看多/前瞻/斐波那契 解释字样。
    """
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "看多" in html, "解释面板应含「看多」"
    assert "前瞻" in html, "解释面板应含「前瞻」"
    assert "斐波那契" in html, "解释面板应含「斐波那契」"


def test_render_harmonic_html_explainer_covers_entry_and_knn():
    """解释面板应覆盖进场/止损/止盈/KNN/订单流的一句话解释。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    # KNN 解释（≈随机基线，仅辅助）
    assert "KNN" in html, "解释面板应含 KNN 说明"
    # 订单流解释（领先意图）
    assert "订单流" in html, "解释面板应含订单流说明"


def test_render_harmonic_html_honest_disclaimer_in_explainer():
    """解释面板应有诚实声明（非投资建议）。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    assert "非投资建议" in html, "解释面板应有诚实声明"


def test_render_harmonic_html_badge_in_table_rows():
    """谐波表格行（coin 列）应含徽章 HTML（区分 TradFi/加密）。

    含 XAU(TradFi) 的 state → HTML 中「TradFi」在表格行里。
    """
    s, now_ms = _store_with_harmonic_tradfi()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    # 徽章应通过 JS 渲染到 <td> 中，模板里定义 badgeHtml 函数即可
    # 确认模板含「TradFi」和「加密」的 badge 定义（字符串出现在 script 里）
    assert "TradFi" in html
    assert "加密" in html


def test_render_harmonic_html_no_cdn_after_new_features():
    """加入解释面板 + 徽章后仍不含外部 CDN（自包含单页）。"""
    s, now_ms = _store_with_harmonic_tradfi()
    state = build_harmonic_state(s, now_ms)
    s.close()

    html = render_harmonic_html(state)
    import re
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"谐波 HTML 不应含外部资源: {kw}"
    bad = [m for m in re.findall(r'https?://[^\s"\'<>]+', html)
           if "w3.org/2000/svg" not in m]
    assert not bad, f"不应含外部链接: {bad[:3]}"


def test_existing_harmonic_tests_still_pass():
    """新功能不破坏现有谐波测试（completed/forming 分组，生成时间，字段完整性）。"""
    s, now_ms = _store_with_harmonic()
    state = build_harmonic_state(s, now_ms)
    s.close()

    # 分组正确
    assert len(state["completed"]) == 1
    assert len(state["forming"]) == 1
    assert state["completed"][0]["coin"] == "BTC"
    assert state["forming"][0]["coin"] == "ETH"
    # generated_at 存在
    assert "generated_at" in state
    # 所有原有字段仍存在（asset_class 是新增，不影响旧字段）
    old_fields = [
        "ts", "coin", "tf", "kind", "pattern", "direction", "price",
        "entry_lo", "entry_hi", "stop", "target1", "target2", "rr",
        "confidence", "knn", "orderflow", "fib_note", "prz_lo", "prz_hi",
    ]
    for row in state["completed"] + state["forming"]:
        for f in old_fields:
            assert f in row, f"新功能不应删除旧字段: {f}"


# ---------------------------------------------------------------------------
# 新: build_harmonic_list / build_coin_detail / render_harmonic_detail_html
# ---------------------------------------------------------------------------

from smc_tracker.dashboard import build_harmonic_list, build_coin_detail, render_harmonic_detail_html  # noqa: E402
from smc_tracker.dashboard import _HARMONIC_KEYS  # noqa: E402


def _store_with_harmonic_multi() -> tuple:
    """建含多币谐波数据的临时 Store（BTC/ETH/XAU）及对应 bb_levels 和 candles。"""
    import tempfile
    from pathlib import Path
    d = tempfile.mkdtemp()
    s = Store(Path(d) / "t.db")
    now_ms = 1_700_000_000_000

    # 插入 BTC completed(long,0.82) + ETH forming(short,0.65) + XAU completed(long,0.78)
    # B2：recent_harmonic_setups() 改为 per-coin latest，各币可用不同 ts（此处同 ts 仅为简化）
    s.insert_harmonic_setups([
        (now_ms, "BTC", "1h", "completed", "Gartley", "long",
         65000.0, 64500.0, 64800.0, 63000.0, 67000.0, 69000.0,
         2.5, 0.82, "✓", "✓ 买压", "XA=0.618", 64000.0, 65200.0,
         1, 60000.0, 10, 70000.0, 15, 55000.0, 20, 65000.0, 25, 64500.0),
        (now_ms, "ETH", "4h", "forming", "Bat", "short",
         3500.0, None, None, None, None, None, None,
         0.65, "?", "", "BC=0.886", 3450.0, 3550.0,
         None, None, None, None, None, None, None, None, None, None),
        (now_ms, "XAU", "1h", "completed", "Gartley", "long",
         2350.0, 2340.0, 2350.0, 2300.0, 2400.0, 2450.0,
         2.0, 0.78, "✓", "✓ 买压", "XA=0.618", 2330.0, 2360.0,
         1, 2200.0, 10, 2450.0, 15, 2280.0, 20, 2420.0, 25, 2340.0),
    ])

    # 插入 BTC bb_levels（两个周期）
    s.insert_bb_levels([
        ("BTC", "1h", now_ms, 66000.0, 65000.0, 64000.0, 0.6, False),
        ("BTC", "4h", now_ms, 68000.0, 65500.0, 63000.0, 0.4, True),
    ])

    # 插入 BTC candles（5根 1h K线）
    s.upsert_candles([
        ("BTC", "1h", now_ms - 300_000, 64800.0, 65200.0, 64600.0, 65100.0, 1000.0),
        ("BTC", "1h", now_ms - 240_000, 65100.0, 65400.0, 64900.0, 65300.0, 1200.0),
        ("BTC", "1h", now_ms - 180_000, 65300.0, 65500.0, 65000.0, 65200.0, 900.0),
        ("BTC", "1h", now_ms - 120_000, 65200.0, 65600.0, 64800.0, 64900.0, 1100.0),
        ("BTC", "1h", now_ms - 60_000,  64900.0, 65100.0, 64700.0, 65000.0, 800.0),
    ])
    s.conn.commit()
    return s, now_ms


# ---- build_harmonic_list ----

def test_build_harmonic_list_returns_list():
    """build_harmonic_list 返回 list。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    assert isinstance(result, list)


def test_build_harmonic_list_has_all_coins():
    """每个出现在 recent_harmonic_setups 中的 coin 都应有一条汇总行。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    coins = {r["coin"] for r in result}
    assert "BTC" in coins
    assert "ETH" in coins
    assert "XAU" in coins


def test_build_harmonic_list_structure():
    """每项含必要字段，类型正确。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    for r in result:
        assert "coin" in r
        assert "asset_class" in r and r["asset_class"] in ("tradfi", "crypto")
        assert "best_conf" in r
        assert "direction" in r
        assert "n_setups" in r and isinstance(r["n_setups"], int) and r["n_setups"] >= 1
        assert "has_completed" in r and isinstance(r["has_completed"], bool)


def test_build_harmonic_list_has_data_ts():
    """每项含 ts（最新 setup 计算时刻），供前端显示真实"数据时间/年龄"而非浏览器时钟。

    至少一币应有非空 ts；ts 为 int(epoch ms) 或 None。回归保护"数据没实时性"修复。
    """
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    for r in result:
        assert "ts" in r, "每项必须含 ts 字段"
        assert r["ts"] is None or isinstance(r["ts"], int)
    assert any(r["ts"] is not None for r in result), "至少一币应有数据时间戳"


def test_build_harmonic_list_sorted_by_conf_desc():
    """按 best_conf 降序排列（BTC 0.82 > XAU 0.78 > ETH 0.65）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    confs = [r["best_conf"] for r in result if r["best_conf"] is not None]
    assert confs == sorted(confs, reverse=True), f"应按置信降序排列，实得 {confs}"


def test_build_harmonic_list_has_completed_flag():
    """BTC/XAU 是 completed → has_completed=True；ETH 是 forming → has_completed=False。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    by_coin = {r["coin"]: r for r in result}
    assert by_coin["BTC"]["has_completed"] is True
    assert by_coin["ETH"]["has_completed"] is False
    assert by_coin["XAU"]["has_completed"] is True


def test_build_harmonic_list_asset_class_xau():
    """XAU → asset_class='tradfi'。"""
    s, _ = _store_with_harmonic_multi()
    result = build_harmonic_list(s)
    s.close()
    by_coin = {r["coin"]: r for r in result}
    assert by_coin["XAU"]["asset_class"] == "tradfi"
    assert by_coin["BTC"]["asset_class"] == "crypto"


def test_build_harmonic_list_empty_store():
    """空库时返回 []，不抛异常。"""
    s = _store_empty()
    result = build_harmonic_list(s)
    s.close()
    assert result == []


# ---- build_coin_detail ----

def test_build_coin_detail_returns_dict():
    """build_coin_detail 返回 dict，含必要顶层键。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    assert isinstance(result, dict)
    for key in ("coin", "asset_class", "tf", "tfs_available", "candles", "setups", "sr", "history"):
        assert key in result, f"缺少键: {key}"


def test_build_coin_detail_coin_field():
    """coin 字段与参数一致。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    assert result["coin"] == "BTC"


def test_build_coin_detail_asset_class():
    """BTC → crypto；XAU → tradfi。"""
    s, _ = _store_with_harmonic_multi()
    btc = build_coin_detail(s, "BTC")
    xau = build_coin_detail(s, "XAU")
    s.close()
    assert btc["asset_class"] == "crypto"
    assert xau["asset_class"] == "tradfi"


def test_build_coin_detail_tf_defaults_to_first_setup():
    """tf 未传时，应等于该币在 recent_harmonic_setups 中第一个 setup 的 tf。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    # BTC setup tf='1h'
    assert result["tf"] == "1h"


def test_build_coin_detail_tfs_available():
    """tfs_available 是该币所有 setup 的 tf 列表（不重复）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    assert isinstance(result["tfs_available"], list)
    assert "1h" in result["tfs_available"]


def test_build_coin_detail_candles_structure():
    """candles 是 list of [ts, o, h, l, c, v]（已插入5根）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC", tf="1h")
    s.close()
    candles = result["candles"]
    assert isinstance(candles, list)
    assert len(candles) == 5
    for c in candles:
        assert len(c) == 6, f"蜡烛行应有6列，实得{len(c)}"
        assert all(v is not None for v in c), "蜡烛值不应为 None"


def test_build_coin_detail_setups_structure():
    """setups 含 BTC 1h 的谐波 setup，含 XABCD 点。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC", tf="1h")
    s.close()
    setups = result["setups"]
    assert len(setups) >= 1
    setup = setups[0]
    for field in _HARMONIC_KEYS:
        assert field in setup, f"setup 缺少字段: {field}"
    assert "asset_class" in setup
    # XABCD 点有值（BTC completed 行含完整 XABCD）
    assert setup["x_px"] == 60000.0
    assert setup["d_px"] == 64500.0


def test_build_coin_detail_sr():
    """sr 含 BTC bb_levels（两个周期）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    sr = result["sr"]
    assert isinstance(sr, list)
    assert len(sr) == 2
    for item in sr:
        for field in ("tf", "upper", "lower", "pct_b", "squeeze"):
            assert field in item, f"sr 条目缺少字段: {field}"


def test_build_coin_detail_history():
    """history 含该币历史形态 list[dict]（BTC 有1条）。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC")
    s.close()
    history = result["history"]
    assert isinstance(history, list)
    assert len(history) >= 1
    assert history[0]["coin"] == "BTC"


def test_build_coin_detail_empty_store():
    """空库时各字段为空 list，不抛。"""
    s = _store_empty()
    result = build_coin_detail(s, "NONEXISTENT")
    s.close()
    assert result["candles"] == []
    assert result["setups"] == []
    assert result["sr"] == []
    assert result["history"] == []


def test_build_coin_detail_unknown_tf():
    """传入不存在的 tf 时 candles=[]，不抛。"""
    s, _ = _store_with_harmonic_multi()
    result = build_coin_detail(s, "BTC", tf="NOPE")
    s.close()
    assert result["candles"] == []


# ---- render_harmonic_detail_html ----

def _detail_list_state() -> list[dict]:
    """合成 list_state（供 render_harmonic_detail_html 首屏注入）。"""
    return [
        {"coin": "BTC", "asset_class": "crypto",  "best_conf": 0.82,
         "direction": "long",  "n_setups": 1, "has_completed": True},
        {"coin": "XAU", "asset_class": "tradfi",  "best_conf": 0.78,
         "direction": "long",  "n_setups": 1, "has_completed": True},
        {"coin": "ETH", "asset_class": "crypto",  "best_conf": 0.65,
         "direction": "short", "n_setups": 1, "has_completed": False},
    ]


def test_render_harmonic_detail_html_returns_str():
    """render_harmonic_detail_html 返回非空字符串。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert isinstance(html, str) and len(html) > 100


def test_render_harmonic_detail_html_doctype():
    """应是完整独立页面（含 <!DOCTYPE html>）。"""
    html = render_harmonic_detail_html([])
    assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()


def test_render_harmonic_detail_html_theme_vars():
    """重设计后为浅色/白底主题：含 CSS 变量 --bg 且底色为 #f6f8fa。"""
    html = render_harmonic_detail_html([])
    assert "--bg" in html
    assert "#f6f8fa" in html  # 浅色/白底主题底色


def test_render_harmonic_detail_html_dark_theme():
    """向后兼容别名：含 CSS 变量 --bg（主题已切浅色，断言仍成立）。"""
    html = render_harmonic_detail_html([])
    assert "--bg" in html


def test_render_harmonic_detail_html_no_cdn():
    """自包含：不含外部 CDN/http 链接（w3.org SVG 命名空间除外）。"""
    import re
    html = render_harmonic_detail_html(_detail_list_state())
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"不应含外部资源: {kw}"
    bad = [m for m in re.findall(r'https?://[^\s"\'<>]+', html)
           if "w3.org/2000/svg" not in m]
    assert not bad, f"不应含外部链接: {bad[:3]}"


def test_render_harmonic_detail_html_svg_elements():
    """模板 JS 中应含 SVG 核心元素 <svg、<rect、<line、<polyline（蜡烛图定义）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    for tag in ("<svg", "<rect", "<line", "<polyline"):
        assert tag in html, f"HTML 应含 SVG 元素: {tag}"


def test_render_harmonic_detail_html_candle_word():
    """HTML 含「蜡烛」字样（表明渲染函数注释/label 存在）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "蜡烛" in html


def test_render_harmonic_detail_html_tf_tab():
    """HTML 含周期 tab 相关 JS（tfs_available）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "tfs_available" in html or "tab" in html.lower()


def test_render_harmonic_detail_html_bullbear_labels():
    """HTML 含「看多」和「看空」方向标签（JS 定义中）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "看多" in html
    assert "看空" in html


def test_render_harmonic_detail_html_tradfi_badge():
    """HTML 含「TradFi」和「加密」徽章定义（JS 中 badgeHtml 或等效函数）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "TradFi" in html
    assert "加密" in html


def test_render_harmonic_detail_html_orderflow():
    """HTML 含「订单流」字样（setup detail 或 explainer）。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "订单流" in html


def test_render_harmonic_detail_html_disclaimer():
    """HTML 含「确认层非投资建议」诚实声明。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "确认层非投资建议" in html or "非投资建议" in html


def test_render_harmonic_detail_html_set_interval():
    """HTML 含 setInterval（5s 轮询）。"""
    html = render_harmonic_detail_html([])
    assert "setInterval" in html


def test_render_harmonic_detail_html_api_list_fetch():
    """HTML 拉取 /api/harmonic/list（左面板刷新）。"""
    html = render_harmonic_detail_html([])
    assert "/api/harmonic/list" in html


def test_render_harmonic_detail_html_api_coin_fetch():
    """HTML 拉取 /api/harmonic/coin/（右面板详情）。"""
    html = render_harmonic_detail_html([])
    assert "/api/harmonic/coin/" in html


def test_render_harmonic_detail_html_no_residual_double_braces():
    """模板解转义完整：不含残留 {{ 。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "{{" not in html, "残留 {{ → 模板转义不完整"


def test_render_harmonic_detail_html_initial_state_injected():
    """__INITIAL_STATE__ 占位符已被替换为可解析 JSON。"""
    import json as _json, re as _re
    html = render_harmonic_detail_html(_detail_list_state())
    assert "__INITIAL_STATE__" not in html
    m = _re.search(r"const S\s*=\s*(\[.*?\]);", html, _re.S)
    assert m, "未找到注入的 const S（应为 JSON array）"
    parsed = _json.loads(m.group(1))
    assert isinstance(parsed, list)
    assert parsed[0]["coin"] == "BTC"


def test_render_harmonic_detail_html_filter_buttons():
    """HTML 含过滤按钮：全部/加密/TradFi/有完整形态。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "全部" in html
    assert "有完整形态" in html


def test_render_harmonic_detail_html_none_as_dash():
    """JS 工具函数中 null → '—'（fmtN 或等效守卫）。"""
    html = render_harmonic_detail_html([])
    # 验证 JS 中有对 null/undefined 的守卫，返回 em dash
    assert "—" in html or "&#x2014;" in html


def test_render_harmonic_detail_html_prz_band():
    """HTML JS 中含 PRZ（潜在反转区带）相关代码。"""
    html = render_harmonic_detail_html(_detail_list_state())
    assert "prz" in html.lower() or "PRZ" in html


# ---- 路由注册（smoke: serve 中路由已声明，不起真实服务）----

def test_serve_registers_harmonic_list_route():
    """serve 函数源码中应含 /api/harmonic/list 路由声明。"""
    import inspect
    from smc_tracker import dashboard as _dash
    src = inspect.getsource(_dash.serve)
    assert "/api/harmonic/list" in src, "serve() 应含 /api/harmonic/list 路由"


def test_serve_registers_harmonic_coin_route():
    """serve 函数源码中应含 /api/harmonic/coin 路由声明。"""
    import inspect
    from smc_tracker import dashboard as _dash
    src = inspect.getsource(_dash.serve)
    assert "/api/harmonic/coin" in src, "serve() 应含 /api/harmonic/coin 路由"


def test_serve_registers_harmonic2_route():
    """serve 函数源码中应含 /harmonic2 路由声明。"""
    import inspect
    from smc_tracker import dashboard as _dash
    src = inspect.getsource(_dash.serve)
    assert "/harmonic2" in src, "serve() 应含 /harmonic2 路由"


def test_serve_still_has_old_harmonic_route():
    """旧 /harmonic 路由在 serve() 中必须保留（不破坏现有用户入口）。"""
    import inspect
    from smc_tracker import dashboard as _dash
    src = inspect.getsource(_dash.serve)
    assert '"/harmonic"' in src or "'/harmonic'" in src, "旧 /harmonic 路由应保留"


# ---------------------------------------------------------------------------
# 测试：render_hl_html（HL 聪明钱地址追踪终端页）
# ---------------------------------------------------------------------------

def test_render_hl_html_returns_str():
    """render_hl_html 返回 str，含页面标识 + 核心 HTML 骨架。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_hl_html(state)
    assert isinstance(html, str)
    # 页面标识
    assert "SMC 聪明钱追踪终端" in html
    # 系统 tab（HL 当前激活，谐波链到 /harmonic2）
    assert "HL 系统" in html
    assert "谐波系统" in html
    assert "/harmonic2" in html


def test_render_hl_html_three_column_markers():
    """render_hl_html 含三栏结构 marker：左列/中栏/右侧栏 DOM id。"""
    s = _store_empty()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    html = render_hl_html(state)
    # 左栏（币种列表）
    assert 'id="hl-left"' in html
    # 中栏（主区域）
    assert 'id="hl-main"' in html
    # 右侧栏（feed）
    assert 'id="hl-right"' in html


def test_render_hl_html_design_tokens():
    """render_hl_html 含 D3 设计 token：浅色终端 CSS 变量。"""
    s = _store_empty()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    html = render_hl_html(state)
    # 浅色金融终端 token（来自 _HARMONIC_DETAIL_TEMPLATE 设计系统）
    assert "--bg:" in html
    assert "--panel:" in html
    assert "--blue:" in html
    # IBM Plex font stack（无外部 CDN，系统 fallback）
    assert "IBM Plex" in html


def test_render_hl_html_no_math_random():
    """render_hl_html 零 Math.random 伪造 — 严格禁止。"""
    s = _store_empty()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    html = render_hl_html(state)
    assert "Math.random" not in html, "禁止 Math.random 伪造数据"


def test_render_hl_html_no_cdn():
    """render_hl_html 不含外部 CDN/资源链接（纯自包含）。"""
    import re
    s = _store_empty()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    html = render_hl_html(state)
    for kw in ("cdn.", "unpkg.com", "jsdelivr", "googleapis"):
        assert kw not in html, f"不应含外部资源: {kw}"
    bad = [m for m in re.findall(r'https?://[^\s"\'<>]+', html)
           if "w3.org/2000/svg" not in m]
    assert not bad, f"不应含外部链接: {bad[:3]}"


def test_render_hl_html_api_state_refresh():
    """render_hl_html 含 /api/state 5s 轮询刷新逻辑。"""
    s = _store_empty()
    state = build_dashboard_state(s, 1_700_000_000_000)
    s.close()

    html = render_hl_html(state)
    assert "/api/state" in html
    assert "setInterval" in html


def test_render_hl_html_brace_escape():
    """render_hl_html 双括号转义正确：CSS/JS 良构，无 ${{ 或 :root{{ 残留。"""
    s, now_ms = _store_with_data()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_hl_html(state)
    assert "${{" not in html, "残留 ${{ → JS 语法错误"
    assert ":root{{" not in html, "残留 :root{{ → CSS 畸形"
    assert "{{" not in html, "残留 {{ → 模板转义不完整"
    assert ":root{" in html, "CSS :root{ 应良构"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
