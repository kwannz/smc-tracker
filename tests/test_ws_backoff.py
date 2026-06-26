"""WS 重连退避纯函数单测：防重连风暴(连接成功即重置 → 稳定才重置)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.hyperliquid.ws_client import _reconnect_backoff, _STABLE_CONN_SEC


def test_stable_connection_resets_backoff():
    """连接存活 ≥ stable_sec(稳定) → 退避重置为 1.0(下次快速重连)。"""
    sleep_sec, next_backoff = _reconnect_backoff(
        conn_elapsed_sec=300.0, current_backoff=16.0, max_backoff=30.0)
    assert sleep_sec == 1.0, "稳定连接后应以 1.0 起退避"
    assert next_backoff == 2.0


def test_immediate_drop_grows_backoff_no_storm():
    """连接成功即断(< stable_sec) → 退避**继续指数增长**,不重置(防重连风暴)。"""
    backoff = 1.0
    sleeps = []
    for _ in range(5):
        s, backoff = _reconnect_backoff(
            conn_elapsed_sec=0.05, current_backoff=backoff, max_backoff=30.0)
        sleeps.append(s)
    # 退避必须单调增长(1,2,4,8,16),而非卡在 1.0 形成 1s 风暴
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0], f"应指数增长防风暴，实得 {sleeps}"


def test_backoff_capped_at_max():
    """退避不超过 max_backoff。"""
    s, nxt = _reconnect_backoff(conn_elapsed_sec=0.0, current_backoff=30.0, max_backoff=30.0)
    assert nxt == 30.0 and s == 30.0


def test_stable_threshold_boundary():
    """恰好 stable_sec 边界 → 视为稳定(>=)。"""
    s, _ = _reconnect_backoff(
        conn_elapsed_sec=_STABLE_CONN_SEC, current_backoff=8.0, max_backoff=30.0)
    assert s == 1.0
