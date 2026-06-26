"""BitgetOIMonitor 单元测试（合成 ticker，临时库，无网络）。

校验点：
  - ticker data dict 的 OI/资金费/标记价解析正确（字段名同 REST：holdingAmount/markPrice/fundingRate/ts）；
  - oi_usd = oi_size * mark_px；
  - 内存最新 OI 快照与查询接口正确；
  - OI 异动阈值上/下触发（增 ≥surge_pct 触发、减 ≤-surge_pct 触发、小幅不触发）；
  - flush 后 SQLite(bitget_oi) 有数据、latest_oi 正确；
  - attach 为每个 symbol 注册 ticker 订阅。
单测全程用临时 db（tempfile），不碰 data/smc.db。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.bitget_oi_monitor import BitgetOIMonitor
from smc_tracker.storage import Store


class _FakeWS:
    """假 WS：只记录订阅，不联网。"""

    def __init__(self) -> None:
        self.subs: list = []

    def subscribe(self, sub, handler) -> None:
        self.subs.append((sub, handler))


def _store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


def _ticker(symbol, holding, mark, funding, ts):
    """合成一条 WS ticker data dict（字段名与实证一致，值为字符串）。"""
    return {
        "instId": symbol,
        "symbol": symbol,
        "lastPr": str(mark),
        "markPrice": str(mark),
        "indexPrice": str(mark),
        "fundingRate": str(funding),
        "holdingAmount": str(holding),
        "nextFundingTime": "1781956800000",
        "ts": str(ts),
    }


def test_attach_subscribes_all_symbols():
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(
        ["1000BONKUSDT", "PEPEUSDT", "WIFUSDT"],
        {"1000BONKUSDT": "BONK", "PEPEUSDT": "PEPE", "WIFUSDT": "WIF"},
        ws, s,
    )
    m.attach()
    assert len(ws.subs) == 3
    syms = {sub.inst_id for sub, _ in ws.subs}
    assert syms == {"1000BONKUSDT", "PEPEUSDT", "WIFUSDT"}
    assert all(sub.channel == "ticker" for sub, _ in ws.subs)
    s.close()


def test_parse_oi_and_snapshot():
    """解析 OI/funding/mark，oi_usd=oi_size*mark，内存快照正确。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["1000BONKUSDT"], {"1000BONKUSDT": "BONK"}, ws, s)
    # holding=1_000_000, mark=0.005 → oi_usd=5000
    m._on_ticker(
        {"instId": "1000BONKUSDT"},
        [_ticker("1000BONKUSDT", 1_000_000, 0.005, 0.000026, 1781942707406)],
        0,
    )
    assert m.ticks_seen == 1
    # row 顺序 = (symbol, coin, oi_size, oi_usd, mark_px, funding, ts)
    row = m._buffer[0]
    assert row[0] == "1000BONKUSDT"
    assert row[1] == "BONK"
    assert abs(row[2] - 1_000_000.0) < 1e-6      # oi_size = holdingAmount
    assert abs(row[3] - 5000.0) < 1e-6           # oi_usd = oi_size * mark
    assert abs(row[4] - 0.005) < 1e-12           # mark_px
    assert abs(row[5] - 0.000026) < 1e-12        # funding
    assert row[6] == 1781942707406               # ts

    snap = m.latest("1000BONKUSDT")
    assert snap is not None
    assert abs(snap["oi_size"] - 1_000_000.0) < 1e-6
    assert abs(snap["oi_usd"] - 5000.0) < 1e-6
    assert abs(m.latest_oi("1000BONKUSDT") - 1_000_000.0) < 1e-6
    s.close()


