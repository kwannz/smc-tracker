"""MonitoredCoinsCfg + resolve_monitored_universe 单测（合成数据，纯函数）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import (
    MonitoredCoinsCfg,
    resolve_monitored_universe,
    Config,
)


def test_cfg_defaults():
    c = MonitoredCoinsCfg()
    assert c.enabled is False
    assert c.timeframes == ["15m", "1H", "4H", "6H", "12H", "1D", "1W"]
    assert c.collect_interval_sec == 300.0


def test_resolve_orders_by_volume():
    """清单 {BTC, ETH}；ETH 成交额更高 → 排前。symbol 用清单存的。"""
    monitored = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
    base_map = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
    tickers = {"BTCUSDT": {"quoteVolume": "500"}, "ETHUSDT": {"quoteVolume": "900"}}
    out = resolve_monitored_universe(monitored, base_map, tickers)
    assert list(out.keys()) == ["ETH", "BTC"]
    assert out["BTC"] == "BTCUSDT"


def test_resolve_symbol_fallback():
    """清单 symbol 缺失 → 回退 base_map 反查；再缺 → coin+'USDT'。"""
    monitored = {"SOL": "", "XYZ": ""}
    base_map = {"SOLUSDT": "SOL"}  # XYZ 不在 base_map
    tickers = {}
    out = resolve_monitored_universe(monitored, base_map, tickers)
    assert out["SOL"] == "SOLUSDT"   # base_map 反查
    assert out["XYZ"] == "XYZUSDT"   # 兜底拼接


def test_resolve_empty():
    assert resolve_monitored_universe({}, {}, {}) == {}


def test_config_load_defaults(tmp_path: Path):
    """config.yaml 无 monitored_coins 段 → 默认 enabled=False。"""
    p = tmp_path / "c.yaml"
    p.write_text("markets: [BTC]\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.monitored_coins.enabled is False


def test_config_load_filters_invalid_tf(tmp_path: Path):
    """非法周期 8h 被剔除（Bitget 不支持）；合法保留。"""
    p = tmp_path / "c.yaml"
    p.write_text(
        "monitored_coins:\n"
        "  enabled: true\n"
        "  timeframes: ['15m', '8h', '1D']\n",
        encoding="utf-8",
    )
    cfg = Config.load(p)
    assert cfg.monitored_coins.enabled is True
    assert "8h" not in cfg.monitored_coins.timeframes
    assert cfg.monitored_coins.timeframes == ["15m", "1D"]
