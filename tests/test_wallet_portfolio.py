"""钱包持仓画像 + 注册表单测（合成数据 + mock，不联网）。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import WatchAddress
from smc_tracker.models import Position
from smc_tracker.monitor.wallet_portfolio import WalletPortfolio, WalletSnapshot, _usd
from smc_tracker.storage import Store


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "s.db")


def _make_position(coin: str, szi: float, entry: float = 100.0,
                   pv: float = 10_000.0, upnl: float = 500.0,
                   lev: float = 5.0, liq: float | None = None) -> Position:
    return Position(
        coin=coin, szi=szi, entry_px=entry,
        position_value=pv, unrealized_pnl=upnl,
        leverage=lev, liquidation_px=liq,
    )


# ---------------------------------------------------------------------------
# Store 方法：upsert_wallet / load_wallets
# ---------------------------------------------------------------------------

def test_upsert_wallet_first_seen_not_overwritten():
    """首见 first_seen_ms 在后续 upsert 中不被覆盖。"""
    s = _store()
    s.upsert_wallet("0xAAA", "庄A", "discover", 1000, account_value=1e6)
    s.upsert_wallet("0xAAA", "庄A", "discover", 2000, account_value=2e6)
    rows = s.load_wallets()
    assert len(rows) == 1
    addr, label, source, first_seen, last_seen, av, tnp, np_ = rows[0]
    assert addr == "0xAAA"
    assert first_seen == 1000          # 首见不变
    assert last_seen == 2000           # 最近更新
    assert abs(av - 2e6) < 1           # account_value 已更新
    s.close()


def test_upsert_wallet_empty_address_guard():
    """空地址 upsert_wallet 直接 return，不入库。"""
    s = _store()
    s.upsert_wallet("", "label", "manual", 1000)
    assert s.load_wallets() == []
    s.close()


def test_load_wallets_order():
    """load_wallets 按 account_value DESC NULLS LAST 排序。"""
    s = _store()
    s.upsert_wallet("0xC", "C", "discover", 1000, account_value=None)
    s.upsert_wallet("0xA", "A", "discover", 1000, account_value=500_000.0)
    s.upsert_wallet("0xB", "B", "discover", 1000, account_value=1_000_000.0)
    rows = s.load_wallets()
    addrs = [r[0] for r in rows]
    assert addrs[0] == "0xB"           # 最大净值第一
    assert addrs[-1] == "0xC"          # NULL 最后
    s.close()


def test_upsert_wallet_label_update():
    """非空 label 会更新；空 label 不覆盖原有 label。"""
    s = _store()
    s.upsert_wallet("0xX", "原标签", "discover", 1000)
    s.upsert_wallet("0xX", "", "discover", 2000)   # 空 label 不覆盖
    rows = s.load_wallets()
    assert rows[0][1] == "原标签"

    s.upsert_wallet("0xX", "新标签", "discover", 3000)  # 非空 label 覆盖
    rows = s.load_wallets()
    assert rows[0][1] == "新标签"
    s.close()


# ---------------------------------------------------------------------------
# Store 方法：save_wallet_positions / latest_wallet_positions
# ---------------------------------------------------------------------------

def _pos_row(addr: str, coin: str, direction: str, szi: float,
             entry: float, pv: float, upnl: float, lev: float,
             liq: float | None, ts: int) -> tuple:
    return (addr, coin, direction, szi, entry, pv, upnl, lev, liq, ts)


def test_save_and_latest_wallet_positions():
    """save_wallet_positions 原子写 + latest_wallet_positions 正确读回最新 ts。"""
    s = _store()
    ts1 = 1000
    rows_t1 = [
        _pos_row("0xA", "ETH", "long",  2.0, 1800.0, 3600.0,  200.0, 10.0, 1500.0, ts1),
        _pos_row("0xA", "BTC", "short", -0.1, 30000.0, 3000.0, -100.0, 5.0, None,  ts1),
    ]
    s.save_wallet_positions(rows_t1)

    # ts2 新快照
    ts2 = 2000
    rows_t2 = [
        _pos_row("0xA", "ETH", "long", 3.0, 1800.0, 5400.0, 400.0, 10.0, 1400.0, ts2),
    ]
    s.save_wallet_positions(rows_t2)

    latest = s.latest_wallet_positions("0xA")
    # 应只返回 ts2 的行
    assert len(latest) == 1
    assert latest[0][9] == ts2         # ts 列
    assert latest[0][1] == "ETH"
    s.close()


def test_latest_wallet_positions_order_by_abs_position_value():
    """latest_wallet_positions 按 abs(position_value) DESC 排序。"""
    s = _store()
    ts = 1000
    rows = [
        _pos_row("0xB", "ETH", "long",  1.0, 1000.0, 500.0,  0.0, 5.0, None, ts),
        _pos_row("0xB", "BTC", "short", -0.5, 2000.0, 3000.0, 0.0, 3.0, None, ts),
        _pos_row("0xB", "SOL", "long",  10.0, 50.0, 1200.0, 0.0, 10.0, None, ts),
    ]
    s.save_wallet_positions(rows)
    latest = s.latest_wallet_positions("0xB")
    pvs = [r[5] for r in latest]
    # 按 abs(position_value) 降序：3000, 1200, 500
    assert pvs == sorted(pvs, key=abs, reverse=True)
    s.close()


def test_save_wallet_positions_empty_noop():
    """空 rows 不写库（不抛异常，不留空事务）。"""
    s = _store()
    s.save_wallet_positions([])   # 应无异常
    assert s.count("wallet_positions_full") == 0
    s.close()


def test_save_wallet_positions_idempotent_replace():
    """INSERT OR REPLACE 允许同 primary key 覆盖写（幂等），不抛 UNIQUE 错误。"""
    s = _store()
    ts = 999
    row = _pos_row("0xZ", "ETH", "long", 1.0, 1000.0, 1000.0, 0.0, 5.0, None, ts)
    # 写两次同 (address, coin, ts) 主键：应覆盖，不报错
    s.save_wallet_positions([row])
    s.save_wallet_positions([row])
    # 仍只有 1 行（INSERT OR REPLACE 覆盖了原行）
    assert s.count("wallet_positions_full") == 1
    s.close()


# ---------------------------------------------------------------------------
# direction 边界测试
# ---------------------------------------------------------------------------

def test_direction_from_szi():
    """szi>0 → long；szi<0 → short（WalletPortfolio.refresh 的核心逻辑）。"""
    pos_long = _make_position("ETH", szi=2.0)
    pos_short = _make_position("BTC", szi=-0.5)
    assert pos_long.szi > 0
    assert pos_short.szi < 0
    assert pos_long.is_long
    assert not pos_short.is_long


# ---------------------------------------------------------------------------
# WalletPortfolio.refresh（注入 mock info，不联网）
# ---------------------------------------------------------------------------

class _FakeInfo:
    """模拟 HyperliquidInfo，返回合成 clearinghouseState。"""

    def __init__(self, positions_data: list[dict], account_value: float = 1_000_000.0,
                 total_ntl_pos: float = 500_000.0) -> None:
        self._positions_data = positions_data
        self._account_value = account_value
        self._total_ntl_pos = total_ntl_pos

    async def clearinghouse_state(self, user: str) -> dict:
        asset_positions = []
        for p in self._positions_data:
            asset_positions.append({"position": p})
        return {
            "marginSummary": {
                "accountValue": str(self._account_value),
                "totalNtlPos": str(self._total_ntl_pos),
            },
            "assetPositions": asset_positions,
        }


def _make_pos_dict(coin: str, szi: str, entry: str = "100.0",
                   pv: str = "10000.0", upnl: str = "500.0",
                   lev: int = 5, liq: str | None = None) -> dict:
    d: dict = {
        "coin": coin, "szi": szi, "entryPx": entry,
        "positionValue": pv, "unrealizedPnl": upnl,
        "leverage": {"value": str(lev)},
    }
    if liq is not None:
        d["liquidationPx"] = liq
    return d


def test_refresh_saves_positions_to_db():
    """refresh 落库行数正确（每个非零持仓 1 行）。"""
    s = _store()
    wp = WalletPortfolio(s, "http://fake")
    wa = WatchAddress("0xA", "庄A")
    fake = _FakeInfo([
        _make_pos_dict("ETH", "2.0"),
        _make_pos_dict("BTC", "-0.1"),
        _make_pos_dict("SOL", "0"),      # szi=0 → 过滤掉
    ])
    snaps = asyncio.run(wp.refresh([wa], now_ms=1000, info=fake))
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.address == "0xA"
    assert len(snap.positions) == 2      # SOL szi=0 被过滤
    assert s.count("wallet_positions_full") == 2
    s.close()


def test_refresh_direction_from_szi_sign():
    """szi>0→long，szi<0→short 在落库行中正确体现。"""
    s = _store()
    wp = WalletPortfolio(s, "http://fake")
    wa = WatchAddress("0xB", "庄B")
    fake = _FakeInfo([
        _make_pos_dict("ETH", "3.0"),
        _make_pos_dict("BTC", "-0.5"),
    ])
    asyncio.run(wp.refresh([wa], now_ms=2000, info=fake))
    rows = s.latest_wallet_positions("0xB")
    directions = {r[1]: r[2] for r in rows}   # coin -> direction
    assert directions["ETH"] == "long"
    assert directions["BTC"] == "short"
    s.close()


def test_refresh_account_value_persisted():
    """account_value 和 total_ntl_pos 正确落入 watched_wallets。"""
    s = _store()
    wp = WalletPortfolio(s, "http://fake")
    wa = WatchAddress("0xC", "庄C")
    fake = _FakeInfo(
        [_make_pos_dict("ETH", "1.0")],
        account_value=32_900_000.0,
        total_ntl_pos=101_000_000.0,
    )
    asyncio.run(wp.refresh([wa], now_ms=3000, info=fake))
    wallets = s.load_wallets()
    assert len(wallets) == 1
    assert abs(wallets[0][5] - 32_900_000.0) < 1    # account_value
    assert abs(wallets[0][6] - 101_000_000.0) < 1   # total_ntl_pos
    s.close()


def test_refresh_single_address_failure_skipped():
    """单地址拉取失败不影响其他地址（log.warning + 跳过）。"""
    s = _store()
    wp = WalletPortfolio(s, "http://fake")

    class _BoomInfo:
        async def clearinghouse_state(self, user: str) -> dict:
            if user == "0xBAD":
                raise RuntimeError("网络超时")
            return {
                "marginSummary": {"accountValue": "1000000", "totalNtlPos": "500000"},
                "assetPositions": [{"position": _make_pos_dict("ETH", "1.0")}],
            }

    wallets = [WatchAddress("0xBAD", "坏"), WatchAddress("0xGOOD", "好")]
    snaps = asyncio.run(wp.refresh(wallets, now_ms=4000, info=_BoomInfo()))
    # 只有 0xGOOD 成功
    assert len(snaps) == 1
    assert snaps[0].address == "0xGOOD"
    s.close()


# ---------------------------------------------------------------------------
# WalletPortfolio.fmt
# ---------------------------------------------------------------------------

def test_fmt_contains_key_info():
    """fmt 输出含币种/方向符号/关键数字。"""
    s = _store()
    wp = WalletPortfolio(s, "http://fake")
    snap = WalletSnapshot(
        address="0xecb6000000000000000000000000000000002b00",
        label="庄#1",
        account_value=32_900_000.0,
        total_ntl_pos=101_000_000.0,
        positions=[
            _make_position("ETH", szi=-2.0, entry=1713.0,
                           pv=32_800_000.0, upnl=-408_847.0,
                           lev=15.0, liq=3240.0),
        ],
        ts=1000,
    )
    out = wp.fmt(snap, top=12)
    assert "庄#1" in out
    assert "ETH" in out
    assert "空🔴" in out          # 空仓方向
    assert "15x" in out
    s.close()


def test_fmt_long_position():
    """多仓显示多🟢。"""
    s = _store()
    wp = WalletPortfolio(s, "http://fake")
    snap = WalletSnapshot(
        address="0x1234",
        label="庄X",
        account_value=1e6,
        total_ntl_pos=2e6,
        positions=[_make_position("BTC", szi=0.5, pv=15000.0)],
        ts=1000,
    )
    out = wp.fmt(snap)
    assert "多🟢" in out
    s.close()


def test_fmt_none_liquidation_price():
    """爆仓价为 None 时显示 —（破折号）。"""
    s = _store()
    wp = WalletPortfolio(s, "http://fake")
    snap = WalletSnapshot(
        address="0xABCD",
        label="庄Y",
        account_value=5e5,
        total_ntl_pos=1e6,
        positions=[_make_position("SOL", szi=10.0, liq=None)],
        ts=1000,
    )
    out = wp.fmt(snap)
    assert "—" in out   # 爆仓价缺失显示破折号
    s.close()


def test_fmt_top_truncates():
    """持仓多于 top 时显示省略提示。"""
    s = _store()
    wp = WalletPortfolio(s, "http://fake")
    positions = [_make_position(f"C{i}", szi=1.0, pv=float(i * 1000)) for i in range(1, 6)]
    snap = WalletSnapshot(
        address="0xZ",
        label="庄Z",
        account_value=1e6,
        total_ntl_pos=5e6,
        positions=positions,
        ts=1000,
    )
    out = wp.fmt(snap, top=3)
    assert "省略" in out   # 有省略提示
    s.close()


# ---------------------------------------------------------------------------
# WalletPortfolio.snapshot_rows
# ---------------------------------------------------------------------------

def test_snapshot_rows_structure():
    """snapshot_rows 返回正确的结构体。"""
    s = _store()
    ts = 5000
    s.upsert_wallet("0xD", "庄D", "discover", ts, account_value=2e6, total_ntl_pos=5e6, n_positions=2)
    s.save_wallet_positions([
        _pos_row("0xD", "ETH", "long",  1.0, 1800.0, 1800.0, 100.0, 10.0, 1500.0, ts),
        _pos_row("0xD", "BTC", "short", -0.1, 30000.0, 3000.0, -50.0, 5.0, None, ts),
    ])
    wp = WalletPortfolio(s, "http://fake")
    rows = wp.snapshot_rows(["0xD"], now_ms=ts)
    assert len(rows) == 1
    row = rows[0]
    assert row["address"] == "0xD"
    assert row["label"] == "庄D"
    assert abs(row["account_value"] - 2e6) < 1
    assert len(row["positions"]) == 2
    coins = {p["coin"] for p in row["positions"]}
    assert "ETH" in coins and "BTC" in coins
    # 每个持仓字典含必需字段
    for p in row["positions"]:
        assert "coin" in p and "direction" in p and "position_value" in p
    s.close()


def test_snapshot_rows_unknown_address():
    """未知地址不抛异常，返回空 positions。"""
    s = _store()
    wp = WalletPortfolio(s, "http://fake")
    rows = wp.snapshot_rows(["0xUNKNOWN"], now_ms=1000)
    assert len(rows) == 1
    assert rows[0]["positions"] == []
    s.close()


# ---------------------------------------------------------------------------
# _usd 格式化函数
# ---------------------------------------------------------------------------

def test_usd_formatter():
    assert _usd(None) == "—"
    assert _usd(1_500_000.0) == "$1.50M"
    assert _usd(3_200.0) == "$3.2K"
    assert _usd(-500.0) == "-$500.00"
    assert _usd(2_000_000_000.0) == "$2.00B"


def test_wallet_snapshot_is_empty_filters_noise():
    """空画像(无持仓且净值可忽略) → is_empty=True，周期推送应跳过（用户#：净值$0/0持仓/无币种方向 是噪声）。"""
    # 真实病例：可疑庄 净值$0.00 总名义$0.00 持仓0个
    empty = WalletSnapshot("0x20cf6f", "可疑庄", 0.0, 0.0, [], 1_700_000_000_000)
    assert empty.is_empty is True
    # 有持仓 → 非空（有币种/方向可追踪）
    nonempty = WalletSnapshot("0xAAA", "庄A", 1_000_000.0, 500_000.0,
                              [object()], 1_700_000_000_000)
    assert nonempty.is_empty is False
    # 0 持仓但有可观净值（离场持币，仍是信息）→ 不算空，保留推送
    cash = WalletSnapshot("0xBBB", "庄B", 5_000_000.0, 0.0, [], 1_700_000_000_000)
    assert cash.is_empty is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("全部通过")