def test_surge_up_triggers():
    """OI 上涨 ≥ surge_pct → 触发异动并回调。"""
    ws = _FakeWS()
    s = _store()
    captured: list[dict] = []
    m = BitgetOIMonitor(
        ["PEPEUSDT"], {"PEPEUSDT": "PEPE"}, ws, s,
        surge_pct=0.05, on_surge=lambda e: captured.append(e),
    )
    # 基准 1000，然后涨到 1100（+10% ≥ 5%）
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker("PEPEUSDT", 1000, 1.0, 0.0, 1)], 0)
    assert m.surges_seen == 0                      # 首条无基准，不触发
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker("PEPEUSDT", 1100, 1.0, 0.0, 2)], 0)
    assert m.surges_seen == 1
    assert len(captured) == 1
    e = captured[0]
    assert e["symbol"] == "PEPEUSDT"
    assert abs(e["change"] - 0.10) < 1e-9
    assert abs(e["prev_oi"] - 1000.0) < 1e-9
    assert abs(e["oi_size"] - 1100.0) < 1e-9
    s.close()


def test_surge_down_triggers():
    """OI 下跌 ≤ -surge_pct → 触发异动。"""
    ws = _FakeWS()
    s = _store()
    captured: list[dict] = []
    m = BitgetOIMonitor(
        ["WIFUSDT"], {"WIFUSDT": "WIF"}, ws, s,
        surge_pct=0.05, on_surge=lambda e: captured.append(e),
    )
    m._on_ticker({"instId": "WIFUSDT"}, [_ticker("WIFUSDT", 2000, 1.0, 0.0, 1)], 0)
    # 跌到 1800（-10% ≤ -5%）
    m._on_ticker({"instId": "WIFUSDT"}, [_ticker("WIFUSDT", 1800, 1.0, 0.0, 2)], 0)
    assert m.surges_seen == 1
    assert len(captured) == 1
    assert captured[0]["change"] < 0
    assert abs(captured[0]["change"] - (-0.10)) < 1e-9
    s.close()


def test_small_change_no_surge():
    """OI 小幅变化（< surge_pct）不触发异动。"""
    ws = _FakeWS()
    s = _store()
    captured: list[dict] = []
    m = BitgetOIMonitor(
        ["DOGEUSDT"], {"DOGEUSDT": "DOGE"}, ws, s,
        surge_pct=0.05, on_surge=lambda e: captured.append(e),
    )
    m._on_ticker({"instId": "DOGEUSDT"}, [_ticker("DOGEUSDT", 1000, 1.0, 0.0, 1)], 0)
    # 涨到 1020（+2% < 5%）
    m._on_ticker({"instId": "DOGEUSDT"}, [_ticker("DOGEUSDT", 1020, 1.0, 0.0, 2)], 0)
    assert m.surges_seen == 0
    assert len(captured) == 0
    # 但快照已更新到最新
    assert abs(m.latest_oi("DOGEUSDT") - 1020.0) < 1e-9
    s.close()


def test_zero_oi_skipped():
    """OI=0 的无效 ticker 跳过，不入缓冲、不更新快照。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["PEPEUSDT"], {"PEPEUSDT": "PEPE"}, ws, s)
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker("PEPEUSDT", 0, 1.0, 0.0, 1)], 0)
    assert m.ticks_seen == 0
    assert len(m._buffer) == 0
    assert m.latest("PEPEUSDT") is None
    s.close()


def test_flush_persists():
    """flush 后 SQLite(bitget_oi) 有数据，store.latest_oi 正确。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["PEPEUSDT"], {"PEPEUSDT": "PEPE"}, ws, s)
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker("PEPEUSDT", 1000, 2.0, 0.0001, 100)], 0)
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker("PEPEUSDT", 1100, 2.0, 0.0001, 200)], 0)
    assert len(m._buffer) == 2
    n = m.flush()
    assert n == 2
    assert len(m._buffer) == 0
    assert s.count("bitget_oi") == 2
    # store 最新一条（ts=200）= (symbol,coin,oi_size,oi_usd,mark_px,funding,ts)
    latest = s.latest_oi("PEPEUSDT")
    assert latest is not None
    assert latest[0] == "PEPEUSDT"
    assert latest[1] == "PEPE"
    assert abs(latest[2] - 1100.0) < 1e-6        # oi_size
    assert abs(latest[3] - 2200.0) < 1e-6        # oi_usd = 1100*2
    assert latest[6] == 200                       # ts
    s.close()


