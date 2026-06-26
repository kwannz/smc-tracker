"""microprice 三件套单测：queue_imbalance / micro_price / ofi_delta / OFITracker (C.1).

所有数据合成确定性，含手算 golden oracle。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals.microprice import (
    OFITracker,
    micro_price,
    ofi_delta,
    queue_imbalance,
)


def _lv(px: float, sz: float) -> dict:
    """构造档位 dict（px/sz 为字符串，仿真实 HL 推送）。"""
    return {"px": str(px), "sz": str(sz)}


# ──────────────────────── queue_imbalance ────────────────────────

def test_queue_imbalance_bid_heavy():
    """买盘 size 远厚 → ≈+1（正=看涨）。"""
    bids = [_lv(100.0, 100.0)] * 5
    asks = [_lv(101.0, 1.0)] * 5
    # bid_sz=500, ask_sz=5 → (500-5)/505 ≈ 0.9802
    qi = queue_imbalance(bids, asks)
    assert qi > 0.9, f"expected >0.9, got {qi}"


def test_queue_imbalance_ask_heavy():
    """卖盘 size 远厚 → 接近 -1（看跌）。"""
    bids = [_lv(100.0, 1.0)] * 5
    asks = [_lv(101.0, 100.0)] * 5
    qi = queue_imbalance(bids, asks)
    assert qi < -0.9, f"expected <-0.9, got {qi}"


def test_queue_imbalance_symmetric():
    """买卖 size 对称 → 0.0。"""
    bids = [_lv(100.0, 10.0)] * 5
    asks = [_lv(101.0, 10.0)] * 5
    qi = queue_imbalance(bids, asks)
    assert abs(qi) < 1e-9, f"expected 0, got {qi}"


def test_queue_imbalance_empty():
    """空盘 → 0.0（不崩）。"""
    assert queue_imbalance([], []) == 0.0
    assert queue_imbalance([_lv(100, 0)], [_lv(101, 0)]) == 0.0


def test_queue_imbalance_remote_large_ask_not_flip():
    """仅 depth 内的档位参与：深处大额虚挂不翻转符号（depth=5）。"""
    # depth=5: bid 前5档 size=5×10=50; ask 前5档 size=5×1=5; 第6档 ask size=99999 在 depth 外
    bids = [_lv(100.0 - i, 10.0) for i in range(10)]
    asks = [_lv(101.0 + i, 1.0) for i in range(5)] + [_lv(110.0, 99999.0)]
    qi = queue_imbalance(bids, asks, depth=5)
    # bid_sz=50, ask_sz=5 → 正值
    assert qi > 0.0, f"深处大额 ask 不应翻转符号，got {qi}"


# ──────────────────────── micro_price ────────────────────────

def test_micro_price_bid_heavy():
    """bid_sz ≫ ask_sz → micro 趋近 ask_px，tilt > 0（买压，前瞻看涨）。

    手算 golden：bid_px=100, bid_sz=100, ask_px=102, ask_sz=1
    micro = (100*1 + 102*100) / (1+100) = (100+10200)/101 = 10300/101 ≈ 101.98
    mid = (100+102)/2 = 101
    tilt = (101.98 - 101)/101 ≈ 0.00970...
    """
    bids = [_lv(100.0, 100.0)]
    asks = [_lv(102.0, 1.0)]
    r = micro_price(bids, asks)
    expected_micro = (100.0 * 1.0 + 102.0 * 100.0) / 101.0
    expected_mid = 101.0
    expected_tilt = (expected_micro - expected_mid) / expected_mid
    assert abs(r["micro"] - expected_micro) < 1e-9, f"micro={r['micro']}"
    assert abs(r["mid"] - expected_mid) < 1e-9, f"mid={r['mid']}"
    assert abs(r["tilt"] - expected_tilt) < 1e-9, f"tilt={r['tilt']}"
    assert r["tilt"] > 0, "bid_sz >> ask_sz → tilt>0（买压看涨）"


def test_micro_price_ask_heavy():
    """ask_sz ≫ bid_sz → micro 趋近 bid_px，tilt < 0（卖压，前瞻看跌）。"""
    bids = [_lv(100.0, 1.0)]
    asks = [_lv(102.0, 100.0)]
    r = micro_price(bids, asks)
    assert r["tilt"] < 0, f"tilt={r['tilt']} 应为负"


def test_micro_price_symmetric():
    """bid_sz == ask_sz → micro = mid，tilt ≈ 0。

    手算：bid_px=100, sz=10; ask_px=102, sz=10
    micro = (100*10 + 102*10)/20 = 2020/20 = 101 = mid
    tilt = 0
    """
    bids = [_lv(100.0, 10.0)]
    asks = [_lv(102.0, 10.0)]
    r = micro_price(bids, asks)
    assert abs(r["tilt"]) < 1e-9, f"对称时 tilt={r['tilt']} 应≈0"


def test_micro_price_zero_size_no_divide_by_zero():
    """bid_sz=ask_sz=0 → 不除零，tilt=0。"""
    bids = [_lv(100.0, 0.0)]
    asks = [_lv(102.0, 0.0)]
    r = micro_price(bids, asks)
    assert r["tilt"] == 0.0
    assert math.isfinite(r["micro"])


def test_micro_price_empty():
    """空盘 → 全零不崩。"""
    r = micro_price([], [])
    assert r["tilt"] == 0.0 and r["micro"] == 0.0


# ──────────────────────── ofi_delta ────────────────────────

def test_ofi_delta_bid_px_up():
    """bid px 上移 → e_b = +cur_bid_sz；ask 不变 e_a=0 → ofi=+cur_bid_sz。

    手算：prev_bid=(100,5) cur_bid=(101,8); prev_ask=(102,3) cur_ask=(102,3)
    e_b = +8（bid_px 上移）
    e_a = 0（ask_px 同 + sz 不变 → e_a = 3-3 = 0）
    ofi = 8 - 0 = 8
    """
    delta = ofi_delta((100.0, 5.0), (102.0, 3.0), (101.0, 8.0), (102.0, 3.0))
    assert abs(delta - 8.0) < 1e-9, f"expected 8.0, got {delta}"


def test_ofi_delta_bid_same_px_sz_increase():
    """bid px 不变 sz 增大 → e_b = +差值（正向净增）。

    手算：prev_bid=(100,3) cur_bid=(100,7) → e_b=7-3=4
    ask 不变 → e_a=0；ofi=4
    """
    delta = ofi_delta((100.0, 3.0), (102.0, 5.0), (100.0, 7.0), (102.0, 5.0))
    assert abs(delta - 4.0) < 1e-9, f"expected 4.0, got {delta}"


def test_ofi_delta_bid_px_down():
    """bid px 下移 → e_b = -prev_bid_sz（买单撤，看跌）。

    手算：prev_bid=(100,5) cur_bid=(99,3) → e_b=-5
    ask 不变 → e_a=0；ofi=-5
    """
    delta = ofi_delta((100.0, 5.0), (102.0, 3.0), (99.0, 3.0), (102.0, 3.0))
    assert abs(delta - (-5.0)) < 1e-9, f"expected -5.0, got {delta}"


def test_ofi_delta_ask_px_down():
    """ask px 下移 → e_a = +cur_ask_sz（卖方激进→压制买方）→ ofi 减小。

    手算：bid 不变；prev_ask=(102,3) cur_ask=(101,4) → e_a=+4；e_b=0；ofi=-4
    """
    delta = ofi_delta((100.0, 5.0), (102.0, 3.0), (100.0, 5.0), (101.0, 4.0))
    assert abs(delta - (-4.0)) < 1e-9, f"expected -4.0, got {delta}"


def test_ofi_delta_ask_px_up():
    """ask px 上移 → e_a = -prev_ask_sz（卖单撤，利好买方）→ ofi 增加。

    手算：bid 不变；prev_ask=(102,3) cur_ask=(103,2) → e_a=-3；e_b=0；ofi=+3
    """
    delta = ofi_delta((100.0, 5.0), (102.0, 3.0), (100.0, 5.0), (103.0, 2.0))
    assert abs(delta - 3.0) < 1e-9, f"expected 3.0, got {delta}"


def test_ofi_delta_ask_same_px_sz_change():
    """ask 同价 size 变化 → e_a = cur_ask_sz - prev_ask_sz（补全 ask 三分支覆盖）。

    手算：bid 不变(e_b=0)；prev_ask=(102,3) cur_ask=(102,7) → e_a=7-3=4；ofi=e_b-e_a=-4。
    ask 挂量增厚=卖压增→ofi 减小，与 ask_px_down 同号(看跌)，符号正确。
    """
    delta = ofi_delta((100.0, 5.0), (102.0, 3.0), (100.0, 5.0), (102.0, 7.0))
    assert abs(delta - (-4.0)) < 1e-9, f"expected -4.0, got {delta}"
    # 对照：ask 挂量减薄=卖压减→ofi 增大(看涨)
    delta2 = ofi_delta((100.0, 5.0), (102.0, 7.0), (100.0, 5.0), (102.0, 3.0))
    assert abs(delta2 - 4.0) < 1e-9, f"expected 4.0, got {delta2}"


def test_ofi_delta_invalid_returns_zero():
    """任一 px/sz 无效（负、NaN）→ 返回 0.0。"""
    import math
    assert ofi_delta((-1.0, 5.0), (102.0, 3.0), (100.0, 5.0), (102.0, 3.0)) == 0.0
    assert ofi_delta((100.0, 5.0), (102.0, 3.0), (float("nan"), 5.0), (102.0, 3.0)) == 0.0


def test_ofi_delta_mixed_frame_golden():
    """混合帧手算 golden：

    prev_bid=(100,5), prev_ask=(102,3)
    cur_bid=(101,8), cur_ask=(101,4)

    e_b: bid_px 上移(101>100) → +cur_bid_sz = +8
    e_a: ask_px 下移(101<102) → +cur_ask_sz = +4
    ofi = 8 - 4 = 4
    """
    delta = ofi_delta((100.0, 5.0), (102.0, 3.0), (101.0, 8.0), (101.0, 4.0))
    assert abs(delta - 4.0) < 1e-9, f"mixed golden expected 4.0, got {delta}"


# ──────────────────────── OFITracker ────────────────────────

def test_ofi_tracker_first_frame_returns_zero():
    """首帧无 prev → 返回 0.0，但记录 prev 状态（下一帧可计算 delta）。"""
    tracker = OFITracker()
    bids = [_lv(100.0, 5.0)]
    asks = [_lv(102.0, 3.0)]
    delta = tracker.update("BTC", bids, asks, ts=1000)
    assert delta == 0.0


def test_ofi_tracker_second_frame_computes_delta():
    """第二帧计算 delta（与 ofi_delta 一致）。"""
    tracker = OFITracker()
    tracker.update("BTC", [_lv(100.0, 5.0)], [_lv(102.0, 3.0)], ts=1000)
    # bid px 上移 → e_b=8；ask 不变 → e_a=0；delta=8
    delta = tracker.update("BTC", [_lv(101.0, 8.0)], [_lv(102.0, 3.0)], ts=2000)
    assert abs(delta - 8.0) < 1e-9, f"expected 8.0, got {delta}"


def test_ofi_tracker_normalized_net_buy():
    """多帧净买流 → normalized 接近 +1。"""
    tracker = OFITracker(window_ms=60_000)
    ts = 1_000
    # 首帧
    tracker.update("BTC", [_lv(100.0, 1.0)], [_lv(102.0, 1.0)], ts=ts)
    # 后续帧：持续 bid px 上移 → 每帧 e_b>0，e_a=0
    for i in range(1, 10):
        ts += 1000
        tracker.update("BTC", [_lv(100.0 + i, 5.0)], [_lv(102.0 + i, 1.0)], ts=ts)
    norm = tracker.normalized("BTC", now_ms=ts)
    assert norm > 0.5, f"净买流下 normalized={norm} 应>0.5"


def test_ofi_tracker_normalized_cancel_out():
    """交替 bid 上移/下移 → normalized 接近 0。"""
    tracker = OFITracker(window_ms=60_000)
    ts = 1000
    tracker.update("BTC", [_lv(100.0, 5.0)], [_lv(102.0, 3.0)], ts=ts)
    # 交替：bid 上移 then 下移 × N
    bids_up = [_lv(101.0, 5.0)]
    bids_dn = [_lv(100.0, 5.0)]
    asks_flat = [_lv(102.0, 3.0)]
    for _ in range(5):
        ts += 1000
        tracker.update("BTC", bids_up, asks_flat, ts=ts)
        ts += 1000
        tracker.update("BTC", bids_dn, asks_flat, ts=ts)
    norm = tracker.normalized("BTC", now_ms=ts)
    assert abs(norm) < 0.3, f"交替时 normalized={norm} 应接近 0"


def test_ofi_tracker_window_rolloff():
    """旧帧滚出窗口后 normalized 仅反映近期帧。"""
    tracker = OFITracker(window_ms=5_000)
    # 古老净空帧（远超窗口）
    tracker.update("BTC", [_lv(100.0, 1.0)], [_lv(102.0, 1.0)], ts=1000)
    for _ in range(3):
        tracker.update("BTC", [_lv(99.0, 10.0)], [_lv(102.0, 1.0)], ts=1001)  # 大量净空

    now = 1_000_000  # 远未来
    # 窗口内只有"近期净买"帧
    tracker.update("BTC", [_lv(100.0, 1.0)], [_lv(102.0, 1.0)], ts=now - 1000)
    for i in range(3):
        tracker.update("BTC", [_lv(101.0 + i, 5.0)], [_lv(102.0, 1.0)], ts=now - 900 + i * 100)
    norm = tracker.normalized("BTC", now_ms=now)
    # 旧净空帧已超出 5s 窗口，不应拉成负
    # 近期净买帧应使 norm > 0
    assert norm > 0.0, f"旧帧应滚出窗口，norm={norm}"


def test_ofi_tracker_empty_coin_returns_zero():
    """未见过的 coin → normalized=0。"""
    tracker = OFITracker()
    assert tracker.normalized("UNKNOWN", now_ms=99999) == 0.0
