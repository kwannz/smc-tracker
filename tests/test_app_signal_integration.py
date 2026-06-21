"""app 集成测试：结构事件 + 聪明钱流向 → 信号落库（离线，无网络）。

验证 TradingSystem 的接线：StructureFeed → _on_structure → 拉 meme 流向 → SignalEngine → SQLite。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.app import TradingSystem
from smc_tracker.config import Config
from smc_tracker.storage import Store


def _ws_candle(coin, t, o, h, l, c):
    return {"s": coin, "i": "1m", "t": t, "T": t + 59999,
            "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": "1", "n": 1}


def test_app_emits_and_persists_signal_on_resonance():
    cfg = Config()
    cfg.smc.swing_lookback = 2
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    app = TradingSystem(cfg, ["kPEPE"], store, Path("."))   # __init__ 不联网
    app.signal_engine.max_stop_pct = 1.0   # 合成 K 线波幅大，放宽止损过滤(本测试只验证接线)

    # 注入聪明钱强净买入（模拟 MemeTradeMonitor 累计的 per-coin 净流向）
    app.meme_monitor._coin_net["kPEPE"] = 300_000

    # 喂入能产生 BOS bull 的合成 K 线（复用 structure 测试构造，逐根不同 t 触发收盘）
    bars = [(12, 10, 11), (13, 11, 12), (11, 8, 9), (14, 10, 13), (16, 12, 15),
            (20, 16, 19), (18, 14, 15), (17, 13, 14), (16, 11, 12), (19, 15, 18),
            (22, 18, 21), (23, 19, 22)]
    for i, (h, l, c) in enumerate(bars):
        app.structure.on_candle_ws(_ws_candle("kPEPE", 1000 + i * 60000, c, h, l, c))
    # 再发一根新 t，确保突破 swing high 的那根（idx10 收21）被作为已收盘喂入
    app.structure.on_candle_ws(_ws_candle("kPEPE", 1000 + 99 * 60000, 22, 24, 20, 21))

    # BOS bull + 净买入 300k → 应产出并落库一条 long 信号
    assert store.count("signals") >= 1
    rows = store.recent_signals("kPEPE")
    assert rows[0][2] == "long"
    assert app.signal_engine.signals_emitted >= 1
    store.close()


def test_app_no_signal_when_flow_disagrees():
    cfg = Config()
    cfg.smc.swing_lookback = 2
    store = Store(Path(tempfile.mkdtemp()) / "s.db")
    app = TradingSystem(cfg, ["kPEPE"], store, Path("."))
    app.meme_monitor._coin_net["kPEPE"] = -300_000   # 结构看多但聪明钱在卖

    bars = [(12, 10, 11), (13, 11, 12), (11, 8, 9), (14, 10, 13), (16, 12, 15),
            (20, 16, 19), (18, 14, 15), (17, 13, 14), (16, 11, 12), (19, 15, 18),
            (22, 18, 21), (23, 19, 22)]
    for i, (h, l, c) in enumerate(bars):
        app.structure.on_candle_ws(_ws_candle("kPEPE", 1000 + i * 60000, c, h, l, c))
    app.structure.on_candle_ws(_ws_candle("kPEPE", 1000 + 99 * 60000, 22, 24, 20, 21))

    assert store.count("signals") == 0   # 无共振 → 不出信号
    store.close()


if __name__ == "__main__":
    test_app_emits_and_persists_signal_on_resonance()
    test_app_no_signal_when_flow_disagrees()
    print("✅ 集成测试通过")
