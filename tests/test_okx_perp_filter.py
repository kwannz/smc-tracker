"""OKXPerpMonitor 共享 trades 频道隔离回归：

run_okx_streaming 在同一 OKXWSClient 上增订了现货 <COIN>-USDT 的 trades 喂 SpotTakerCollector，
而 OKXWSClient 按 channel 分发——现货 trades 也会到达永续 monitor 的 _on_trades。
monitor 必须只处理自己监控的 inst，否则产生假 net_flow 键(如 "BTC-USDT", ctVal 错为 1.0)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _WS:
    def subscribe(self, *a: object) -> None:
        pass


def test_on_trades_ignores_non_monitored_inst():
    """现货 BTC-USDT 成交不得污染永续 monitor 的 net_flow(只认监控的 -SWAP inst)。"""
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor
    m = OKXPerpMonitor(["BTC-USDT-SWAP"], {"BTC-USDT-SWAP": "BTC"},
                       {"BTC-USDT-SWAP": 0.01}, _WS(), store=None)
    # 现货 BTC-USDT 成交（共享 trades 频道会到达此 handler）→ 应被忽略
    m._on_trades({"instId": "BTC-USDT"}, [{"side": "buy", "sz": "10", "px": "60000"}], 0)
    assert "BTC-USDT" not in m.all_net_flows(), "现货 inst 污染了永续 net_flow"
    assert m.all_net_flows() == {}

    # 监控的永续 inst 正常累计（名义 = sz × ctVal × px）
    m._on_trades({"instId": "BTC-USDT-SWAP"}, [{"side": "buy", "sz": "100", "px": "60000"}], 0)
    assert abs(m.net_flow("BTC") - 100 * 0.01 * 60000) < 1e-6
