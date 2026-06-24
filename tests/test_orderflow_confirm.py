"""订单流确认层（confirm_setup）单测（合成数据，纯逻辑，无网络）。

TDD Red 阶段——先建测试，实现未存在时全部失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals.orderflow_confirm import OrderflowConfirm, confirm_setup


# ---- 假订单簿提供者（鸭子类型） ----

class _FakeOB:
    """提供 confirming_wall + book_imbalance 的桩对象。"""

    def __init__(
        self,
        wall: dict | None,
        imbalance: float,
    ) -> None:
        self._wall = wall
        self._imbalance = imbalance

    def confirming_wall(
        self, coin: str, price: float, side: str, tol_pct: float = 0.015
    ) -> dict | None:
        return self._wall

    def book_imbalance(self, coin: str) -> dict[str, float]:
        return {"imbalance": self._imbalance, "bid_usd": 0.0, "ask_usd": 0.0}


def _wall(notional: float = 1_000_000.0, px: float = 100.0, dist_pct: float = 0.001) -> dict:
    return {"px": px, "notional": notional, "n": 3, "dist_pct": dist_pct}


# ---- confirmed=True 场景 ----

def test_long_bid_wall_pos_imbalance_confirmed():
    """long + bid 支撑墙 + 正失衡(bid 占优) → confirmed=True。"""
    ob = _FakeOB(wall=_wall(notional=500_000), imbalance=0.3)
    result = confirm_setup("BTC", "long", entry_lo=99.0, entry_hi=101.0, ob_provider=ob)
    assert result is not None
    assert result.confirmed is True
    assert result.wall_usd == 500_000.0
    assert result.imbalance == 0.3


def test_short_ask_wall_neg_imbalance_confirmed():
    """short + ask 压制墙 + 负失衡(ask 占优) → confirmed=True。"""
    ob = _FakeOB(wall=_wall(notional=800_000), imbalance=-0.4)
    result = confirm_setup("ETH", "short", entry_lo=1990.0, entry_hi=2010.0, ob_provider=ob)
    assert result is not None
    assert result.confirmed is True
    assert result.wall_usd == 800_000.0
    assert result.imbalance == -0.4


# ---- confirmed=False 场景 ----

def test_long_bid_wall_neg_imbalance_not_confirmed():
    """long + bid 墙存在 但 失衡方向相反(ask 占优) → confirmed=False。"""
    ob = _FakeOB(wall=_wall(notional=500_000), imbalance=-0.2)
    result = confirm_setup("BTC", "long", entry_lo=99.0, entry_hi=101.0, ob_provider=ob)
    assert result is not None
    assert result.confirmed is False


def test_long_no_wall_not_confirmed():
    """long + PRZ 处无同向墙 → confirmed=False，wall_usd=0.0，wall_dist_pct=1.0。"""
    ob = _FakeOB(wall=None, imbalance=0.5)
    result = confirm_setup("BTC", "long", entry_lo=99.0, entry_hi=101.0, ob_provider=ob)
    assert result is not None
    assert result.confirmed is False
    assert result.wall_usd == 0.0
    assert result.wall_dist_pct == 1.0


def test_short_ask_wall_pos_imbalance_not_confirmed():
    """short + ask 墙存在 但 失衡为正(bid 占优) → confirmed=False。"""
    ob = _FakeOB(wall=_wall(notional=600_000), imbalance=0.1)
    result = confirm_setup("ETH", "short", entry_lo=1990.0, entry_hi=2010.0, ob_provider=ob)
    assert result is not None
    assert result.confirmed is False


def test_short_no_wall_not_confirmed():
    """short + 无墙 → confirmed=False，wall_usd=0.0。"""
    ob = _FakeOB(wall=None, imbalance=-0.3)
    result = confirm_setup("ETH", "short", entry_lo=1990.0, entry_hi=2010.0, ob_provider=ob)
    assert result is not None
    assert result.confirmed is False
    assert result.wall_usd == 0.0


# ---- ob_provider=None ----

def test_none_provider_returns_none():
    """ob_provider=None → 无订单流数据 → 返回 None。"""
    result = confirm_setup("BTC", "long", entry_lo=99.0, entry_hi=101.0, ob_provider=None)
    assert result is None


# ---- 非法参数 ----

def test_invalid_direction_returns_none():
    """direction 不是 long/short → 返回 None。"""
    ob = _FakeOB(wall=_wall(), imbalance=0.1)
    assert confirm_setup("BTC", "neutral", 99.0, 101.0, ob_provider=ob) is None


def test_nonpositive_price_returns_none():
    """entry_lo <= 0 → 中点 <= 0 → 返回 None。"""
    ob = _FakeOB(wall=_wall(), imbalance=0.1)
    assert confirm_setup("BTC", "long", entry_lo=0.0, entry_hi=0.0, ob_provider=ob) is None
    assert confirm_setup("BTC", "long", entry_lo=-10.0, entry_hi=-5.0, ob_provider=ob) is None


# ---- min_wall_usd 过滤 ----

def test_min_wall_usd_filter():
    """墙存在但名义小于 min_wall_usd → confirmed=False。"""
    ob = _FakeOB(wall=_wall(notional=50_000), imbalance=0.3)
    result = confirm_setup(
        "BTC", "long", 99.0, 101.0, ob_provider=ob, min_wall_usd=100_000.0
    )
    assert result is not None
    assert result.confirmed is False
    # wall_usd 仍记录实际值（真实墙额）
    assert result.wall_usd == 50_000.0


# ---- note 字段诚实标注 ----

def test_confirmed_note_contains_spoof_warning():
    """confirmed 时，note 含 spoof 警告（诚实铁律）。"""
    ob = _FakeOB(wall=_wall(notional=500_000), imbalance=0.3)
    result = confirm_setup("BTC", "long", 99.0, 101.0, ob_provider=ob)
    assert result is not None
    assert result.confirmed is True
    assert "spoof" in result.note


def test_no_wall_note_contains_caution():
    """无墙时，note 含谨慎字样。"""
    ob = _FakeOB(wall=None, imbalance=0.3)
    result = confirm_setup("BTC", "long", 99.0, 101.0, ob_provider=ob)
    assert result is not None
    assert "无" in result.note or "谨慎" in result.note or "no" in result.note.lower()


# ---- dataclass 结构 ----

def test_orderflow_confirm_fields():
    """OrderflowConfirm dataclass 有所有必须字段。"""
    obj = OrderflowConfirm(
        confirmed=True,
        wall_usd=1_000_000.0,
        wall_dist_pct=0.005,
        imbalance=0.4,
        note="test",
    )
    assert obj.confirmed is True
    assert obj.wall_usd == 1_000_000.0
    assert obj.wall_dist_pct == 0.005
    assert obj.imbalance == 0.4
    assert obj.note == "test"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
