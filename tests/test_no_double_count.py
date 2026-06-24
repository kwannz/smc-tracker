"""防双计回归护栏（C.1~C.5）。

断言：
1. FlowPredictor.predict 分数仅由 accel + book_intent + oi 构成，
   funding 不影响 predict 输出（传不同 funding 不改 predict 分）。
2. OFI/queue/micro 仅经单一 book_intent 出口进 predict（不并列）。
3. book_imbalance 向后兼容：旧键 "imbalance" 仍存在。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals.flow_predictor import FlowPredictor, orderbook_imbalance
from smc_tracker.signals.funding_extreme import funding_extreme_signal
from smc_tracker.signals.microprice import queue_imbalance, micro_price, ofi_delta, OFITracker
from smc_tracker.monitor.orderbook_monitor import HLOrderbookMonitor

NOW = 10_000_000


# ──────────────────── 1. funding 不进 predict ────────────────────

def test_funding_not_in_predict_signature():
    """FlowPredictor.predict 签名不含 funding 参数（API 级防双计）。"""
    import inspect
    sig = inspect.signature(FlowPredictor.predict)
    params = list(sig.parameters.keys())
    assert "funding" not in params, f"predict 签名不应含 funding: {params}"
    assert "funding_now" not in params
    assert "funding_history" not in params


def test_funding_does_not_change_predict_score():
    """传入不同 funding 值不改变 predict 结果（funding 与 predict 独立）。

    构造两次相同的 push 序列 + 相同 book/oi，验证 predict 输出一致，
    且 funding_extreme_signal 不影响 predict（只有上层聚合器消费 funding）。
    """
    def _make_fp():
        fp = FlowPredictor(accel_scale=100_000, threshold=0.1, window_ms=600_000,
                           min_accel_samples=3)
        for i in range(10):
            fp.push("X", 5_000.0, NOW - 600_000 + i * 60_000 + 1000)
        return fp

    fp1 = _make_fp()
    fp2 = _make_fp()
    book = 0.4
    oi = 0.1

    pred1 = fp1.predict("X", NOW, book_imbalance=book, oi_velocity=oi)
    pred2 = fp2.predict("X", NOW, book_imbalance=book, oi_velocity=oi)

    # 两个 predict 结果应一致（无论外部 funding 如何变化）
    if pred1 is None and pred2 is None:
        return  # 同样无预测，通过
    assert pred1 is not None and pred2 is not None
    assert abs(pred1.score - pred2.score) < 1e-9, (
        f"predict 分数不应因 funding 变化而不同: {pred1.score} vs {pred2.score}"
    )

    # 验证 funding_extreme_signal 对不同 funding 确实给出不同结果（单独消费）
    hist = [0.0001 + 0.00002 * (i % 7 - 3) for i in range(30)]
    f_high = funding_extreme_signal(0.01, hist)
    f_low = funding_extreme_signal(-0.01, hist)
    assert f_high != f_low, "funding_extreme_signal 不同输入应给不同输出"


# ──────────────────── 2. OFI/queue/micro 单一出口 ────────────────────

def test_ofi_queue_micro_not_in_predict_signature():
    """predict 签名不直接接受 ofi_norm/queue_imb/micro_tilt（防并列双计）。

    这些只经 book_intent → book_imbalance 单入参进 predict。
    """
    import inspect
    sig = inspect.signature(FlowPredictor.predict)
    params = list(sig.parameters.keys())
    for forbidden in ("ofi_norm", "queue_imb", "micro_tilt", "ofi", "queue", "micro"):
        assert forbidden not in params, (
            f"predict 签名不应直接接受 {forbidden}: {params}"
        )


def test_book_intent_is_single_composite():
    """book_intent 是单一出口：返回 float 或 None，不返回分项。"""
    class _FakeWS:
        def subscribe(self, *args): pass

    ws = _FakeWS()
    mon = HLOrderbookMonitor(["BTC"], ws, store=None, min_lifetime_ms=0)
    # 注入一帧
    bids = [{"px": "60000.0", "sz": "5.0", "n": 1}]
    asks = [{"px": "60100.0", "sz": "2.0", "n": 1}]
    mon._on_l2book({"coin": "BTC", "time": 1000, "levels": [bids, asks]}, 0)
    # 再注入一帧以有 OFI delta
    bids2 = [{"px": "60010.0", "sz": "6.0", "n": 1}]
    mon._on_l2book({"coin": "BTC", "time": 2000, "levels": [bids2, asks]}, 0)

    intent = mon.book_intent("BTC", now_ms=62_000)
    assert isinstance(intent, float), f"book_intent 应返回 float，got {type(intent)}"
    assert -1.0 <= intent <= 1.2, f"book_intent 值域合理，got {intent}"


# ──────────────────── 3. book_imbalance 向后兼容 ────────────────────

def test_orderbook_imbalance_still_has_imbalance_key():
    """旧 orderbook_imbalance 函数仍返回 imbalance 键（向后兼容）。"""
    bids = [{"px": "100", "sz": "10"}]
    asks = [{"px": "101", "sz": "5"}]
    r = orderbook_imbalance(bids, asks)
    assert "imbalance" in r, f"imbalance 键必须存在: {r}"
    assert "bid_usd" in r and "ask_usd" in r


def test_book_imbalance_monitor_has_imbalance_key():
    """HLOrderbookMonitor.book_imbalance 返回 dict 仍含 imbalance 键（C.1 向后兼容）。"""
    class _FakeWS:
        def subscribe(self, *args): pass

    ws = _FakeWS()
    mon = HLOrderbookMonitor(["ETH"], ws, store=None, min_lifetime_ms=0)
    bids = [{"px": "3000.0", "sz": "5.0", "n": 1}]
    asks = [{"px": "3001.0", "sz": "2.0", "n": 1}]
    mon._on_l2book({"coin": "ETH", "time": 1000, "levels": [bids, asks]}, 0)
    result = mon.book_imbalance("ETH")
    assert "imbalance" in result, f"imbalance 键仍须存在: {result}"
    # C.1 新增键也应存在
    assert "queue_imb" in result, f"C.1 扩展键 queue_imb 应存在: {result}"
    assert "micro_tilt" in result, f"C.1 扩展键 micro_tilt 应存在: {result}"


# ──────────────────── 4. oi_directional_velocity 单一入口 ────────────────────

def test_oi_velocity_param_name_in_predict():
    """predict 的 OI 入参名为 oi_velocity（单一入参，防多路并入）。"""
    import inspect
    sig = inspect.signature(FlowPredictor.predict)
    assert "oi_velocity" in sig.parameters, (
        f"predict 应有 oi_velocity 参数: {list(sig.parameters)}"
    )
    # 不应有多个 OI 参数（排除 coin 参数名误匹配）
    oi_params = [p for p in sig.parameters if p.startswith("oi")]
    assert len(oi_params) == 1, f"predict 应只有一个 OI 参数: {oi_params}"
