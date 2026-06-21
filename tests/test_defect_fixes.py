"""审计缺陷修复的回归测试（锁定易静默退化的修复点）。

覆盖本轮审计确认并修复的关键缺陷：
- stochastic %D 此前因 NaN 污染恒为 None（#20）
- rsi 全平盘此前误为 100→超买，应 50→中性（#37）
- fibonacci 非法 direction 此前静默按 down，应抛 ValueError（#40）
- 协同算法不应期键缺 side，致买/卖跨组去重错乱（#6）
- onchain 转账去重键改用 log_index（避免浮点 amount 入主键，#16）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.indicators import technical as T
from smc_tracker.indicators.fibonacci import fib_levels
from smc_tracker.monitor.address_correlation import AddressCorrelation
from smc_tracker.storage import Store


def test_stochastic_d_not_none():
    """#20：%D 应有有限值（此前 NaN 污染恒为 None）。"""
    rng = np.random.default_rng(0)
    c = 100 + np.cumsum(rng.normal(0, 1, 100))
    h, l = c + 1, c - 1
    k, dd = T.stochastic(h, l, c, 14, 3)
    assert np.isfinite(dd[-1])                      # 末值有效
    assert np.isfinite(dd[16:]).any()              # warmup 后有有效段


def test_rsi_flat_is_neutral():
    """#37：全平盘(close 恒定) RSI 应为 50(中性)，而非 100(超买)。"""
    c = np.full(50, 100.0)
    r = T.rsi(c, 14)
    assert abs(r[-1] - 50.0) < 1e-9


def test_rsi_pure_uptrend_is_100():
    """#37 边界：纯单边上涨仍应为 100(合法超买)。"""
    c = np.arange(1.0, 51.0)                        # 严格递增
    r = T.rsi(c, 14)
    assert abs(r[-1] - 100.0) < 1e-6


def test_fib_invalid_direction_raises():
    """#40：非法 direction 应抛 ValueError，不再静默降级。"""
    with pytest.raises(ValueError):
        fib_levels(100.0, 50.0, direction="sideways")
    # 合法方向(大小写无关)不抛
    assert fib_levels(100.0, 50.0, direction="UP")
    assert fib_levels(100.0, 50.0, direction="down")


def _trade(coin, side, taker, t):
    return (coin, 1.0, 1.0, 100.0, side, taker, "0xM", taker, "h", t, t)


def test_correlation_refractory_per_side():
    """#6：同币买/卖应独立计不应期，互不串扰。"""
    s = Store(Path(tempfile.mkdtemp()) / "s.db")
    rows = []
    for k in range(4):                              # A、B 同窗：先同向买、再同向卖
        t = k * 120_000
        rows.append(_trade("kPEPE", "B", "0xA", t))
        rows.append(_trade("kPEPE", "B", "0xB", t + 1000))
        rows.append(_trade("kPEPE", "A", "0xA", t + 2000))
        rows.append(_trade("kPEPE", "A", "0xB", t + 3000))
    s.insert_hl_meme_trades(rows)
    cm = AddressCorrelation(s).co_movers(since_ms=0, window_sec=60, min_shared=1)
    # 买、卖各 4 次协同独立计数 → 总计 8（此前共享不应期键会少计）
    pair = [c for a, b, c in cm if {a, b} == {"0xA", "0xB"}]
    assert pair and pair[0] == 8
    s.close()


def test_onchain_dedup_by_log_index():
    """#16：同 tx 同合约不同 log_index 视为不同转账(不被去重)。"""
    from smc_tracker.onchain.monitor import OnchainMemeMonitor
    s = Store(Path(tempfile.mkdtemp()) / "s.db")
    mon = OnchainMemeMonitor(s, {"ETH": "http://x"})
    # 列顺序：coin,chain,contract,from,to,amount,amount_usd,block,tx_hash,log_index,ts
    base = ("PEPE", "ETH", "0xc", "0xf", "0xt", 1.0, None, 100, "0xtx", )
    n1 = mon.insert([base + (0, 1)])               # log_index=0
    n2 = mon.insert([base + (1, 1)])               # 同 tx 不同 log → 不去重
    n3 = mon.insert([base + (0, 1)])               # 重复 log_index=0 → 去重
    assert n1 == 1 and n2 == 1 and n3 == 0
    s.close()


if __name__ == "__main__":
    import traceback
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  ✓ {name}")
            except Exception:
                traceback.print_exc()
    print("done")