def test_maybe_flush_threshold():
    """缓冲累积到阈值后由显式 flush 落库（热路径不再自动 flush，由周期 _periodic_flush 驱动）。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(
        ["PEPEUSDT"], {"PEPEUSDT": "PEPE"}, ws, s, flush_threshold=3,
    )
    # 喂 2 条（不同 ts 避免主键覆盖）→ 未达阈值
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker("PEPEUSDT", 1000, 1.0, 0.0, 1)], 0)
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker("PEPEUSDT", 1001, 1.0, 0.0, 2)], 0)
    assert s.count("bitget_oi") == 0
    assert len(m._buffer) == 2
    # 第 3 条 → 累积到达阈值（3 条在缓冲），热路径不再自动 flush
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker("PEPEUSDT", 1002, 1.0, 0.0, 3)], 0)
    assert len(m._buffer) == 3          # 3 条仍在缓冲
    # 显式 flush 后落库
    n = m.flush()
    assert n == 3
    assert s.count("bitget_oi") == 3
    assert len(m._buffer) == 0
    s.close()


def test_price_change_returns_last_px_and_chg24():
    """喂含 lastPr/change24h 的合成 ticker，断言 price_change 返回正确 (px, chg)。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["1000BONKUSDT"], {"1000BONKUSDT": "BONK"}, ws, s)

    # 构造含 lastPr(最新价) 和 change24h(涨幅比率) 的 ticker
    tk = {
        "instId": "1000BONKUSDT",
        "symbol": "1000BONKUSDT",
        "lastPr": "0.0835",       # 最新成交价
        "markPrice": "0.0836",    # 标记价（lastPr 应优先）
        "indexPrice": "0.0835",
        "fundingRate": "0.000026",
        "holdingAmount": "1000000",
        "change24h": "0.00361",   # 24h 涨幅比率 = +0.361%
        "nextFundingTime": "1781956800000",
        "ts": "1781942707406",
    }
    m._on_ticker({"instId": "1000BONKUSDT"}, [tk], 0)

    result = m.price_change("1000BONKUSDT")
    assert result is not None, "price_change 不应返回 None（已有快照）"
    px, chg = result
    assert abs(px - 0.0835) < 1e-9, f"期望 lastPr=0.0835，实得 {px}"
    assert abs(chg - 0.00361) < 1e-9, f"期望 change24h=0.00361，实得 {chg}"

    # 无快照时应返回 None
    assert m.price_change("UNKNOWN") is None

    # 确认已有键未破坏
    snap = m.latest("1000BONKUSDT")
    assert snap is not None
    assert abs(snap["oi_size"] - 1_000_000.0) < 1e-6
    assert abs(snap["mark_px"] - 0.0836) < 1e-9
    s.close()


