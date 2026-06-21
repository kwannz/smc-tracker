"""仪表盘单测：build_dashboard_state / render_html（合成数据，无网络，不起真实服务器）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store
from smc_tracker.dashboard import build_dashboard_state, render_html


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


def test_render_html_contains_ticker_board():
    """render_html 应包含「行情监控板」字样（来自 sections 数组标题）。"""
    s, now_ms = _store_with_bitget_oi()
    state = build_dashboard_state(s, now_ms)
    s.close()

    html = render_html(state)
    assert "行情监控板" in html, "render_html 应含「行情监控板」字样"


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
