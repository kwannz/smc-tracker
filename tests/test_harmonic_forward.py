"""谐波前瞻信号 provider（HarmonicForwardSignals）单测。

职责：每轮 refresh 用 Bitget tickers 快照（OI/funding/price）更新——构建每币 CoinSignalProfile +
**变化才采样**累积 funding 历史算极值（C1 修复：funding 8h 才变，按 refresh 采样会灌满重复值）+
从 OI/price 帧间变化算方向化 OI 信号（C2 修复：oi_directional_velocity 接线，不再孤儿）。
__call__ 返回 (profile, flow_score, oi_signal, funding_extreme) 供 apply_forward 施加前瞻乘子。

诚实分层：flow_score 来自 BitgetTradeMonitor（资金流加速度，仅此一项，非"三合一"）；OI 方向化为
独立分量；funding 极值独立分量；纯股票 funding=0 按 has_funding 门控跳过。
"""
from __future__ import annotations

from smc_tracker.monitor.harmonic_forward import HarmonicForwardSignals


def _parsed(coin, oi, funding, price=100.0, symbol=None):
    return {coin: {"symbol": symbol or coin + "USDT", "oi": oi, "funding": funding, "price": price}}


def test_unknown_coin_returns_none():
    fs = HarmonicForwardSignals()
    assert fs("BTC", "long") is None


def test_profile_built_from_ticker():
    fs = HarmonicForwardSignals()
    fs.update(_parsed("BTC", oi=33000.0, funding=0.0001), now_ms=1000)
    sig = fs("BTC", "long")
    assert sig is not None
    profile, flow_score, oi_signal, funding_extreme = sig
    assert profile.asset_class == "crypto"
    assert profile.has_funding is True
    assert flow_score is None  # 无 flow_source


def test_stock_zero_funding_profile():
    fs = HarmonicForwardSignals()
    fs.update(_parsed("TSLA", oi=24000.0, funding=0.0), now_ms=1000)
    profile, _, _, _ = fs("TSLA", "long")
    assert profile.asset_class == "tradfi_stock"
    assert profile.has_funding is False


def test_funding_extreme_after_distinct_history():
    """变化采样：喂足够多**不同** funding 后，极端 funding → funding_extreme 非零（看跌反转<0）。"""
    fs = HarmonicForwardSignals(min_funding_samples=20)
    for i in range(25):
        f = 0.0001 + 0.00002 * ((i % 7) - 3) + i * 1e-7  # 每步不同，模拟多 epoch
        fs.update(_parsed("ETH", oi=5000.0, funding=f), now_ms=1000 + i)
    fs.update(_parsed("ETH", oi=5000.0, funding=0.01), now_ms=2000)  # 极高
    _, _, _, funding_extreme = fs("ETH", "long")
    assert funding_extreme is not None and funding_extreme < 0.0


def test_funding_constant_not_resampled():
    """C1：funding 恒定（多轮 refresh 同值）→ 去重后历史不足 → funding_extreme=0（不臆测）。"""
    fs = HarmonicForwardSignals(min_funding_samples=20)
    for i in range(30):
        fs.update(_parsed("BTC", oi=1.0, funding=0.0005), now_ms=1000 + i)  # 30 轮同值
    _, _, _, funding_extreme = fs("BTC", "long")
    assert funding_extreme == 0.0   # 去重后仅 1 个不同值 → 不足 → 0


def test_oi_signal_from_oi_price_change():
    """C2：帧间 OI↑+价↑=新多 → oi_signal > 0（方向化 OI 真接线）。"""
    fs = HarmonicForwardSignals()
    fs.update(_parsed("BTC", oi=1000.0, funding=0.0001, price=100.0), now_ms=1000)
    fs.update(_parsed("BTC", oi=1050.0, funding=0.0001, price=110.0), now_ms=2000)
    _, _, oi_signal, _ = fs("BTC", "long")
    assert oi_signal is not None and oi_signal > 0.0


def test_oi_signal_none_on_first_update():
    """首帧无前值 → oi_signal=0（无法算速度）。"""
    fs = HarmonicForwardSignals()
    fs.update(_parsed("BTC", oi=1000.0, funding=0.0001, price=100.0), now_ms=1000)
    _, _, oi_signal, _ = fs("BTC", "long")
    assert oi_signal == 0.0


def test_flow_source_provides_flow_score():
    fs = HarmonicForwardSignals(flow_source=lambda coin: 0.6 if coin == "BTC" else None)
    fs.update(_parsed("BTC", oi=1.0, funding=0.0001), now_ms=1000)
    _, flow_score, _, _ = fs("BTC", "long")
    assert flow_score == 0.6


def test_no_flow_source_keeps_flow_none():
    fs = HarmonicForwardSignals()
    fs.update(_parsed("BTC", oi=1.0, funding=0.0001), now_ms=1000)
    _, flow_score, _, _ = fs("BTC", "long")
    assert flow_score is None


# ---------------------------------------------------------------------------
# P1: _oi_signal 死状态已删除（不应出现在 __slots__）
# ---------------------------------------------------------------------------

def test_p1_no_oi_signal_slot():
    """P1：_oi_signal 是死状态，不应存在于 __slots__ 中（已删除）。"""
    assert "_oi_signal" not in HarmonicForwardSignals.__slots__, (
        "_oi_signal 是死状态，应已从 __slots__ 删除"
    )


def test_p1_no_oi_signal_in_instance():
    """P1：实例不应有 _oi_signal 属性（slots 删除后实例也无此属性）。"""
    fs = HarmonicForwardSignals()
    fs.update(_parsed("BTC", oi=1000.0, funding=0.0001, price=100.0), now_ms=1000)
    assert not hasattr(fs, "_oi_signal"), "_oi_signal 死状态应已从实例中删除"


# ---------------------------------------------------------------------------
# P2: OI 加速度在 v_sig=0 时不应被归零
# ---------------------------------------------------------------------------

def test_p2_accel_contributes_when_velocity_zero():
    """P2：价格不变（v_sig=0）时，OI 持续加速仍应产生非零 oi_signal（加速度不应被归零）。

    构造：OI 持续加速增长，但价格恒定（v_sig=0）。
    修复前：a_sig = a_raw * 0.0 = 0（错误归零）。
    修复后：a_sig = a_raw（保留加速度信号）。
    需要 ≥3 帧 OI 历史才能计算加速度（_MIN_OI_FRAMES=3）。
    """
    fs = HarmonicForwardSignals()
    # 价格固定=100，OI 持续加速增长（一阶导在增加）
    # 帧1: OI=1000
    fs.update(_parsed("BTC", oi=1000.0, funding=0.0001, price=100.0), now_ms=1000)
    # 帧2: OI=1020（+2%）
    fs.update(_parsed("BTC", oi=1020.0, funding=0.0001, price=100.0), now_ms=2000)
    # 帧3: OI=1060（+3.9%，加速：速度从2%→~3.9%）
    fs.update(_parsed("BTC", oi=1060.0, funding=0.0001, price=100.0), now_ms=3000)
    # 帧4: OI=1120（+5.7%，加速：速度继续增加）
    fs.update(_parsed("BTC", oi=1120.0, funding=0.0001, price=100.0), now_ms=4000)
    _, _, oi_signal, _ = fs("BTC", "long")
    # 价格不变 → v_sig=0，但 OI 在加速，accel 应非零，oi_signal 应非零
    assert oi_signal is not None and oi_signal != 0.0, (
        f"价格不变时 OI 加速应产生非零信号，得到 oi_signal={oi_signal}"
    )
