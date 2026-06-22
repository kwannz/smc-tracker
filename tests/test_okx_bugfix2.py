"""第二轮 bug 修复回归测试（确定性，不联网）：

bug1: OKX net(单向)持仓模式强平 posSide=='net' 须由 side 推导被平方向(原 if/elif 漏)。
bug2: 常驻背离须用窗口净流向(增量)，而非 monitor._net_flow 的自启动累积值(陈旧)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _WS:
    def subscribe(self, *a: object) -> None:
        pass


# ---- bug1: net 模式强平方向推导 ----

def test_liquidation_net_mode_sell_is_long_liquidated():
    """posSide='net' + side='sell' → 多头被强平, long_liq_usd 累计 + 级联触发。"""
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor
    fired: list = []
    m = OKXPerpMonitor(["BTC-USDT-SWAP"], {"BTC-USDT-SWAP": "BTC"},
                       {"BTC-USDT-SWAP": 1.0}, _WS(), store=None,
                       liq_signal_usd=100_000.0, on_liquidation_signal=fired.append)
    # net 模式: posSide='net', side='sell'(多头被强平), 名义=2*1.0*60000=12万 → 跨 10万 触发
    m._on_liquidation({}, [{"instId": "BTC-USDT-SWAP", "details": [
        {"posSide": "net", "side": "sell", "sz": "2", "bkPx": "60000", "ts": "1"}]}], 0)
    assert m._liq["BTC"]["long_liq_usd"] == 120_000.0     # 原代码会漏(net 不命中 if/elif)
    assert len(fired) == 1 and fired[0]["liquidated_side"] == "long"


def test_liquidation_net_mode_buy_is_short_liquidated():
    """posSide='net' + side='buy' → 空头被强平, short_liq_usd 累计。"""
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor
    m = OKXPerpMonitor(["BTC-USDT-SWAP"], {"BTC-USDT-SWAP": "BTC"},
                       {"BTC-USDT-SWAP": 1.0}, _WS(), store=None)
    m._on_liquidation({}, [{"instId": "BTC-USDT-SWAP", "details": [
        {"posSide": "net", "side": "buy", "sz": "1", "bkPx": "60000", "ts": "1"}]}], 0)
    assert m._liq["BTC"]["short_liq_usd"] == 60_000.0


def test_liquidation_explicit_long_short_unchanged():
    """posSide='long'/'short' 显式时仍直接用(向后兼容)。"""
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor
    m = OKXPerpMonitor(["BTC-USDT-SWAP"], {"BTC-USDT-SWAP": "BTC"},
                       {"BTC-USDT-SWAP": 1.0}, _WS(), store=None)
    m._on_liquidation({}, [{"instId": "BTC-USDT-SWAP", "details": [
        {"posSide": "long", "side": "sell", "sz": "1", "bkPx": "60000", "ts": "1"}]}], 0)
    assert m._liq["BTC"]["long_liq_usd"] == 60_000.0


# ---- bug2: 窗口净流向(增量, 非 lifetime) ----

def test_windowed_net_flow_is_delta_not_lifetime():
    """窗口净流向 = 当前累积 − 上窗累积, 而非 lifetime 巨值。"""
    from smc_tracker.okx.stream import windowed_net_flow
    # BTC lifetime 累积到 5e6, 上窗 4.9e6 → 本窗仅 +1e5(不会因 lifetime 巨大误判背离)
    win = windowed_net_flow({"BTC": 5_000_000.0, "ETH": -200_000.0}, {"BTC": 4_900_000.0})
    assert abs(win["BTC"] - 100_000.0) < 1e-6      # 本窗增量, 非 5e6
    assert abs(win["ETH"] - (-200_000.0)) < 1e-6   # ETH 首现 → 全量


def test_windowed_net_flow_empty_prev():
    """首窗 prev 为空 → 等于当前累积。"""
    from smc_tracker.okx.stream import windowed_net_flow
    assert windowed_net_flow({"BTC": 300.0}, {}) == {"BTC": 300.0}