def test_price_change_fallback_to_mark_px():
    """lastPr 缺失时回退到 mark_px；chg24 缺失时返回 0.0。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["PEPEUSDT"], {"PEPEUSDT": "PEPE"}, ws, s)

    # 不带 lastPr/change24h 字段的 ticker（只有 markPrice 和 holdingAmount）
    tk = {
        "instId": "PEPEUSDT",
        "symbol": "PEPEUSDT",
        "lastPr": "",             # 空串 → to_float → 0.0 → 回退 mark_px
        "markPrice": "0.00001234",
        "indexPrice": "0.00001234",
        "fundingRate": "0.0001",
        "holdingAmount": "500000",
        "ts": "1781942707000",
    }
    m._on_ticker({"instId": "PEPEUSDT"}, [tk], 0)

    result = m.price_change("PEPEUSDT")
    assert result is not None
    px, chg = result
    assert abs(px - 0.00001234) < 1e-12, f"期望回退到 mark_px，实得 {px}"
    assert chg == 0.0, f"change24h 缺失时期望 0.0，实得 {chg}"
    s.close()


def _ticker_with_chg(symbol: str, holding: float, mark: float, last_pr: float,
                     funding: float, change24h: float, ts: int) -> dict:
    """合成含 lastPr/change24h/fundingRate/holdingAmount 的完整 ticker dict。"""
    return {
        "instId": symbol,
        "symbol": symbol,
        "lastPr": str(last_pr),
        "markPrice": str(mark),
        "indexPrice": str(mark),
        "fundingRate": str(funding),
        "holdingAmount": str(holding),
        "change24h": str(change24h),
        "nextFundingTime": "1781956800000",
        "ts": str(ts),
    }


def test_ticker_returns_correct_fields():
    """ticker(symbol) 应返回 price/chg24/funding/oi_usd 四字段；price 优先取 lastPr。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["1000BONKUSDT"], {"1000BONKUSDT": "BONK"}, ws, s)

    tk = _ticker_with_chg(
        "1000BONKUSDT",
        holding=2_000_000,
        mark=0.0836,
        last_pr=0.0835,
        funding=0.0001,
        change24h=0.00361,
        ts=1781942707406,
    )
    m._on_ticker({"instId": "1000BONKUSDT"}, [tk], 0)

    result = m.ticker("1000BONKUSDT")
    assert result is not None, "有快照时 ticker() 不应返回 None"

    # price 优先取 lastPr=0.0835（而非 markPrice=0.0836）
    assert abs(result["price"] - 0.0835) < 1e-9, f"期望 price=0.0835，实得 {result['price']}"
    # chg24 = 0.00361（+0.361%）
    assert abs(result["chg24"] - 0.00361) < 1e-9, f"期望 chg24=0.00361，实得 {result['chg24']}"
    # funding = 0.0001（0.01%）
    assert abs(result["funding"] - 0.0001) < 1e-9, f"期望 funding=0.0001，实得 {result['funding']}"
    # oi_usd = holding * mark = 2_000_000 * 0.0836 = 167_200
    assert abs(result["oi_usd"] - 167_200.0) < 1.0, f"期望 oi_usd≈167200，实得 {result['oi_usd']}"

    # 未知 symbol 应返回 None
    assert m.ticker("UNKNOWN") is None

    s.close()


