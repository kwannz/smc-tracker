"""配置化币种选择单测：UniverseCfg dataclass + resolve_universe 纯函数。

TDD RED 阶段先写，config.py 新增 UniverseCfg/resolve_universe 前全部失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# RED 阶段：这些导入将失败(ImportError/AttributeError)
from smc_tracker.config import UniverseCfg, resolve_universe


# ---------------------------------------------------------------------------
# 辅助数据：合成 tickers 和 base_map
# ---------------------------------------------------------------------------

def _make_tickers(coins: list[str], volumes: list[float]) -> dict[str, dict]:
    """生成合成 tickers dict，quoteVolume 按指定值设置。"""
    return {
        f"{coin}USDT_UMCBL": {"quoteVolume": str(vol), "baseCoin": coin}
        for coin, vol in zip(coins, volumes)
    }


def _make_base_map(coins: list[str]) -> dict[str, str]:
    """生成合成 base_map: **symbol → baseCoin**（与 BitgetREST.perp_base_coins() 真实契约一致）。"""
    return {f"{coin}USDT_UMCBL": coin for coin in coins}


# 默认测试集：5个币，成交额降序 ETH>BTC>SOL>SOXL>XAU
_COINS = ["BTC", "ETH", "SOL", "SOXL", "XAU"]
_VOLS = [500.0, 800.0, 300.0, 50.0, 20.0]
_BASE_MAP = _make_base_map(_COINS)
_TICKERS = _make_tickers(_COINS, _VOLS)

# 按成交额降序排列：ETH(800)>BTC(500)>SOL(300)>SOXL(50)>XAU(20)
_EXPECTED_ORDER = ["ETH", "BTC", "SOL", "SOXL", "XAU"]


# ---------------------------------------------------------------------------
# UniverseCfg dataclass 基本属性
# ---------------------------------------------------------------------------

def test_universe_cfg_default_values():
    """UniverseCfg 默认值：mode='top_n', top_n=12, include=[], exclude=[], asset_filter='all'。"""
    cfg = UniverseCfg()
    assert cfg.mode == "top_n"
    assert cfg.top_n == 12
    assert cfg.include == []
    assert cfg.exclude == []
    assert cfg.asset_filter == "all"


def test_universe_cfg_custom_values():
    """UniverseCfg 可自定义所有字段。"""
    cfg = UniverseCfg(
        mode="list",
        top_n=5,
        include=["BTC", "ETH"],
        exclude=["DOGE"],
        asset_filter="crypto",
    )
    assert cfg.mode == "list"
    assert cfg.top_n == 5
    assert cfg.include == ["BTC", "ETH"]
    assert cfg.exclude == ["DOGE"]
    assert cfg.asset_filter == "crypto"


def test_universe_cfg_is_dataclass_with_slots():
    """UniverseCfg 是 slots=True 的 dataclass（不允许动态属性）。"""
    import dataclasses
    cfg = UniverseCfg()
    assert dataclasses.is_dataclass(cfg)
    # slots=True 时 __slots__ 存在于类
    assert hasattr(UniverseCfg, "__slots__")


def test_universe_cfg_include_exclude_are_independent_lists():
    """不同 UniverseCfg 实例的 include/exclude 列表互不干扰（无可变默认值共享）。"""
    a = UniverseCfg()
    b = UniverseCfg()
    a.include.append("BTC")
    assert "BTC" not in b.include, "include 列表不应共享同一对象"


# ---------------------------------------------------------------------------
# resolve_universe — mode="all"
# ---------------------------------------------------------------------------

def test_resolve_universe_mode_all_returns_all_symbols():
    """mode='all' 返回所有 base_map 中的 coin→symbol 映射。"""
    cfg = UniverseCfg(mode="all")
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert set(result.keys()) == set(_COINS)


def test_resolve_universe_mode_all_correct_symbol_mapping():
    """mode='all' 时每个 coin 映射到正确 symbol。"""
    cfg = UniverseCfg(mode="all")
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    for coin in _COINS:
        assert result[coin] == f"{coin}USDT_UMCBL"


# ---------------------------------------------------------------------------
# resolve_universe — mode="top_n" + 成交额排序
# ---------------------------------------------------------------------------

def test_resolve_universe_mode_top_n_returns_n_coins():
    """mode='top_n', top_n=3 返回正好 3 个 coin。"""
    cfg = UniverseCfg(mode="top_n", top_n=3)
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert len(result) == 3


def test_resolve_universe_mode_top_n_sorted_by_volume():
    """mode='top_n' 选取成交额最高的 N 个：ETH(800)>BTC(500)>SOL(300) 为前3。"""
    cfg = UniverseCfg(mode="top_n", top_n=3)
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert set(result.keys()) == {"ETH", "BTC", "SOL"}


def test_resolve_universe_mode_top_n_larger_than_available():
    """top_n 超过可用 coin 数时，返回全部（不报错）。"""
    cfg = UniverseCfg(mode="top_n", top_n=100)
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert len(result) == len(_COINS)


def test_resolve_universe_mode_top_n_zero():
    """top_n=0 返回空 dict（不报错）。"""
    cfg = UniverseCfg(mode="top_n", top_n=0)
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert result == {}


# ---------------------------------------------------------------------------
# resolve_universe — mode="list" (include)
# ---------------------------------------------------------------------------

def test_resolve_universe_mode_list_returns_only_include():
    """mode='list' 只返回 include 列表中指定的 coin。"""
    cfg = UniverseCfg(mode="list", include=["BTC", "ETH"])
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert set(result.keys()) == {"BTC", "ETH"}


def test_resolve_universe_mode_list_ignores_unknown_coin():
    """mode='list' include 中包含不存在的 coin 时，忽略该 coin（不报错）。"""
    cfg = UniverseCfg(mode="list", include=["BTC", "FOOBARXYZ"])
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert set(result.keys()) == {"BTC"}
    assert "FOOBARXYZ" not in result


def test_resolve_universe_mode_list_empty_include():
    """mode='list' include=[] 时返回空 dict。"""
    cfg = UniverseCfg(mode="list", include=[])
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert result == {}


# ---------------------------------------------------------------------------
# resolve_universe — exclude 剔除
# ---------------------------------------------------------------------------

def test_resolve_universe_exclude_removes_coins():
    """exclude 从结果中剔除指定 coin（不论 mode）。"""
    cfg = UniverseCfg(mode="all", exclude=["BTC", "XAU"])
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert "BTC" not in result
    assert "XAU" not in result


def test_resolve_universe_exclude_top_n():
    """mode='top_n' + exclude 先选 top_n 再剔除（剔除后可能少于 top_n，不补充）。"""
    # top_n=3 → ETH,BTC,SOL；exclude BTC → ETH,SOL
    cfg = UniverseCfg(mode="top_n", top_n=3, exclude=["BTC"])
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert "BTC" not in result
    assert "ETH" in result
    assert "SOL" in result


def test_resolve_universe_exclude_list_mode():
    """mode='list' + exclude：include 中指定但 exclude 中也有的 coin 被剔除。"""
    cfg = UniverseCfg(mode="list", include=["BTC", "ETH", "SOL"], exclude=["SOL"])
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert "SOL" not in result
    assert set(result.keys()) == {"BTC", "ETH"}


def test_resolve_universe_exclude_unknown_coin_no_error():
    """exclude 中包含不在 base_map 的 coin 不报错。"""
    cfg = UniverseCfg(mode="all", exclude=["FOOBARXYZ"])
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert set(result.keys()) == set(_COINS)


# ---------------------------------------------------------------------------
# resolve_universe — asset_filter
# ---------------------------------------------------------------------------

def test_resolve_universe_asset_filter_crypto_excludes_tradfi():
    """asset_filter='crypto' 时 SOXL/XAU(tradfi) 被过滤掉，BTC/ETH/SOL(crypto) 保留。"""
    cfg = UniverseCfg(mode="all", asset_filter="crypto")
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert "SOXL" not in result
    assert "XAU" not in result
    assert "BTC" in result
    assert "ETH" in result
    assert "SOL" in result


def test_resolve_universe_asset_filter_tradfi_excludes_crypto():
    """asset_filter='tradfi' 时 BTC/ETH/SOL(crypto) 被过滤掉，SOXL/XAU(tradfi) 保留。"""
    cfg = UniverseCfg(mode="all", asset_filter="tradfi")
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert "BTC" not in result
    assert "ETH" not in result
    assert "SOL" not in result
    assert "SOXL" in result
    assert "XAU" in result


def test_resolve_universe_asset_filter_all_keeps_everything():
    """asset_filter='all'(默认) 不过滤任何资产类别。"""
    cfg = UniverseCfg(mode="all", asset_filter="all")
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert set(result.keys()) == set(_COINS)


def test_resolve_universe_asset_filter_crypto_top_n():
    """asset_filter='crypto' + mode='top_n', top_n=2 → 成交额最高的 2 个 crypto。"""
    # crypto：ETH(800)、BTC(500)、SOL(300)；top_n=2 → ETH、BTC
    cfg = UniverseCfg(mode="top_n", top_n=2, asset_filter="crypto")
    result = resolve_universe(_BASE_MAP, _TICKERS, cfg)
    assert set(result.keys()) == {"ETH", "BTC"}


# ---------------------------------------------------------------------------
# resolve_universe — quoteVolume 缺失/无效时鲁棒性
# ---------------------------------------------------------------------------

def test_resolve_universe_missing_quoteVolume_treated_as_zero():
    """tickers 中 quoteVolume 缺失的 coin 成交额视为 0（不崩溃，排在末尾）。"""
    coins = ["BTC", "ETH", "SOL"]
    base_map = _make_base_map(coins)
    tickers = {
        "BTCUSDT_UMCBL": {"baseCoin": "BTC"},  # quoteVolume 缺失
        "ETHUSDT_UMCBL": {"quoteVolume": "800.0", "baseCoin": "ETH"},
        "SOLUSDT_UMCBL": {"quoteVolume": "300.0", "baseCoin": "SOL"},
    }
    cfg = UniverseCfg(mode="top_n", top_n=2)
    result = resolve_universe(base_map, tickers, cfg)
    # ETH(800) > SOL(300) 为前2，BTC(0) 排末尾不选
    assert set(result.keys()) == {"ETH", "SOL"}


def test_resolve_universe_invalid_quoteVolume_treated_as_zero():
    """tickers 中 quoteVolume 为无效字符串时视为 0（不崩溃）。"""
    coins = ["BTC", "ETH"]
    base_map = _make_base_map(coins)
    tickers = {
        "BTCUSDT_UMCBL": {"quoteVolume": "NaN", "baseCoin": "BTC"},
        "ETHUSDT_UMCBL": {"quoteVolume": "800.0", "baseCoin": "ETH"},
    }
    cfg = UniverseCfg(mode="top_n", top_n=1)
    result = resolve_universe(base_map, tickers, cfg)
    assert set(result.keys()) == {"ETH"}


def test_resolve_universe_symbol_not_in_tickers_treated_as_zero():
    """base_map 中 symbol 不在 tickers 时成交额视为 0（不崩溃）。"""
    coins = ["BTC", "ETH", "UNKNOWN"]
    base_map = _make_base_map(coins)
    tickers = {
        "BTCUSDT_UMCBL": {"quoteVolume": "500.0", "baseCoin": "BTC"},
        "ETHUSDT_UMCBL": {"quoteVolume": "800.0", "baseCoin": "ETH"},
        # UNKNOWNUSDT_UMCBL 不在 tickers
    }
    cfg = UniverseCfg(mode="top_n", top_n=2)
    result = resolve_universe(base_map, tickers, cfg)
    assert set(result.keys()) == {"ETH", "BTC"}


# ---------------------------------------------------------------------------
# resolve_universe — 空输入边界
# ---------------------------------------------------------------------------

def test_resolve_universe_empty_base_map():
    """base_map 为空 → 返回空 dict（任何 mode）。"""
    cfg = UniverseCfg(mode="all")
    assert resolve_universe({}, _TICKERS, cfg) == {}


def test_resolve_universe_empty_tickers():
    """tickers 为空 → 所有 quoteVolume=0，mode='top_n' 仍返回 top_n 个（按 0 成交额）。"""
    cfg = UniverseCfg(mode="top_n", top_n=2)
    result = resolve_universe(_BASE_MAP, {}, cfg)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Config.load() 集成：universe 字段从 YAML 正确加载
# ---------------------------------------------------------------------------

def test_config_load_universe_defaults(tmp_path: Path):
    """Config.load YAML 中无 universe 字段 → UniverseCfg 默认值。"""
    import yaml
    from smc_tracker.config import Config
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump({}), encoding="utf-8")
    cfg = Config.load(cfg_file)
    assert hasattr(cfg, "universe")
    assert isinstance(cfg.universe, UniverseCfg)
    assert cfg.universe.mode == "top_n"
    assert cfg.universe.top_n == 12


def test_config_load_universe_from_yaml(tmp_path: Path):
    """Config.load YAML 中有 universe 字段 → 正确解析到 UniverseCfg。"""
    import yaml
    from smc_tracker.config import Config
    data = {
        "universe": {
            "mode": "list",
            "top_n": 20,
            "include": ["BTC", "ETH"],
            "exclude": ["DOGE"],
            "asset_filter": "crypto",
        }
    }
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump(data), encoding="utf-8")
    cfg = Config.load(cfg_file)
    assert cfg.universe.mode == "list"
    assert cfg.universe.top_n == 20
    assert cfg.universe.include == ["BTC", "ETH"]
    assert cfg.universe.exclude == ["DOGE"]
    assert cfg.universe.asset_filter == "crypto"


def test_config_load_universe_partial_yaml(tmp_path: Path):
    """Config.load YAML 中 universe 只有部分字段 → 其余用默认值。"""
    import yaml
    from smc_tracker.config import Config
    data = {"universe": {"mode": "all"}}
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump(data), encoding="utf-8")
    cfg = Config.load(cfg_file)
    assert cfg.universe.mode == "all"
    assert cfg.universe.top_n == 12      # 默认值
    assert cfg.universe.include == []    # 默认值
    assert cfg.universe.exclude == []    # 默认值
    assert cfg.universe.asset_filter == "all"  # 默认值


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except Exception as e:
                print(f"  ✗ {name}: {e}")
