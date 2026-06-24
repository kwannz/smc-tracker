"""谐波监控器 universe_mode 单测（TDD A2）。

覆盖场景：
1. HarmonicCfg 新增字段 universe_mode 默认值/合法值
2. top_n 模式行为不变（回归）
3. all_perp 模式：从合成 contracts/tickers 构建出按 vol 排序的全币 universe
4. all_perp 模式：contracts 为空时优雅降级（返回空映射）
5. all_perp 模式：部分 ticker 缺少成交额时容错处理

所有测试均为合成数据，不打真实网络。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import HarmonicCfg, UniverseCfg, resolve_universe


# ---------------------------------------------------------------------------
# 辅助数据构造
# ---------------------------------------------------------------------------

def _make_base_map(coins: list[str]) -> dict[str, str]:
    """生成合成 base_map: {symbol → baseCoin}（模拟 BitgetREST.perp_base_coins() 输出）。"""
    return {f"{coin}USDT": coin for coin in coins}


def _make_tickers(coins: list[str], volumes: list[float]) -> dict[str, dict]:
    """生成合成 tickers dict，quoteVolume 按指定值设置（模拟 BitgetREST.tickers() 输出）。"""
    return {
        f"{coin}USDT": {"quoteVolume": str(vol), "baseCoin": coin}
        for coin, vol in zip(coins, volumes)
    }


# ---------------------------------------------------------------------------
# HarmonicCfg.universe_mode 字段测试
# ---------------------------------------------------------------------------

class TestHarmonicCfgUniverseMode:
    """HarmonicCfg 新增 universe_mode 字段的属性测试。"""

    def test_default_universe_mode_is_top_n(self):
        """universe_mode 默认值为 'top_n'（向后兼容，不改变现有行为）。"""
        cfg = HarmonicCfg()
        assert cfg.universe_mode == "top_n"

    def test_universe_mode_can_be_set_to_all_perp(self):
        """universe_mode 可设置为 'all_perp'。"""
        cfg = HarmonicCfg(universe_mode="all_perp")
        assert cfg.universe_mode == "all_perp"

    def test_universe_mode_is_string(self):
        """universe_mode 字段类型为字符串。"""
        cfg = HarmonicCfg()
        assert isinstance(cfg.universe_mode, str)

    def test_other_harmonic_cfg_defaults_unchanged(self):
        """新增 universe_mode 字段不影响其他字段的默认值（向后兼容性回归）。"""
        cfg = HarmonicCfg()
        assert cfg.enabled is True
        assert cfg.top_n == 12
        assert cfg.bars == 2500
        assert cfg.order == 3
        assert cfg.tol == 0.05
        assert cfg.account_usd == 10_000.0
        assert cfg.risk_pct == 0.01
        assert cfg.target_rr == 2.0


# ---------------------------------------------------------------------------
# top_n 模式回归测试（行为完全不变）
# ---------------------------------------------------------------------------

class TestTopNModeRegression:
    """top_n 模式：行为与 universe_mode 引入前完全一致（回归）。"""

    _COINS = ["BTC", "ETH", "SOL", "DOGE", "AVAX"]
    _VOLS = [500.0, 800.0, 300.0, 150.0, 100.0]

    @property
    def _base_map(self):
        return _make_base_map(self._COINS)

    @property
    def _tickers(self):
        return _make_tickers(self._COINS, self._VOLS)

    def test_top_n_returns_n_coins(self):
        """top_n 模式：返回恰好 top_n 个币。"""
        result = resolve_universe(
            self._base_map,
            self._tickers,
            UniverseCfg(mode="top_n", top_n=3),
        )
        assert len(result) == 3

    def test_top_n_returns_highest_vol_coins(self):
        """top_n 模式：返回成交额最高的 top_n 个币（按 vol 降序）。"""
        result = resolve_universe(
            self._base_map,
            self._tickers,
            UniverseCfg(mode="top_n", top_n=3),
        )
        # ETH(800) > BTC(500) > SOL(300) → 前三
        assert set(result.keys()) == {"ETH", "BTC", "SOL"}

    def test_top_n_order_is_descending_by_vol(self):
        """top_n 模式：按 24h 成交额降序排列（高 vol 在前）。"""
        result = resolve_universe(
            self._base_map,
            self._tickers,
            UniverseCfg(mode="top_n", top_n=3),
        )
        coins_ordered = list(result.keys())
        # ETH(800) > BTC(500) > SOL(300)
        assert coins_ordered == ["ETH", "BTC", "SOL"]

    def test_top_n_maps_coin_to_symbol_correctly(self):
        """top_n 模式：返回正确的 {coin: symbol} 映射。"""
        result = resolve_universe(
            self._base_map,
            self._tickers,
            UniverseCfg(mode="top_n", top_n=2),
        )
        assert result["ETH"] == "ETHUSDT"
        assert result["BTC"] == "BTCUSDT"


# ---------------------------------------------------------------------------
# all_perp 模式测试
# ---------------------------------------------------------------------------

class TestAllPerpMode:
    """all_perp 模式：从全部 USDT 永续合约按 vol 排序构建 universe。"""

    _COINS = ["BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "MATIC"]
    _VOLS = [500.0, 800.0, 300.0, 150.0, 100.0, 80.0, 60.0]

    @property
    def _base_map(self):
        return _make_base_map(self._COINS)

    @property
    def _tickers(self):
        return _make_tickers(self._COINS, self._VOLS)

    def test_all_perp_returns_all_coins(self):
        """all_perp 模式：返回全部合法永续合约币（不受 top_n 限制）。"""
        result = resolve_universe(
            self._base_map,
            self._tickers,
            UniverseCfg(mode="all", asset_filter="all"),
        )
        assert len(result) == len(self._COINS)
        assert set(result.keys()) == set(self._COINS)

    def test_all_perp_sorted_by_vol_descending(self):
        """all_perp 模式：按 24h 成交额降序排列（高 vol 优先，模拟冷启动优先回填高流动性币）。"""
        result = resolve_universe(
            self._base_map,
            self._tickers,
            UniverseCfg(mode="all", asset_filter="all"),
        )
        expected_order = ["ETH", "BTC", "SOL", "DOGE", "AVAX", "LINK", "MATIC"]
        assert list(result.keys()) == expected_order

    def test_all_perp_maps_coin_to_symbol_correctly(self):
        """all_perp 模式：返回正确的 {coin: symbol} 映射。"""
        result = resolve_universe(
            self._base_map,
            self._tickers,
            UniverseCfg(mode="all", asset_filter="all"),
        )
        for coin in self._COINS:
            assert result[coin] == f"{coin}USDT"

    def test_all_perp_more_coins_than_top_n(self):
        """all_perp 模式：返回币数 > top_n（验证未被截断）。"""
        top_n_result = resolve_universe(
            self._base_map,
            self._tickers,
            UniverseCfg(mode="top_n", top_n=3),
        )
        all_perp_result = resolve_universe(
            self._base_map,
            self._tickers,
            UniverseCfg(mode="all", asset_filter="all"),
        )
        assert len(all_perp_result) > len(top_n_result)
        assert len(all_perp_result) == len(self._COINS)


# ---------------------------------------------------------------------------
# 边界场景：all_perp 模式容错测试
# ---------------------------------------------------------------------------

class TestAllPerpEdgeCases:
    """all_perp 模式边界：contracts 空/ticker 缺失/成交额缺失时容错。"""

    def test_empty_contracts_returns_empty_universe(self):
        """contracts 为空时，all_perp 模式返回空映射（优雅降级，不崩）。"""
        result = resolve_universe(
            {},         # 空 base_map
            {},         # 空 tickers
            UniverseCfg(mode="all", asset_filter="all"),
        )
        assert result == {}

    def test_missing_ticker_vol_treated_as_zero(self):
        """部分币缺失 ticker 信息时，视 vol=0（排在末尾，不崩）。"""
        base_map = _make_base_map(["BTC", "ETH", "SOL"])
        # SOL 没有对应 ticker
        tickers = _make_tickers(["BTC", "ETH"], [200.0, 500.0])

        result = resolve_universe(
            base_map,
            tickers,
            UniverseCfg(mode="all", asset_filter="all"),
        )
        # 全部三个币都应出现（SOL vol=0 排末尾）
        assert "BTC" in result
        assert "ETH" in result
        assert "SOL" in result
        # ETH(500) > BTC(200) > SOL(0)
        coins_list = list(result.keys())
        assert coins_list.index("ETH") < coins_list.index("BTC")
        assert coins_list.index("BTC") < coins_list.index("SOL")

    def test_invalid_vol_string_treated_as_zero(self):
        """成交额为非法字符串时，视 vol=0（_safe_vol 守卫，不崩）。"""
        base_map = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
        tickers = {
            "BTCUSDT": {"quoteVolume": "not_a_number"},  # 非法 vol
            "ETHUSDT": {"quoteVolume": "800.0"},
        }
        result = resolve_universe(
            base_map,
            tickers,
            UniverseCfg(mode="all", asset_filter="all"),
        )
        # 两个币都出现，ETH(800) 在前，BTC(0) 在后
        assert set(result.keys()) == {"BTC", "ETH"}
        coins_list = list(result.keys())
        assert coins_list[0] == "ETH"

    def test_all_perp_with_single_coin(self):
        """all_perp 模式：仅一个合约时正常返回（边界：最小集合）。"""
        base_map = {"BTCUSDT": "BTC"}
        tickers = {"BTCUSDT": {"quoteVolume": "1000000.0"}}
        result = resolve_universe(
            base_map,
            tickers,
            UniverseCfg(mode="all", asset_filter="all"),
        )
        assert result == {"BTC": "BTCUSDT"}

    def test_top_n_larger_than_available_coins(self):
        """top_n 超过可用币数时，返回全部可用币（不报错，向后兼容）。"""
        base_map = _make_base_map(["BTC", "ETH"])
        tickers = _make_tickers(["BTC", "ETH"], [500.0, 800.0])
        result = resolve_universe(
            base_map,
            tickers,
            UniverseCfg(mode="top_n", top_n=100),  # top_n 远大于实际币数
        )
        assert len(result) == 2  # 只有 2 个，不报错


# ---------------------------------------------------------------------------
# HarmonicCfg.universe_mode 字段 YAML 加载测试
# ---------------------------------------------------------------------------

class TestHarmonicCfgLoadFromDict:
    """HarmonicCfg 可从字典正确加载 universe_mode 字段（模拟 YAML 加载）。"""

    def test_load_top_n_mode(self):
        """从 YAML dict 加载 universe_mode='top_n' 正确。"""
        cfg = HarmonicCfg(universe_mode="top_n", top_n=20)
        assert cfg.universe_mode == "top_n"
        assert cfg.top_n == 20

    def test_load_all_perp_mode(self):
        """从 YAML dict 加载 universe_mode='all_perp' 正确。"""
        cfg = HarmonicCfg(universe_mode="all_perp")
        assert cfg.universe_mode == "all_perp"

    def test_default_still_top_n_when_not_specified(self):
        """不指定 universe_mode 时默认为 'top_n'（向后兼容：现有 config.yaml 无此字段不崩）。"""
        cfg = HarmonicCfg()  # 不指定 universe_mode
        assert cfg.universe_mode == "top_n"