def test_ticker_returns_none_when_price_zero():
    """lastPr=0 且 markPrice=0 时 ticker() 应返回 None（无有效价格）。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["PEPEUSDT"], {"PEPEUSDT": "PEPE"}, ws, s)

    # OI 非零但价格都是 0（异常情况）
    # 先注入一个正常 ticker 让 OI>0 通过过滤
    m._ingest("PEPEUSDT", {
        "symbol": "PEPEUSDT",
        "lastPr": "0",
        "markPrice": "0",
        "indexPrice": "0",
        "fundingRate": "0.0001",
        "holdingAmount": "1000",
        "change24h": "0.001",
        "ts": "100",
    })
    # _ingest 会因 oi_size<=0 或 mark_px<=0 而被 parse_oi_row 过滤（holding=1000 但 mark=0 → oi_usd=0）
    # 但若 oi_size > 0（依赖 holdingAmount），它仍可能进入 _latest；
    # 关键是 ticker() 在 price<=0 时返回 None
    # 直接手动塞一个 price=0 的快照（绕过 _ingest 过滤）
    m._latest["PEPEUSDT"] = {"last_px": 0.0, "mark_px": 0.0, "chg24": 0.0,
                              "funding": 0.0001, "oi_size": 1000.0, "oi_usd": 0.0, "ts": 100}
    result = m.ticker("PEPEUSDT")
    assert result is None, f"price=0 时应返回 None，实得 {result}"
    s.close()


def test_board_rows_structure_and_sort():
    """board_rows() 返回结构正确，按 abs(chg24) 降序排列。"""
    ws = _FakeWS()
    s = _store()
    sym_to_coin = {
        "1000BONKUSDT": "BONK",
        "PEPEUSDT": "PEPE",
        "WIFUSDT": "WIF",
    }
    m = BitgetOIMonitor(list(sym_to_coin), sym_to_coin, ws, s)

    # 注入三个 symbol，涨跌幅各不同
    m._on_ticker({"instId": "1000BONKUSDT"}, [_ticker_with_chg(
        "1000BONKUSDT", 1_000_000, 0.0836, 0.0835, 0.0001, 0.00361, 1)], 0)
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker_with_chg(
        "PEPEUSDT", 5_000_000, 0.00001, 0.000011, 0.00005, -0.05123, 2)], 0)
    m._on_ticker({"instId": "WIFUSDT"}, [_ticker_with_chg(
        "WIFUSDT", 200_000, 1.5, 1.48, 0.00008, 0.01200, 3)], 0)

    rows = m.board_rows()
    assert len(rows) == 3, f"应有 3 行，实得 {len(rows)}"

    # 按 abs(chg24) 降序：PEPE(0.051) > WIF(0.012) > BONK(0.004)
    assert rows[0]["coin"] == "PEPE", f"第1行应为 PEPE（最大跌幅），实得 {rows[0]['coin']}"
    assert rows[1]["coin"] == "WIF", f"第2行应为 WIF，实得 {rows[1]['coin']}"
    assert rows[2]["coin"] == "BONK", f"第3行应为 BONK（最小涨幅），实得 {rows[2]['coin']}"

    # 检查每行字段
    for row in rows:
        assert "symbol" in row
        assert "coin" in row
        assert "price" in row and row["price"] > 0
        assert "chg24" in row
        assert "funding" in row
        assert "oi_usd" in row

    # 检查数值正确性（BONK 行）
    bonk = rows[2]
    assert bonk["symbol"] == "1000BONKUSDT"
    assert abs(bonk["price"] - 0.0835) < 1e-9  # 优先 lastPr
    assert abs(bonk["chg24"] - 0.00361) < 1e-9
    assert abs(bonk["funding"] - 0.0001) < 1e-9

    s.close()


def test_board_rows_filters_zero_price():
    """price<=0 的 symbol 被 board_rows() 过滤，不出现在结果中。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["PEPEUSDT"], {"PEPEUSDT": "PEPE"}, ws, s)

    # 直接塞一个 price=0 的快照（绕过 _ingest 过滤器）
    m._latest["PEPEUSDT"] = {
        "last_px": 0.0, "mark_px": 0.0, "chg24": 0.01,
        "funding": 0.0001, "oi_size": 1000.0, "oi_usd": 0.0, "ts": 100,
    }
    rows = m.board_rows()
    assert rows == [], f"price=0 应被过滤，实得 {rows}"

    s.close()


# ================================================================
# 【Bug修复验证】_on_oi_surge 回调签名一致性测试
#
# 修复：app.py._on_oi_surge(self, symbol, prev, cur) → (self, evt: dict)
# 匹配 BitgetOIMonitor.SurgeCallback = Callable[[dict], Any] 协议（单 dict 参数）。
# 旧签名导致每次 OI 异动均抛 TypeError，OI 异动输出静默死亡。
# ================================================================

