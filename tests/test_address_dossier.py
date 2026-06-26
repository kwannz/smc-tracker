"""完整地址档案单测：build_dossier 组装(fake info,无网络) + fmt_dossier 渲染。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.models import Fill, Position, Side
from smc_tracker.monitor.address_dossier import build_dossier, fmt_dossier
from smc_tracker.storage import Store

ADDR = "0xabc0000000000000000000000000000000000001"
NOW = 1_700_000_000_000


class _FakeInfo:
    """最小 HyperliquidInfo 桩：满足 AddressAnalyzer.analyze + build_dossier 调用。"""

    def __init__(self, positions=None, fills=None, account=1_000_000.0):
        self._positions = positions or []
        self._fills = fills or []
        self._account = account

    async def clearinghouse_state(self, user):
        return {"marginSummary": {"accountValue": str(self._account),
                                  "totalNtlPos": "500000"}}

    async def positions(self, user):
        return self._positions

    async def user_fills(self, user):
        return self._fills


def test_dossier_assembles_all_sections():
    s = Store(Path(tempfile.mkdtemp()) / "d.db")
    pos = [Position(coin="BTC", szi=-1.5, entry_px=60000.0, position_value=90000.0,
                    unrealized_pnl=1200.0, leverage=5.0, liquidation_px=70000.0),
           Position(coin="ETH", szi=10.0, entry_px=1700.0, position_value=17000.0,
                    unrealized_pnl=-50.0, leverage=3.0, liquidation_px=None)]
    info = _FakeInfo(positions=pos, account=2_000_000.0)
    d = asyncio.run(build_dossier(ADDR, info, s, NOW, window_h=24.0))

    assert d["address"] == ADDR
    assert d["profile"]["account_value"] == 2_000_000.0
    assert d["profile"]["n_positions"] == 2
    # 持仓按 |名义| 降序：BTC(9万) 在前，方向正确
    assert d["positions"][0]["coin"] == "BTC" and d["positions"][0]["side"] == "空"
    assert d["positions"][1]["coin"] == "ETH" and d["positions"][1]["side"] == "多"

    text = fmt_dossier(d)
    assert "地址完整档案" in text
    assert "实时持仓 2 个" in text
    assert "BTC" in text and "ETH" in text
    assert "画像" in text
    s.close()


def test_dossier_empty_address():
    """无持仓/无成交/无协同 → 档案不抛，给出空态友好提示。"""
    s = Store(Path(tempfile.mkdtemp()) / "e.db")
    d = asyncio.run(build_dossier(ADDR, _FakeInfo(), s, NOW))
    assert d["positions"] == []
    text = fmt_dossier(d)
    assert "地址完整档案" in text
    assert "空仓" in text or "无永续持仓" in text
    s.close()


def test_dossier_realtime_fill_detail():
    """实时全币种成交明细：含开/平语义 + 每笔盈亏 + 主被动，按时间倒序。"""
    fills = [
        Fill(coin="BTC", side=Side.SELL, px=60000.0, sz=0.5, time_ms=NOW - 30_000,
             start_position=0.0, dir="Open Short", closed_pnl=0.0, hash="h1",
             oid=1, crossed=True),
        Fill(coin="ETH", side=Side.BUY, px=1700.0, sz=2.0, time_ms=NOW - 10_000,
             start_position=-2.0, dir="Close Short", closed_pnl=125.0, hash="h2",
             oid=2, crossed=False),
    ]
    s = Store(Path(tempfile.mkdtemp()) / "rf.db")
    d = asyncio.run(build_dossier(ADDR, _FakeInfo(fills=fills), s, NOW))
    assert len(d["recent_fills"]) == 2
    assert d["recent_fills"][0]["coin"] == "ETH"        # 最近优先(time desc)
    assert d["recent_fills"][0]["dir"] == "Close Short"
    text = fmt_dossier(d)
    assert "实时成交明细" in text
    assert "Open Short" in text and "Close Short" in text
    assert "平盈亏$+125" in text                          # 平仓盈亏渲染
    s.close()


def test_dossier_flagged_and_trajectory():
    """已标记 + 有成交轨迹 → 档案体现标记与轨迹时间线。"""
    s = Store(Path(tempfile.mkdtemp()) / "f.db")
    s.flag_address(ADDR, NOW, "kPEPE", "净买越阈值", 100000.0)
    # 写一笔该地址作为买方的 meme 成交
    s.insert_hl_meme_trades([
        ("kPEPE", 0.01, 1e7, 100_000.0, "B", ADDR, "0xseller", ADDR, "h1", 1, NOW - 60_000)])
    d = asyncio.run(build_dossier(ADDR, _FakeInfo(), s, NOW))
    assert d["flagged"] is True
    assert len(d["trajectory"]) >= 1
    text = fmt_dossier(d)
    assert "已标记为可疑" in text
    assert "成交轨迹" in text and "kPEPE" in text
    s.close()


def test_dossier_avg_hold_sec_is_real_hold_duration():
    """修审计P1：avg_hold_sec 须是**真实持仓时长**(now-当前持仓段 open_ms)，非「平仓跨度÷笔数」频率代理。

    场景:5h 前开多、至今未平 → 真实持仓 ≈ 5h(18000s)，足以过 whale 持仓阈值。
    """
    five_h_ms = 5 * 3600_000
    fills_open = [
        Fill(coin="BTC", side=Side.BUY, px=60000.0, sz=1.0,
             time_ms=NOW - five_h_ms, start_position=0.0, dir="Open Long",
             closed_pnl=0.0, hash="o1", oid=1, crossed=True),
    ]
    s = Store(Path(tempfile.mkdtemp()) / "hold.db")
    d = asyncio.run(build_dossier(ADDR, _FakeInfo(fills=fills_open), s, NOW))
    assert "avg_hold_sec" in d, "build_dossier 未返回 avg_hold_sec"
    assert abs(d["avg_hold_sec"] - five_h_ms / 1000.0) < 60, (
        f"avg_hold_sec 应≈真实持仓 {five_h_ms / 1000:.0f}s，实际={d['avg_hold_sec']}"
    )
    s.close()


def test_dossier_avg_hold_sec_zero_for_flat_scalper():
    """修审计P1:scalper 开+全平、现已离场(flat)→ avg_hold_sec=0(无可测持仓段，诚实不冒充)。

    旧 bug 用「平仓时间跨度÷笔数」会对这类快进快出地址算出非0频率代理，
    跨过 whale 持仓阈值把游资误判为持仓型庄家。修复后 flat→0→不误判。
    """
    fills_scalp = [
        Fill(coin="BTC", side=Side.BUY, px=60000.0, sz=1.0, time_ms=NOW - 600_000,
             start_position=0.0, dir="Open Long", closed_pnl=0.0, hash="o1", oid=1, crossed=True),
        Fill(coin="BTC", side=Side.SELL, px=60100.0, sz=1.0, time_ms=NOW - 480_000,
             start_position=1.0, dir="Close Long", closed_pnl=50.0, hash="c1", oid=2, crossed=False),
    ]
    s = Store(Path(tempfile.mkdtemp()) / "scalp.db")
    d = asyncio.run(build_dossier(ADDR, _FakeInfo(fills=fills_scalp), s, NOW))
    assert d["avg_hold_sec"] == 0.0, (
        f"flat scalper 无持仓段应=0(不误判whale)，实际={d['avg_hold_sec']}"
    )
    s.close()


def test_dossier_avg_hold_sec_zero_without_fills():
    """P0 边界：无 fills 时 avg_hold_sec 为 0.0，不抛异常。"""
    s = Store(Path(tempfile.mkdtemp()) / "nofill.db")
    d = asyncio.run(build_dossier(ADDR, _FakeInfo(), s, NOW))
    assert "avg_hold_sec" in d
    assert d["avg_hold_sec"] == 0.0
    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
