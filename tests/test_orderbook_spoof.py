"""C.5 orderbook spoof 过滤单测（存活时间 + build/pull flap 计数）。

断言要点：
- 瞬现瞬撤（存活 < min_lifetime_ms）→ 不 emit build 信号
- 同 px 在 flap_window 内 build+pull ≥ max_flap → event["spoof"]=True
- 真实墙（持续 ≥ min_lifetime_ms 多帧）→ 正常 emit build
- book_intent 不把 spoof 墙计入（confirming_wall 跳过）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.orderbook_monitor import HLOrderbookMonitor


class _FakeWS:
    def subscribe(self, sub, handler) -> None:
        pass


def _lv(px: float, sz: float, n: int = 1) -> dict:
    return {"px": str(px), "sz": str(sz), "n": n}


def _frame(coin: str, bids: list[dict], asks: list[dict], t: int) -> dict:
    return {"coin": coin, "time": t, "levels": [bids, asks]}


def _make_mon(min_lifetime_ms: int = 3000, max_flap: int = 4,
              flap_window_ms: int = 30_000) -> tuple[HLOrderbookMonitor, list[dict]]:
    events: list[dict] = []
    ws = _FakeWS()
    mon = HLOrderbookMonitor(
        ["BTC"], ws, store=None,
        on_wall_signal=events.append,
        min_wall_usd=200_000.0,
        min_lifetime_ms=min_lifetime_ms,
        max_flap=max_flap,
        flap_window_ms=flap_window_ms,
    )
    return mon, events


def _big_bid_frame(ts: int) -> dict:
    """构造含一个大 bid 墙（600k）的帧。"""
    small_bids = [_lv(60000.0 - i, 0.001) for i in range(1, 19)]
    big_bid = _lv(60000.0, 10.0, n=5)  # notional=600k
    asks = [_lv(60200.0 + i, 0.001) for i in range(19)]
    return _frame("BTC", [big_bid] + small_bids, asks, ts)


def _no_wall_frame(ts: int) -> dict:
    """构造无大墙的帧（原墙位置变成极小档）。"""
    bids = [_lv(60000.0 - i, 0.001) for i in range(19)]  # 全部极小
    asks = [_lv(60200.0 + i, 0.001) for i in range(19)]
    return _frame("BTC", bids, asks, ts)


# ──────────────────── 存活时间过滤 ────────────────────

def test_instant_wall_not_emitted():
    """瞬现瞬撤（仅1帧）→ 不 emit build 事件（spoof 过滤）。"""
    mon, events = _make_mon(min_lifetime_ms=3000)
    # 第1帧：大墙出现（记录 born）
    mon._on_l2book(_big_bid_frame(1000), 0)
    builds = [e for e in events if e["kind"] == "build"]
    assert len(builds) == 0, f"瞬现不应 emit build: {builds}"
    # 第2帧 t=2000（存活1s < 3s）：墙消失 → pull，但 build 仍未 emit
    mon._on_l2book(_no_wall_frame(2000), 0)
    builds_after = [e for e in events if e["kind"] == "build"]
    assert len(builds_after) == 0, f"存活不足时消失不应 emit build: {builds_after}"


def test_persistent_wall_emitted():
    """真实墙（持续存活 ≥ min_lifetime_ms）→ 正常 emit build。"""
    mon, events = _make_mon(min_lifetime_ms=3000)
    # 第1帧 t=1000
    mon._on_l2book(_big_bid_frame(1000), 0)
    assert len([e for e in events if e["kind"] == "build"]) == 0

    # 第2帧 t=5000（存活4s ≥ 3s）
    mon._on_l2book(_big_bid_frame(5000), 0)
    builds = [e for e in events if e["kind"] == "build"]
    assert len(builds) == 1, f"存活≥3s应 emit build: {events}"
    assert builds[0]["px"] == 60000.0
    assert builds[0]["spoof"] is False, "真实墙 spoof=False"


def test_build_emitted_only_once():
    """存活确认后第3帧继续存在 → 不重复 emit build。"""
    mon, events = _make_mon(min_lifetime_ms=3000)
    mon._on_l2book(_big_bid_frame(1000), 0)
    mon._on_l2book(_big_bid_frame(5000), 0)  # emit build (第1次)
    events.clear()
    mon._on_l2book(_big_bid_frame(9000), 0)  # 继续存在，不再 emit
    builds = [e for e in events if e["kind"] == "build"]
    assert len(builds) == 0, f"不应重复 emit build: {builds}"


# ──────────────────── Flap 计数 / spoof 标记 ────────────────────

def _cycle_wall(mon: HLOrderbookMonitor, base_ts: int, cycle_ms: int, n: int) -> None:
    """交替 build/pull 以制造 flap 事件。"""
    for i in range(n):
        ts = base_ts + i * cycle_ms
        # build: 大墙出现
        mon._on_l2book(_big_bid_frame(ts), 0)
        # pull: 大墙消失（很快）
        mon._on_l2book(_no_wall_frame(ts + cycle_ms // 2), 0)


def test_frequent_flap_marks_spoof():
    """同 px 在 flap_window 内 build+pull ≥ max_flap(4) → 标记 spoof。"""
    mon, events = _make_mon(min_lifetime_ms=0, max_flap=4, flap_window_ms=30_000)
    # 在 30s 内 build/pull 5 次（≥ max_flap=4）
    _cycle_wall(mon, base_ts=1000, cycle_ms=3000, n=5)
    # 检查 spoof 标记
    q = mon.wall_quality("BTC", "bid", 60000.0)
    assert q["spoof"] is True, f"频繁 flap 应标记 spoof: {q}"


def test_spoof_event_flag_in_callback():
    """spoof 标记的 pull 事件 callback 中包含 spoof=True。"""
    mon, events = _make_mon(min_lifetime_ms=0, max_flap=4, flap_window_ms=30_000)
    _cycle_wall(mon, base_ts=1000, cycle_ms=3000, n=5)
    spoof_events = [e for e in events if e.get("spoof") is True]
    assert len(spoof_events) > 0, f"应有 spoof=True 事件: {events}"


def test_few_flaps_not_spoof():
    """少量 build/pull（< max_flap）→ 不标记 spoof。"""
    mon, events = _make_mon(min_lifetime_ms=0, max_flap=4, flap_window_ms=30_000)
    # 仅 2 次 cycle（4 个事件，但 build+pull 事件各 2，总 flap=4 刚好 = max_flap 触发）
    # 使用 n=1（仅 build→ 仅 2 个 flap 事件）
    mon._on_l2book(_big_bid_frame(1000), 0)  # build flap
    # 不 pull，仅 1 个 build 事件
    q = mon.wall_quality("BTC", "bid", 60000.0)
    assert q["spoof"] is False, f"1次 build 不应标记 spoof: {q}"


# ──────────────────── confirming_wall spoof 过滤 ────────────────────

def test_confirming_wall_skips_spoof():
    """confirming_wall 跳过 spoof 标记墙。"""
    mon, events = _make_mon(min_lifetime_ms=0, max_flap=4, flap_window_ms=30_000)
    # 制造 spoof（5次 cycle）
    _cycle_wall(mon, base_ts=1000, cycle_ms=3000, n=5)
    # confirming_wall 不应返回该 spoof 墙
    result = mon.confirming_wall("BTC", price=60000.0, side="bid", tol_pct=0.05)
    assert result is None, f"confirming_wall 应跳过 spoof 墙: {result}"


def test_confirming_wall_returns_real_wall():
    """非 spoof 真实墙（存活 ≥ min_lifetime_ms，无 flap）→ confirming_wall 返回。"""
    mon, events = _make_mon(min_lifetime_ms=0, max_flap=10, flap_window_ms=30_000)
    # 注入一个墙（min_lifetime_ms=0 → 无需等待，直接 emit）
    mon._on_l2book(_big_bid_frame(1000), 0)
    # 第2帧让状态稳定（墙持续存在）
    mon._on_l2book(_big_bid_frame(5000), 0)
    # 应能找到该墙
    result = mon.confirming_wall("BTC", price=60000.0, side="bid", tol_pct=0.02)
    assert result is not None, f"真实墙应被 confirming_wall 找到"
    assert result["px"] == 60000.0


# ──────────────────── book_intent 不含 spoof ────────────────────

def test_book_intent_available_after_ws_frame():
    """收到 WS 帧后 book_intent 返回 float（非 None）。"""
    mon, events = _make_mon(min_lifetime_ms=0)
    mon._on_l2book(_big_bid_frame(1000), 0)
    result = mon.book_intent("BTC", now_ms=1000 + 60_000)
    assert result is not None and isinstance(result, float), (
        f"book_intent 应返回 float，got {result}"
    )


def test_book_intent_none_for_unknown_coin():
    """未收到 WS 帧的 coin → book_intent 返回 None（调用方降级）。"""
    mon, events = _make_mon()
    assert mon.book_intent("UNKNOWN", now_ms=99999) is None


# ──────────────────── confirming_wall 存活时间检查 ────────────────────

def test_confirming_wall_rejects_newborn_wall():
    """confirming_wall 不返回存活时间 < min_lifetime_ms 的墙（名实一致）。

    TDD：此测试先于实现存在，初始应 FAIL（当前实现未检查 born_ts）。
    """
    mon, events = _make_mon(min_lifetime_ms=3000)
    # 第1帧 t=1000：大墙刚出现，_wall_born 记录 born_ts=1000，存活=0ms < 3000ms
    mon._on_l2book(_big_bid_frame(1000), 0)
    # 刚出现的墙不应被 confirming_wall 返回（未满足存活要求）
    result = mon.confirming_wall("BTC", price=60000.0, side="bid", tol_pct=0.02)
    assert result is None, (
        f"confirming_wall 不应返回存活 < min_lifetime_ms 的墙: {result}"
    )


def test_confirming_wall_returns_after_sufficient_lifetime():
    """confirming_wall 在墙存活 >= min_lifetime_ms 后才返回它（build 已 emit）。

    TDD：需配合实现修复才能通过。
    """
    mon, events = _make_mon(min_lifetime_ms=3000)
    # 第1帧 t=1000：记录 born（存活 0ms，未确认）
    mon._on_l2book(_big_bid_frame(1000), 0)
    assert mon.confirming_wall("BTC", price=60000.0, side="bid", tol_pct=0.02) is None

    # 第2帧 t=5000（存活 4000ms >= 3000ms）：build 已确认 emit → 应返回
    mon._on_l2book(_big_bid_frame(5000), 0)
    result = mon.confirming_wall("BTC", price=60000.0, side="bid", tol_pct=0.02)
    assert result is not None, (
        "存活 >= min_lifetime_ms 后 confirming_wall 应返回该墙"
    )
    assert result["px"] == 60000.0


def test_confirming_wall_rejects_wall_disappeared_before_maturity():
    """墙在未满 min_lifetime_ms 前消失后重新出现，不应立即被 confirming_wall 返回。

    重现场景：同一 px 在 born 后快速 pull 再 build → born_ts 重置 → 仍需等待。
    """
    mon, events = _make_mon(min_lifetime_ms=5000)
    # t=1000：墙出现（born=1000）
    mon._on_l2book(_big_bid_frame(1000), 0)
    # t=2000（存活1s < 5s）：墙消失（pull，born 清除）
    mon._on_l2book(_no_wall_frame(2000), 0)
    # t=2500：墙重新出现（born 重置为 2500）
    mon._on_l2book(_big_bid_frame(2500), 0)
    # t=4000（距第2次 born 仅1.5s < 5s）：仍不应返回
    mon._on_l2book(_big_bid_frame(4000), 0)
    result = mon.confirming_wall("BTC", price=60000.0, side="bid", tol_pct=0.02)
    assert result is None, (
        f"重生墙存活不足 min_lifetime_ms，confirming_wall 不应返回: {result}"
    )