def test_on_surge_callback_receives_single_dict():
    """验证 on_surge 回调接收单个 dict（非 symbol+prev+cur 三参数）。

    此测试直接断言 Monitor 协议：on_surge=lambda e: ... 收到的是单个 dict，
    包含 symbol / prev_oi / oi_size / change 字段。
    这正是 app.py._on_oi_surge(self, evt: dict) 需要处理的格式。
    """
    ws = _FakeWS()
    s = _store()
    received_evts: list = []

    # 模拟修复后的回调签名：单 dict 参数
    def fixed_handler(evt: dict) -> None:
        received_evts.append(evt)

    m = BitgetOIMonitor(
        ["BTCUSDT"], {"BTCUSDT": "BTC"}, ws, s,
        surge_pct=0.05, on_surge=fixed_handler,
    )

    # 基准 OI
    m._on_ticker({"instId": "BTCUSDT"}, [_ticker("BTCUSDT", 1000, 50000.0, 0.0001, 1)], 0)
    # 涨 10% 触发 surge
    m._on_ticker({"instId": "BTCUSDT"}, [_ticker("BTCUSDT", 1100, 50000.0, 0.0001, 2)], 0)

    assert len(received_evts) == 1, f"应触发 1 次 surge，实际 {len(received_evts)}"
    evt = received_evts[0]

    # 断言 dict 包含所需字段（修复后的 _on_oi_surge 依赖这些字段）
    assert "symbol" in evt, f"evt 应含 symbol 字段: {evt.keys()}"
    assert "prev_oi" in evt, f"evt 应含 prev_oi 字段: {evt.keys()}"
    assert "oi_size" in evt, f"evt 应含 oi_size 字段: {evt.keys()}"
    assert "change" in evt, f"evt 应含 change 字段: {evt.keys()}"

    # 断言字段值正确（供 _on_oi_surge 内部提取）
    assert evt["symbol"] == "BTCUSDT"
    assert abs(evt["prev_oi"] - 1000.0) < 1e-9, f"prev_oi 期望 1000，实际 {evt['prev_oi']}"
    assert abs(evt["oi_size"] - 1100.0) < 1e-9, f"oi_size 期望 1100，实际 {evt['oi_size']}"
    assert abs(evt["change"] - 0.10) < 1e-9, f"change 期望 0.10，实际 {evt['change']}"

    s.close()


def test_on_surge_fixed_handler_extracts_fields_correctly():
    """验证修复后的 _on_oi_surge 逻辑（dict 字段提取）可正确运行。

    模拟 app.py._on_oi_surge(evt) 的逻辑：
      symbol = evt.get('symbol', '')
      prev = evt.get('prev_oi', 0.0)
      cur = evt.get('oi_size', 0.0)
      pct = (cur - prev) / prev * 100 if prev else 0
    断言：对已知 surge evt，pct 计算正确，不抛异常。
    """
    ws = _FakeWS()
    s = _store()
    results: list[tuple] = []

    def fixed_on_oi_surge(evt: dict) -> None:
        """仿 app.py 修复后的签名与逻辑。"""
        symbol = evt.get("symbol", "")
        prev = evt.get("prev_oi", 0.0)
        cur = evt.get("oi_size", 0.0)
        pct = (cur - prev) / prev * 100 if prev else 0
        results.append((symbol, prev, cur, pct))

    m = BitgetOIMonitor(
        ["ETHUSDT"], {"ETHUSDT": "ETH"}, ws, s,
        surge_pct=0.05, on_surge=fixed_on_oi_surge,
    )
    m._on_ticker({"instId": "ETHUSDT"}, [_ticker("ETHUSDT", 2000, 3000.0, 0.0, 10)], 0)
    m._on_ticker({"instId": "ETHUSDT"}, [_ticker("ETHUSDT", 2300, 3000.0, 0.0, 20)], 0)

    assert len(results) == 1
    symbol, prev, cur, pct = results[0]
    assert symbol == "ETHUSDT"
    assert abs(prev - 2000.0) < 1e-9
    assert abs(cur - 2300.0) < 1e-9
    assert abs(pct - 15.0) < 1e-6, f"期望 pct=15%，实际 {pct}"

    s.close()


# ================================================================
# P1 修复验证：_oi_window_data 改用 deque，popleft() O(1)
# ================================================================

def test_oi_window_data_is_deque():
    """P1 修复：_oi_window_data 中每个 symbol 的容器必须是 deque（非 list）。

    deque.popleft() 是 O(1)，list.pop(0) 是 O(n)。
    此测试验证修复后 setdefault 创建的是 deque 实例。
    """
    from collections import deque as _deque
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["PEPEUSDT"], {"PEPEUSDT": "PEPE"}, ws, s)

    # 喂一条 ticker 触发 _oi_window_data 初始化
    m._on_ticker({"instId": "PEPEUSDT"}, [_ticker("PEPEUSDT", 1000, 1.0, 0.0, 1_000_000)], 0)

    window = m._oi_window_data.get("PEPEUSDT")
    assert window is not None, "_oi_window_data 应有 PEPEUSDT 条目"
    assert isinstance(window, _deque), (
        f"P1 修复：_oi_window_data 的容器应为 deque，实际为 {type(window).__name__}"
    )
    s.close()


