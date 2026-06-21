"""抓庄单测：聪明钱筛选排名(纯函数) + 跟庄信号落库（无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import Config
from smc_tracker.models import Side
from smc_tracker.monitor import EventType, SmartMoneyEvent
from smc_tracker.monitor.whale_discovery import rank_smart_money
from smc_tracker.monitor.whale_momentum import pnl_rows_from
from smc_tracker.storage import Store


def _row(addr, av, alltime, month):
    return {"ethAddress": addr, "accountValue": str(av),
            "windowPerformances": [["day", {"pnl": "0"}], ["week", {"pnl": "0"}],
                                   ["month", {"pnl": str(month)}],
                                   ["allTime", {"pnl": str(alltime)}]]}


def test_rank_filters_and_orders():
    rows = [
        _row("0xA", 1_000_000, 5_000_000, 100_000),    # 合格
        _row("0xB", 2_000_000, 10_000_000, 200_000),   # 合格，PnL 更高 → 第一
        _row("0xC", 100_000, 5_000_000, 100_000),      # 账户太小 → 剔除
        _row("0xD", 1_000_000, 100_000, 100_000),      # 全期 PnL 太小 → 剔除
        _row("0xE", 1_000_000, 5_000_000, -50_000),    # 近月亏损 → 剔除
    ]
    out = rank_smart_money(rows, top_n=10)
    assert [w.address for w in out] == ["0xB", "0xA"]   # 按全期 PnL 降序
    assert "PnL$10M" in out[0].label and out[0].label.startswith("庄#1")


def test_rank_top_n_limit():
    rows = [_row(f"0x{i}", 1_000_000, (10 - i) * 1_000_000, 1) for i in range(8)]
    out = rank_smart_money(rows, top_n=3)
    assert len(out) == 3 and out[0].address == "0x0"


def test_leaderboard_cache_fallback(tmp_path, monkeypatch):
    """排行榜拉取失败时回退到上次缓存（慢/不稳定端点不致整轮 poll 报废，#56）。"""
    import asyncio

    import orjson

    import smc_tracker.monitor.whale_discovery as wd

    cache = tmp_path / "lb.json"
    rows = [{"ethAddress": "0xCACHED", "accountValue": "1", "windowPerformances": []}]
    cache.write_bytes(orjson.dumps(rows))
    monkeypatch.setattr(wd, "_LB_CACHE", cache)

    class _BoomSession:                       # 构造即抛 → 模拟拉取失败/超时
        def __init__(self, *a, **k):
            raise TimeoutError()
    monkeypatch.setattr(wd.aiohttp, "ClientSession", _BoomSession)

    out = asyncio.run(wd.fetch_leaderboard_rows())
    assert out == rows                         # 回退缓存成功


def test_leaderboard_no_cache_reraises(tmp_path, monkeypatch):
    """拉取失败且无缓存可回退 → 向上抛异常（不静默吞错）。"""
    import asyncio

    import smc_tracker.monitor.whale_discovery as wd

    monkeypatch.setattr(wd, "_LB_CACHE", tmp_path / "absent.json")   # 不存在

    class _BoomSession:
        def __init__(self, *a, **k):
            raise TimeoutError()
    monkeypatch.setattr(wd.aiohttp, "ClientSession", _BoomSession)

    try:
        asyncio.run(wd.fetch_leaderboard_rows())
        assert False, "应抛 TimeoutError"
    except TimeoutError:
        pass


def test_pnl_rows_from_parses_and_filters():
    """PnL 纯解析：账户过滤 + 按全期 PnL 降序 + top_n（轮询单次拉取复用，无网络）。"""
    rows = [
        _row("0xA", 1_000_000, 5_000_000, 100_000),
        _row("0xB", 2_000_000, 10_000_000, 200_000),   # 全期最高 → 第一
        _row("0xC", 100_000, 9_000_000, 100_000),      # 账户 < min_account → 剔除
    ]
    out = pnl_rows_from(rows, top_n=10, min_account=300_000.0)
    assert [r[0] for r in out] == ["0xb", "0xa"]       # 地址小写 + 全期降序，0xC 被剔除
    assert out[0][5] == 10_000_000.0 and out[0][6] == 2_000_000.0  # alltime, acct
    assert len(pnl_rows_from(rows, top_n=1, min_account=300_000.0)) == 1  # top_n 生效


def _evt(etype, coin, notional, pos_after, taker=True):
    side = Side.BUY if pos_after >= 0 else Side.SELL   # 建多=买，建空=卖
    return SmartMoneyEvent(
        type=etype, address="0xWHALE", label="庄#1", coin=coin, side=side,
        sz=1.0, px=100.0, notional=notional, position_before=0.0,
        position_after=pos_after, closed_pnl=0.0, time_ms=1000, is_taker=taker)


def _app(store):
    from smc_tracker.app import TradingSystem
    return TradingSystem(Config(), [], store, Path("."))


def test_follow_signal_on_whale_open():
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    app = _app(store)
    # 庄大额建多仓 → 跟庄做多信号落库
    app._on_sm_event(_evt(EventType.OPEN, "BTC", 100_000, pos_after=2.0))
    rows = store.conn.execute(
        "SELECT coin,action,direction,notional FROM whale_signals").fetchall()
    assert len(rows) == 1
    assert rows[0] == ("BTC", "OPEN", "long", 100_000)
    store.close()


def test_no_follow_on_close_or_small():
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    app = _app(store)
    app._on_sm_event(_evt(EventType.CLOSE, "ETH", 100_000, pos_after=0.0))   # 平仓不跟
    app._on_sm_event(_evt(EventType.OPEN, "SOL", 1_000, pos_after=1.0))      # 小额不跟
    assert store.count("whale_signals") == 0
    store.close()


def test_follow_short_direction():
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    app = _app(store)
    app._on_sm_event(_evt(EventType.OPEN, "WIF", 80_000, pos_after=-5.0))    # 建空
    row = store.conn.execute("SELECT direction FROM whale_signals").fetchone()
    assert row[0] == "short"
    store.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
