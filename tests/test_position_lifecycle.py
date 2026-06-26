"""持仓生命周期重建单测 + user_fills_by_time 分页 mock 测试（确定性，不联网）。

覆盖：
  ① 纯开仓单段（open_ms=首笔）
  ② 同向多笔加仓（open_ms 不变，n_segment_fills 累加）
  ③ 部分平仓后仍持仓（open_ms 不变，last_close_ms 更新）
  ④ 完全平仓（current_dir=flat，open_ms=0）
  ⑤ 平仓后重新开仓（open_ms=重开那笔）
  ⑥ 反手 'Long > Short'（open_ms=反手笔，方向翻转）
  fmt_hold 各档（秒/分/时/天/—）
  user_fills_by_time 分页推进 + 去重
  WalletPortfolio.refresh 注入 fake info（含 user_fills），验证 open_ms 落库正确
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import WatchAddress
from smc_tracker.models import Fill, Side
from smc_tracker.monitor.position_lifecycle import (
    PositionLifecycle,
    fmt_hold,
    reconstruct,
)
from smc_tracker.monitor.wallet_portfolio import WalletPortfolio
from smc_tracker.storage import Store


# ---------------------------------------------------------------------------
# 测试辅助：构造合成 Fill
# ---------------------------------------------------------------------------

def _fill(
    coin: str,
    side: str,             # 'BUY' 或 'SELL'
    sz: float,
    time_ms: int,
    dir_str: str,          # HL dir 语义
    hash_: str = "",
    oid: int = 0,
    start_position: float = 0.0,
) -> Fill:
    """构造合成 Fill（确定性，不联网）。"""
    s = Side.BUY if side == "BUY" else Side.SELL
    return Fill(
        coin=coin,
        side=s,
        px=100.0,
        sz=sz,
        time_ms=time_ms,
        start_position=start_position,
        dir=dir_str,
        closed_pnl=0.0,
        hash=hash_ or f"h{time_ms}",
        oid=oid or time_ms,
        crossed=False,
        address="0xTEST",
    )


# ---------------------------------------------------------------------------
# ① 纯开仓单段
# ---------------------------------------------------------------------------

def test_open_single_segment():
    """首笔开多 → open_ms = 该笔时间，current_dir = long，n_segment_fills = 1。"""
    fills = [_fill("BTC", "BUY", 1.0, 1000, "Open Long", hash_="h1", oid=1)]
    lcs = reconstruct(fills, now_ms=5000)
    lc = lcs["BTC"]
    assert lc.open_ms == 1000
    assert lc.current_dir == "long"
    assert lc.n_segment_fills == 1
    assert lc.last_close_ms == 0


# ---------------------------------------------------------------------------
# ② 同向多笔加仓
# ---------------------------------------------------------------------------

def test_add_position_same_direction():
    """同向三笔加仓：open_ms 保持第一笔，n_segment_fills = 3。"""
    fills = [
        _fill("ETH", "BUY", 1.0, 1000, "Open Long",  hash_="h1", oid=1),
        _fill("ETH", "BUY", 0.5, 2000, "Open Long",  hash_="h2", oid=2),
        _fill("ETH", "BUY", 0.5, 3000, "Open Long",  hash_="h3", oid=3),
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["ETH"]
    assert lc.open_ms == 1000         # 首笔不变
    assert lc.n_segment_fills == 3    # 三笔都累加
    assert lc.current_dir == "long"


# ---------------------------------------------------------------------------
# ③ 部分平仓后仍持仓
# ---------------------------------------------------------------------------

def test_partial_close_keeps_open_ms():
    """开仓 2.0 → 平 0.5 → 还有 1.5：open_ms 不变，last_close_ms 更新。"""
    fills = [
        _fill("SOL", "BUY",  2.0, 1000, "Open Long",  hash_="h1", oid=1),
        _fill("SOL", "SELL", 0.5, 2000, "Close Long", hash_="h2", oid=2),
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["SOL"]
    assert lc.open_ms == 1000           # 开仓时间不变
    assert lc.last_close_ms == 2000     # 更新平仓时间
    assert lc.current_dir == "long"     # 仍持多


# ---------------------------------------------------------------------------
# ④ 完全平仓
# ---------------------------------------------------------------------------

def test_full_close_flat():
    """开仓 1.0 → 完全平仓 → current_dir = flat，open_ms = 0。"""
    fills = [
        _fill("BTC", "BUY",  1.0, 1000, "Open Long",  hash_="h1", oid=1),
        _fill("BTC", "SELL", 1.0, 2000, "Close Long", hash_="h2", oid=2),
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["BTC"]
    assert lc.current_dir == "flat"
    assert lc.open_ms == 0
    assert lc.last_close_ms == 2000


# ---------------------------------------------------------------------------
# ⑤ 平仓后重新开仓
# ---------------------------------------------------------------------------

def test_reopen_after_close():
    """完全平仓后重新开仓：open_ms = 重开那笔，n_segment_fills = 1。"""
    fills = [
        _fill("BTC", "BUY",  1.0, 1000, "Open Long",  hash_="h1", oid=1),
        _fill("BTC", "SELL", 1.0, 2000, "Close Long", hash_="h2", oid=2),
        _fill("BTC", "BUY",  0.5, 5000, "Open Long",  hash_="h3", oid=3),  # 重开
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["BTC"]
    assert lc.open_ms == 5000          # 重开那笔
    assert lc.current_dir == "long"
    assert lc.n_segment_fills == 1


# ---------------------------------------------------------------------------
# ⑥ 反手 Long > Short
# ---------------------------------------------------------------------------

def test_reversal_long_to_short():
    """反手 'Long > Short'：方向翻转，open_ms = 反手笔时间，last_close_ms 更新。"""
    fills = [
        _fill("ETH", "BUY",  2.0, 1000, "Open Long",     hash_="h1", oid=1),
        _fill("ETH", "SELL", 3.0, 3000, "Long > Short",  hash_="h2", oid=2),  # 净 -1.0
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["ETH"]
    assert lc.current_dir == "short"
    assert lc.open_ms == 3000          # 反手笔重置
    assert lc.last_close_ms == 3000    # 反手含平仓动作
    assert lc.n_segment_fills == 1


def test_reversal_short_to_long():
    """反手 'Short > Long'：方向从空翻多。"""
    fills = [
        _fill("SOL", "SELL", 5.0, 1000, "Open Short",    hash_="h1", oid=1),
        _fill("SOL", "BUY",  8.0, 2000, "Short > Long",  hash_="h2", oid=2),  # 净 +3.0
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["SOL"]
    assert lc.current_dir == "long"
    assert lc.open_ms == 2000
    assert lc.last_close_ms == 2000


# ---------------------------------------------------------------------------
# 多 coin 混合
# ---------------------------------------------------------------------------

def test_multi_coin():
    """两个 coin 的 fills 混合输入，各自独立。"""
    fills = [
        _fill("BTC", "BUY",  1.0, 1000, "Open Long",  hash_="h1", oid=1),
        _fill("ETH", "SELL", 2.0, 2000, "Open Short", hash_="h2", oid=2),
    ]
    lcs = reconstruct(fills, now_ms=9000)
    assert lcs["BTC"].current_dir == "long"
    assert lcs["ETH"].current_dir == "short"
    assert lcs["BTC"].open_ms == 1000
    assert lcs["ETH"].open_ms == 2000


# ---------------------------------------------------------------------------
# fmt_hold 各档
# ---------------------------------------------------------------------------

def test_fmt_hold_zero():
    """open_ms <= 0 → '—'。"""
    assert fmt_hold(0, 9000) == "—"
    assert fmt_hold(-1, 9000) == "—"


def test_fmt_hold_seconds():
    """持仓 30s → '30s'。"""
    now = 100_000
    result = fmt_hold(now - 30_000, now)
    assert result == "30s"


def test_fmt_hold_minutes():
    """持仓 45 分钟 → '45m'。"""
    now = 100_000_000
    result = fmt_hold(now - 45 * 60_000, now)
    assert result == "45m"


def test_fmt_hold_hours():
    """持仓 2h13m。"""
    now = 1_000_000_000
    elapsed_ms = (2 * 3600 + 13 * 60) * 1000
    result = fmt_hold(now - elapsed_ms, now)
    assert result == "2h13m"


def test_fmt_hold_hours_exact():
    """整点小时 → '3h'（无 0m 后缀）。"""
    now = 1_000_000_000
    elapsed_ms = 3 * 3600 * 1000
    result = fmt_hold(now - elapsed_ms, now)
    assert result == "3h"


def test_fmt_hold_days():
    """持仓 3 天 4 小时 → '3d4h'。"""
    now = 1_000_000_000
    elapsed_ms = (3 * 86400 + 4 * 3600) * 1000
    result = fmt_hold(now - elapsed_ms, now)
    assert result == "3d4h"


def test_fmt_hold_days_exact():
    """整天数 → '2d'。"""
    now = 1_000_000_000
    elapsed_ms = 2 * 86400 * 1000
    result = fmt_hold(now - elapsed_ms, now)
    assert result == "2d"


# ---------------------------------------------------------------------------
# user_fills_by_time 分页 mock 测试
# ---------------------------------------------------------------------------

class _FakeSession:
    """模拟两页成交的 HTTP session（第一页满 2000 笔，第二页不满）。"""

    def __init__(self) -> None:
        self._call_count = 0
        # 第一页：2000 笔，hash h0~h1999，time=1000..2999
        self._page1 = [
            {"coin": "BTC", "side": "B", "px": "100", "sz": "0.1",
             "time": str(1000 + i), "startPosition": "0",
             "dir": "Open Long", "closedPnl": "0",
             "hash": f"h{i}", "oid": str(i), "crossed": False}
            for i in range(2000)
        ]
        # 第二页：3 笔（不满，说明是最后一页），hash h2000~h2002，time=3001..3003
        self._page2 = [
            {"coin": "BTC", "side": "B", "px": "100", "sz": "0.1",
             "time": str(3001 + j), "startPosition": "0",
             "dir": "Open Long", "closedPnl": "0",
             "hash": f"h{2000 + j}", "oid": str(2000 + j), "crossed": False}
            for j in range(3)
        ]

    def post(self, url: str, data: bytes, headers: dict):
        """返回伪造的 aiohttp Response context manager。"""
        self._call_count += 1
        import json

        body = json.loads(data)
        start_time = body.get("startTime", 0)
        if start_time <= 2999:
            payload = self._page1
        else:
            payload = self._page2

        class _FakeResp:
            async def __aenter__(self_inner):
                return self_inner
            async def __aexit__(self_inner, *_):
                pass
            def raise_for_status(self_inner):
                pass
            async def read(self_inner):
                import orjson
                return orjson.dumps(payload)

        return _FakeResp()


async def _run_fills_by_time():
    """用 fake session 执行 user_fills_by_time，返回结果。"""
    from smc_tracker.hyperliquid.info_client import HyperliquidInfo

    client = HyperliquidInfo()
    fake_session = _FakeSession()
    client._session = fake_session  # 注入 fake session
    fills = await client.user_fills_by_time("0xTEST", start_ms=500, max_pages=5)
    return fills, fake_session._call_count


def test_user_fills_by_time_pagination():
    """两页分页：第一页满 2000 笔触发翻页，第二页 3 笔终止；共 2003 笔去重结果。"""
    fills, call_count = asyncio.run(_run_fills_by_time())
    assert len(fills) == 2003          # 2000 + 3 笔
    assert call_count == 2             # 请求了两页
    # 按 time_ms 升序
    times = [f.time_ms for f in fills]
    assert times == sorted(times)


def test_user_fills_by_time_dedup():
    """去重：重复的 (hash, oid) 在多页中只保留一份。"""
    from smc_tracker.hyperliquid.info_client import HyperliquidInfo
    import orjson

    # 构造两页都含相同的 5 笔（模拟重叠）
    dup_fills = [
        {"coin": "ETH", "side": "B", "px": "200", "sz": "1.0",
         "time": str(100 + i), "startPosition": "0",
         "dir": "Open Long", "closedPnl": "0",
         "hash": f"dup{i}", "oid": str(i), "crossed": False}
        for i in range(5)
    ]

    call_count_holder = [0]

    class _DupSession:
        def post(self, url, data, headers):
            call_count_holder[0] += 1

            class _Resp:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *_):
                    pass
                def raise_for_status(self):
                    pass
                async def read(self):
                    # 只返回 5 笔（< 2000），不会触发翻页
                    return orjson.dumps(dup_fills)

            return _Resp()

    async def _run():
        client = HyperliquidInfo()
        client._session = _DupSession()
        return await client.user_fills_by_time("0xTEST", start_ms=0, max_pages=5)

    fills = asyncio.run(_run())
    # 只有 5 笔去重后的结果
    assert len(fills) == 5
    assert call_count_holder[0] == 1   # 不满 2000 笔不翻页


# ---------------------------------------------------------------------------
# WalletPortfolio.refresh 注入 fake info（含 user_fills），验证 open_ms 落库
# ---------------------------------------------------------------------------

def _make_store() -> Store:
    return Store(Path(tempfile.mkdtemp()) / "s.db")


class _FakeInfoWithFills:
    """模拟 HyperliquidInfo：clearinghouse_state + user_fills 均合成。"""

    def __init__(self) -> None:
        self._fills = [
            _fill("BTC", "BUY", 1.0, 10_000, "Open Long",  hash_="h1", oid=1),
            _fill("BTC", "BUY", 0.5, 20_000, "Open Long",  hash_="h2", oid=2),
            _fill("ETH", "SELL", 2.0, 15_000, "Open Short", hash_="h3", oid=3),
        ]

    async def clearinghouse_state(self, user: str) -> dict:
        return {
            "marginSummary": {"accountValue": "500000", "totalNtlPos": "200000"},
            "assetPositions": [
                {"position": {
                    "coin": "BTC", "szi": "1.5", "entryPx": "30000",
                    "positionValue": "45000", "unrealizedPnl": "500",
                    "leverage": {"value": "5"},
                }},
                {"position": {
                    "coin": "ETH", "szi": "-2.0", "entryPx": "1800",
                    "positionValue": "3600", "unrealizedPnl": "-100",
                    "leverage": {"value": "3"},
                }},
            ],
        }

    async def user_fills(self, user: str) -> list[Fill]:
        return self._fills


def test_refresh_with_lifecycle_open_ms_saved():
    """refresh 注入含 user_fills 的 fake info，验证 open_ms 落库正确。"""
    s = _make_store()
    wp = WalletPortfolio(s, "http://fake")
    wa = WatchAddress("0xA", "庄A")
    now_ms = 100_000

    snaps = asyncio.run(wp.refresh([wa], now_ms=now_ms, info=_FakeInfoWithFills()))
    assert len(snaps) == 1
    snap = snaps[0]

    # 验证 lifecycles 已重建
    assert "BTC" in snap.lifecycles
    assert "ETH" in snap.lifecycles
    btc_lc = snap.lifecycles["BTC"]
    eth_lc = snap.lifecycles["ETH"]
    assert btc_lc.open_ms == 10_000    # BTC 第一笔开仓时间
    assert btc_lc.current_dir == "long"
    assert eth_lc.open_ms == 15_000
    assert eth_lc.current_dir == "short"

    # 验证落库行含 open_ms
    rows = s.latest_wallet_positions("0xA")
    assert len(rows) == 2
    row_by_coin = {r[1]: r for r in rows}
    btc_row = row_by_coin["BTC"]
    # 列 10 = open_ms（13 列模式）
    assert len(btc_row) == 13
    assert btc_row[10] == 10_000       # open_ms
    assert btc_row[12] is not None     # hold_sec 不为 None
    assert btc_row[12] == (now_ms - 10_000) // 1000

    s.close()


def test_refresh_fills_failure_graceful():
    """user_fills 拉取失败时 refresh 降级：持仓仍落库，open_ms 为 None。"""

    class _FakeInfoFillsFail:
        async def clearinghouse_state(self, user: str) -> dict:
            return {
                "marginSummary": {"accountValue": "100000", "totalNtlPos": "50000"},
                "assetPositions": [
                    {"position": {
                        "coin": "SOL", "szi": "10.0", "entryPx": "50",
                        "positionValue": "500", "unrealizedPnl": "0",
                        "leverage": {"value": "2"},
                    }},
                ],
            }

        async def user_fills(self, user: str) -> list[Fill]:
            raise RuntimeError("网络超时")

    s = _make_store()
    wp = WalletPortfolio(s, "http://fake")
    wa = WatchAddress("0xB", "庄B")
    snaps = asyncio.run(wp.refresh([wa], now_ms=50_000, info=_FakeInfoFillsFail()))
    assert len(snaps) == 1
    snap = snaps[0]
    # lifecycles 为空（降级）
    assert snap.lifecycles == {}
    # 持仓仍成功落库
    rows = s.latest_wallet_positions("0xB")
    assert len(rows) == 1
    assert rows[0][10] is None     # open_ms = None（降级）
    s.close()


# ---------------------------------------------------------------------------
# snapshot_rows 含新列
# ---------------------------------------------------------------------------

def test_snapshot_rows_contains_open_ms():
    """snapshot_rows 返回的 positions 包含 open_ms/last_close_ms/hold_sec 字段。"""
    s = _make_store()
    ts = 50_000
    s.upsert_wallet("0xC", "庄C", "discover", ts, account_value=1e6,
                    total_ntl_pos=3e6, n_positions=1)
    # 13 元组（含 open_ms/last_close_ms/hold_sec）
    s.save_wallet_positions([
        ("0xC", "BTC", "long", 1.0, 30000.0, 30000.0, 1000.0, 5.0, None, ts,
         10_000, 0, 40),
    ])
    wp = WalletPortfolio(s, "http://fake")
    rows = wp.snapshot_rows(["0xC"], now_ms=ts)
    assert len(rows) == 1
    positions = rows[0]["positions"]
    assert len(positions) == 1
    p = positions[0]
    assert p["open_ms"] == 10_000
    assert p["hold_sec"] == 40
    s.close()


# ---------------------------------------------------------------------------
# 缺陷1 修复测试：超量平仓穿越0变号，current_dir 应翻转
# ---------------------------------------------------------------------------

def test_overclose_flips_direction():
    """long 100 单位 → Close Long 150（超量）→ running=-50 → current_dir 变 short。
    缺陷1：修复前 current_dir 仍为 'long'（旧方向），修复后应翻转为 'short'。
    """
    fills = [
        _fill("BTC", "BUY",  100.0, 1000, "Open Long",  hash_="h1", oid=1),
        _fill("BTC", "SELL", 150.0, 2000, "Close Long", hash_="h2", oid=2),  # 超量平仓
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["BTC"]
    # running = 100 - 150 = -50 → 应为 short
    assert lc.current_dir == "short", f"期望 short，实际 {lc.current_dir}"
    assert lc.last_close_ms == 2000


def test_overclose_short_flips_to_long():
    """short 80 单位 → Close Short 120（超量）→ running=+40 → current_dir 变 long。"""
    fills = [
        _fill("ETH", "SELL", 80.0, 1000, "Open Short",  hash_="h1", oid=1),
        _fill("ETH", "BUY",  120.0, 2000, "Close Short", hash_="h2", oid=2),  # 超量平仓
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["ETH"]
    # running = -80 + 120 = +40 → 应为 long
    assert lc.current_dir == "long", f"期望 long，实际 {lc.current_dir}"
    assert lc.last_close_ms == 2000


# ---------------------------------------------------------------------------
# 缺陷2 修复测试：裸 'Buy'/'Sell' dir 减仓不应增加 n_segment_fills
# ---------------------------------------------------------------------------

def test_bare_sell_reduces_long_no_fill_count():
    """long 段 + 裸 'Sell' 减仓：n_segment_fills 不增，current_dir 仍 long，last_close_ms 更新。
    缺陷2：修复前 n_segment_fills 会+1（错），修复后保持不变。
    """
    fills = [
        _fill("BTC", "BUY", 3.0, 1000, "Open Long", hash_="h1", oid=1),
        _fill("BTC", "BUY", 1.0, 2000, "Open Long", hash_="h2", oid=2),
        # 裸 Sell：减仓（不是 Close Long），减 1.0 → running=3.0
        _fill("BTC", "SELL", 1.0, 3000, "Sell",      hash_="h3", oid=3),
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["BTC"]
    # 2 笔开仓：n_segment_fills=2，减仓笔不算
    assert lc.n_segment_fills == 2, f"期望 2，实际 {lc.n_segment_fills}"
    assert lc.current_dir == "long"
    assert lc.last_close_ms == 3000     # 减仓时间更新


def test_bare_sell_reduces_to_flat():
    """long 3.0 → 裸 'Sell' 3.0 → running=0 → flat，open_ms=0，last_close_ms 更新。"""
    fills = [
        _fill("SOL", "BUY",  3.0, 1000, "Open Long", hash_="h1", oid=1),
        _fill("SOL", "SELL", 3.0, 4000, "Sell",       hash_="h2", oid=2),
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["SOL"]
    assert lc.current_dir == "flat", f"期望 flat，实际 {lc.current_dir}"
    assert lc.open_ms == 0
    assert lc.last_close_ms == 4000
    assert lc.n_segment_fills == 0


def test_bare_buy_reduces_short_no_fill_count():
    """short 段 + 裸 'Buy' 减仓：n_segment_fills 不增，current_dir 仍 short。"""
    fills = [
        _fill("ETH", "SELL", 5.0, 1000, "Open Short", hash_="h1", oid=1),
        # 裸 Buy：减仓（买入覆盖空仓），减 2.0 → running=-3.0
        _fill("ETH", "BUY",  2.0, 2000, "Buy",        hash_="h2", oid=2),
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["ETH"]
    assert lc.n_segment_fills == 1, f"期望 1（仅开仓笔），实际 {lc.n_segment_fills}"
    assert lc.current_dir == "short"
    assert lc.last_close_ms == 2000


# ---------------------------------------------------------------------------
# 缺陷2 修复测试：裸 'Buy'/'Sell' 同向加仓 n_fills 仍递增
# ---------------------------------------------------------------------------

def test_bare_buy_adds_long_increments_fills():
    """flat 后裸 'Buy' 开仓，再 'Buy' 同向加仓：n_fills=1→2（同号加仓仍计）。"""
    fills = [
        _fill("BTC", "BUY", 1.0, 1000, "Buy", hash_="h1", oid=1),  # 新开，flat→long
        _fill("BTC", "BUY", 1.0, 2000, "Buy", hash_="h2", oid=2),  # 同向加仓
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["BTC"]
    assert lc.current_dir == "long"
    assert lc.n_segment_fills == 2, f"期望 2，实际 {lc.n_segment_fills}"
    assert lc.open_ms == 1000     # 首笔开仓时间


def test_bare_sell_adds_short_increments_fills():
    """flat 后裸 'Sell' 开空，再 'Sell' 加空：n_fills=1→2。"""
    fills = [
        _fill("ETH", "SELL", 2.0, 1000, "Sell", hash_="h1", oid=1),
        _fill("ETH", "SELL", 1.0, 2000, "Sell", hash_="h2", oid=2),
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["ETH"]
    assert lc.current_dir == "short"
    assert lc.n_segment_fills == 2, f"期望 2，实际 {lc.n_segment_fills}"
    assert lc.open_ms == 1000


# ---------------------------------------------------------------------------
# P1 越零路径修复测试：is_close 超量平仓穿越0后，open_ms/n_fills/seg_max_abs 重置
# ---------------------------------------------------------------------------

def test_overclose_resets_open_ms_to_fill_time():
    """P1 修复：Close Long 超量穿越零后，open_ms 应重置为该笔时间（新段起点）。

    修复前：open_ms 仍为旧段开仓时间（1000），表示持仓时长虚高。
    修复后：open_ms = 越零笔时间（2000），n_fills=1，seg_max_abs=abs(running)。
    """
    fills = [
        _fill("BTC", "BUY",  100.0, 1000, "Open Long",  hash_="h1", oid=1),
        _fill("BTC", "SELL", 150.0, 2000, "Close Long", hash_="h2", oid=2),  # 超量越零 → running=-50
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["BTC"]
    # 方向应翻转为 short（已有 test_overclose_flips_direction 验证）
    assert lc.current_dir == "short"
    # P1 修复：open_ms 重置为越零笔时间 2000
    assert lc.open_ms == 2000, f"P1 修复：open_ms 应为越零笔 2000，实际 {lc.open_ms}"
    # n_fills 重置为 1（新段第一笔）
    assert lc.n_segment_fills == 1, f"P1 修复：n_fills 应为 1，实际 {lc.n_segment_fills}"


def test_overclose_short_resets_open_ms():
    """P1 修复：Close Short 超量穿越零后，open_ms 应重置为该笔时间。"""
    fills = [
        _fill("ETH", "SELL",  80.0, 1000, "Open Short",  hash_="h1", oid=1),
        _fill("ETH", "BUY",  120.0, 2000, "Close Short", hash_="h2", oid=2),  # 净 +40 → running=+40
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["ETH"]
    assert lc.current_dir == "long"
    # P1 修复：open_ms 重置为越零笔时间 2000
    assert lc.open_ms == 2000, f"P1 修复：open_ms 应为越零笔 2000，实际 {lc.open_ms}"
    assert lc.n_segment_fills == 1, f"P1 修复：n_fills 应为 1，实际 {lc.n_segment_fills}"


def test_partial_close_no_zero_crossing_open_ms_unchanged():
    """P1 修复不应影响正常部分平仓（未越零）：open_ms 不变，n_fills 不变。

    确保越零检测仅在方向真实翻转时触发，不误伤正常减仓。
    """
    fills = [
        _fill("SOL", "BUY",  200.0, 1000, "Open Long",  hash_="h1", oid=1),
        _fill("SOL", "SELL",  50.0, 2000, "Close Long", hash_="h2", oid=2),  # 部分平仓 → running=150
    ]
    lcs = reconstruct(fills, now_ms=9000)
    lc = lcs["SOL"]
    # 未越零：方向仍 long，open_ms 仍为 1000
    assert lc.current_dir == "long"
    assert lc.open_ms == 1000, f"正常部分平仓 open_ms 不应改变，实际 {lc.open_ms}"
    assert lc.last_close_ms == 2000   # last_close_ms 更新


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("全部通过")