def test_oi_window_prunes_old_entries_via_popleft():
    """P1 修复：过老的 OI 历史点通过 popleft() 被正确剪裁（保留 retain_ms 内的数据）。

    构造三个 ts：t0（太旧）、t1（刚好在边界外）、t2（最新）。
    喂入后期望只保留最新点（t2），t0/t1 被 popleft() 清除。
    """
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["WIFUSDT"], {"WIFUSDT": "WIF"}, ws, s)

    retain_ms = m._oi_window_retain_ms  # 默认 1_200_000ms = 20min

    # t0 很早（肯定过期），t1 = t2 - retain_ms - 1（刚好过期）
    t2 = 10_000_000
    t1 = t2 - retain_ms - 1    # 过期
    t0 = t1 - 60_000            # 更早，也过期

    # 喂三条，每条单独 on_ticker（不同 ts）
    m._on_ticker({"instId": "WIFUSDT"}, [_ticker("WIFUSDT", 100, 1.0, 0.0, t0)], 0)
    m._on_ticker({"instId": "WIFUSDT"}, [_ticker("WIFUSDT", 200, 1.0, 0.0, t1)], 0)
    m._on_ticker({"instId": "WIFUSDT"}, [_ticker("WIFUSDT", 300, 1.0, 0.0, t2)], 0)

    window = m._oi_window_data.get("WIFUSDT")
    assert window is not None

    # t0 和 t1 均早于 t2 - retain_ms，应被 popleft() 剪掉
    # 只剩 t2 这一条
    remaining_ts = [entry[0] for entry in window]
    assert t0 not in remaining_ts, f"t0={t0} 应被剪裁，剩余: {remaining_ts}"
    assert t1 not in remaining_ts, f"t1={t1} 应被剪裁，剩余: {remaining_ts}"
    assert t2 in remaining_ts, f"t2={t2} 应保留，剩余: {remaining_ts}"

    s.close()


def test_oi_window_query_returns_correct_past_oi():
    """oi_window() 能正确返回 (latest_oi, past_oi)，past_oi 取窗口边界前最近一条。"""
    ws = _FakeWS()
    s = _store()
    m = BitgetOIMonitor(["1000BONKUSDT"], {"1000BONKUSDT": "BONK"}, ws, s)

    now_ms = 2_000_000
    window_ms = 600_000   # 10min 窗口
    boundary = now_ms - window_ms  # = 1_400_000

    # ts=1_000_000：比 boundary 更早，应作为 past_oi
    # ts=1_500_000：比 boundary 更晚，不作为 past_oi
    # ts=2_000_000：最新，作为 latest_oi
    m._on_ticker({"instId": "1000BONKUSDT"}, [_ticker("1000BONKUSDT", 100, 0.01, 0.0, 1_000_000)], 0)
    m._on_ticker({"instId": "1000BONKUSDT"}, [_ticker("1000BONKUSDT", 200, 0.01, 0.0, 1_500_000)], 0)
    m._on_ticker({"instId": "1000BONKUSDT"}, [_ticker("1000BONKUSDT", 300, 0.01, 0.0, 2_000_000)], 0)

    result = m.oi_window("1000BONKUSDT", window_ms, now_ms)
    assert result is not None, "有数据时 oi_window 不应返回 None"
    latest_oi, past_oi = result
    assert abs(latest_oi - 300.0) < 1e-9, f"latest_oi 期望 300，实际 {latest_oi}"
    assert past_oi is not None, "past_oi 应找到 ts=1_000_000 的点"
    assert abs(past_oi - 100.0) < 1e-9, f"past_oi 期望 100，实际 {past_oi}"

    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
