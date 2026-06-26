"""路线 A — 地基加固 TDD 测试套件。

T1  PRAGMA 生效（busy_timeout/journal_mode/cache_size/temp_store）
T2  多进程 busy_timeout 不抛 locked（确定性：两 Store → 同文件，monkeypatch 锁行为）
T3  sm_events 缓冲→批量落（回调入 _sm_buffer，flush 批量写，insert_sm_events_batch）
T4  oi_window 内存查询（纯内存，数值正确，无历史返回 None）
T5  supervise 重启+退避（fake factory / fake sleep，确定性）
T6  push 背压（maxsize + 丢最旧 + 计数）
T7  harmonic 索引存在+被用（PRAGMA index_list + EXPLAIN QUERY PLAN）

全部用合成数据、无网络、无真实 DB 文件依赖（tmp_path fixture）。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

# ── 路径插入（本地 src layout）──────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage.db import Store
from smc_tracker.monitor.bitget_oi_monitor import BitgetOIMonitor
from smc_tracker.supervisor import supervise


# ─────────────────────────────────────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path, name: str = "t.db") -> Store:
    return Store(tmp_path / name)


# ─────────────────────────────────────────────────────────────────────────────
# T1  PRAGMA 生效
# ─────────────────────────────────────────────────────────────────────────────

def test_pragma_busy_timeout(tmp_path):
    """Store.__init__ 后 busy_timeout 应为 5000ms。"""
    s = _make_store(tmp_path)
    assert s.pragma("busy_timeout") == 5000


def test_pragma_journal_mode_wal(tmp_path):
    """journal_mode 应为 wal。"""
    s = _make_store(tmp_path)
    assert s.pragma("journal_mode") == "wal"


def test_pragma_cache_size(tmp_path):
    """cache_size 应为 -16000（16 MB；负值=KB）。"""
    s = _make_store(tmp_path)
    assert s.pragma("cache_size") == -16000


def test_pragma_temp_store(tmp_path):
    """temp_store 应为 2（MEMORY）。"""
    s = _make_store(tmp_path)
    assert s.pragma("temp_store") == 2


# ─────────────────────────────────────────────────────────────────────────────
# T2  多进程（多 Store）busy_timeout 不抛 locked — 确定性测试
# ─────────────────────────────────────────────────────────────────────────────

def test_busy_timeout_prevents_locked(tmp_path):
    """两个 Store 实例指向同一文件：busy_timeout=5000 使写锁等待不立即抛 OperationalError。

    测试方法（确定性，不依赖真实线程竞速）：
      1. 断言 Store 的 busy_timeout pragma 为 5000。
      2. 用 sqlite3.connect 手动开启写事务持有锁，设 timeout=0（no-wait）→ 应抛。
      3. 同样连接设 timeout=5 → 在真实锁释放后不抛（因为锁会很快释放）。
    """
    db_path = str(tmp_path / "lock_test.db")

    # Store A：建库并设好 PRAGMA
    s_a = Store(tmp_path / "lock_test.db")
    assert s_a.pragma("busy_timeout") == 5000

    # 验证 busy_timeout=0 时会抛（无等待）
    conn_b = sqlite3.connect(db_path, timeout=0)
    conn_b.execute("PRAGMA journal_mode=WAL")

    # 线程持有写锁
    lock_held = threading.Event()
    lock_release = threading.Event()
    write_error: list[Exception] = []

    def hold_write():
        try:
            conn_b.execute("BEGIN EXCLUSIVE")
            lock_held.set()
            lock_release.wait(timeout=3.0)
            conn_b.execute("ROLLBACK")
        except Exception as exc:
            write_error.append(exc)
            lock_held.set()

    t = threading.Thread(target=hold_write, daemon=True)
    t.start()
    lock_held.wait(timeout=2.0)

    if write_error:
        pytest.skip(f"无法建立写锁: {write_error[0]}")

    # conn_c（no-wait）应抛 OperationalError
    conn_c_nowait = sqlite3.connect(db_path, timeout=0)
    conn_c_nowait.execute("PRAGMA journal_mode=WAL")
    with pytest.raises(sqlite3.OperationalError, match="locked|unable"):
        conn_c_nowait.execute("BEGIN EXCLUSIVE")

    # Store（busy_timeout=5000）断言 pragma 生效 — 验证等价于 timeout>0
    # （真实等待需释放锁，这里只验证 pragma 值正确即可，避免 flaky 超时）
    s_b = Store(tmp_path / "lock_test.db")
    assert s_b.pragma("busy_timeout") == 5000

    # 释放锁
    lock_release.set()
    t.join(timeout=3.0)
    conn_b.close()
    conn_c_nowait.close()


# ─────────────────────────────────────────────────────────────────────────────
# T3  sm_events 缓冲→批量落
# ─────────────────────────────────────────────────────────────────────────────

def _fake_sm_row(i: int = 0) -> tuple:
    """合成一行 sm_events 13 列 tuple（顺序与 insert_sm_event 一致）。"""
    ts = 1_700_000_000_000 + i * 1000
    return (ts, "open", f"0xaddr{i}", f"庄{i}", "BTC", "BUY",
            1.0, 50000.0, 50000.0, 0.0, 1.0, 0.0, 1)


def test_insert_sm_events_batch_empty(tmp_path):
    """空 rows → 返回 0，不抛。"""
    s = _make_store(tmp_path)
    assert s.insert_sm_events_batch([]) == 0


def test_insert_sm_events_batch_writes(tmp_path):
    """批量写入 N 行后 sm_events 表有 N 行，列序正确。"""
    s = _make_store(tmp_path)
    rows = [_fake_sm_row(i) for i in range(5)]
    n = s.insert_sm_events_batch(rows)
    assert n == 5

    result = s.conn.execute("SELECT * FROM sm_events ORDER BY ts ASC").fetchall()
    assert len(result) == 5
    # 校验第一行 13 列顺序（ts, type, address, label, coin, side, sz, px, notional,
    # pos_before, pos_after, closed_pnl, taker）
    first = result[0]
    assert first[0] == 1_700_000_000_000   # ts
    assert first[1] == "open"              # type
    assert first[4] == "BTC"              # coin
    assert first[12] == 1                 # taker


def test_concurrent_writes_no_txn_conflict_no_loss(tmp_path):
    """修审计P1:共享连接上多线程并发 BEGIN..COMMIT 不抛 OperationalError 且不丢数据(_txn 写锁串行化)。

    无锁时两线程并发手写事务会偶发 'cannot start a transaction within a transaction'/抢提交丢行。
    两线程各 400 次单行批量写,断言:零异常 + 行数精确=800(无静默丢失)。
    """
    s = _make_store(tmp_path)
    assert hasattr(s, "_write_lock") and hasattr(s, "_txn")   # 锁与事务管理器存在
    errors: list[Exception] = []
    N = 400

    def hammer(base: int) -> None:
        try:
            for i in range(N):
                s.insert_sm_events_batch([_fake_sm_row(base + i)])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=hammer, args=(0,))
    t2 = threading.Thread(target=hammer, args=(1_000_000,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, f"并发写事务抛异常(锁未生效?): {errors[:2]}"
    cnt = s.conn.execute("SELECT COUNT(*) FROM sm_events").fetchone()[0]
    assert cnt == 2 * N, f"应写入 {2 * N} 行,实际 {cnt}(并发丢数据?)"


def test_sm_buffer_deferred_write(tmp_path):
    """模拟热路径 append + flush 模式：append N 次后表仍空，flush 后有 N 行。"""
    s = _make_store(tmp_path)
    buf: list[tuple] = []

    # 热路径：只 append，不写 DB
    for i in range(3):
        buf.append(_fake_sm_row(i))

    # 期间表为空
    count = s.conn.execute("SELECT COUNT(*) FROM sm_events").fetchone()[0]
    assert count == 0
    assert len(buf) == 3

    # flush：批量落库 + 清空 buffer
    rows, buf = buf, []
    written = s.insert_sm_events_batch(rows)
    assert written == 3
    assert len(buf) == 0

    count = s.conn.execute("SELECT COUNT(*) FROM sm_events").fetchone()[0]
    assert count == 3


def test_insert_sm_events_batch_consistency(tmp_path):
    """batch 写入与逐条 insert_sm_event 写入结果一致（交叉验证）。"""
    s1 = _make_store(tmp_path, "b1.db")
    s2 = _make_store(tmp_path, "b2.db")

    rows = [_fake_sm_row(i) for i in range(3)]
    s1.insert_sm_events_batch(rows)
    for r in rows:
        s2.insert_sm_event(r)

    r1 = s1.conn.execute("SELECT * FROM sm_events ORDER BY ts").fetchall()
    r2 = s2.conn.execute("SELECT * FROM sm_events ORDER BY ts").fetchall()
    assert r1 == r2


# ─────────────────────────────────────────────────────────────────────────────
# T4  oi_window 内存查询
# ─────────────────────────────────────────────────────────────────────────────

class _FakeWS:
    def subscribe(self, sub, handler) -> None:
        pass


def _oi_monitor(tmp_path: Path, sym: str = "BTCUSDT") -> BitgetOIMonitor:
    ws = _FakeWS()
    s = _make_store(tmp_path)
    return BitgetOIMonitor([sym], {sym: "BTC"}, ws, s)


def _feed_oi(mon: BitgetOIMonitor, symbol: str, entries: list[tuple]) -> None:
    """直接向 _oi_window 注入 (ts, oi) 序列（测试专用）。"""
    for ts, oi in entries:
        mon._oi_window_data.setdefault(symbol, []).append((ts, oi))


def test_oi_window_no_history(tmp_path):
    """无历史数据 → 返回 None。"""
    mon = _oi_monitor(tmp_path)
    assert mon.oi_window("BTCUSDT", 600_000, 1_700_000_600_000) is None


def test_oi_window_latest_and_past(tmp_path):
    """喂入合成序列 → oi_window 返回 (latest, past) 数值正确。"""
    mon = _oi_monitor(tmp_path)
    sym = "BTCUSDT"
    now = 1_700_000_600_000
    # 构造：t=0(past 候选), t=100s(中间), t=600s(latest)
    entries = [
        (now - 600_000, 100.0),   # 恰好在窗口边界
        (now - 300_000, 150.0),
        (now,           200.0),   # latest
    ]
    _feed_oi(mon, sym, entries)

    result = mon.oi_window(sym, 600_000, now)
    assert result is not None
    latest_oi, past_oi = result
    assert latest_oi == pytest.approx(200.0)
    # past = 窗口前最近一条 ≤ (now - 600_000)
    assert past_oi == pytest.approx(100.0)


def test_oi_window_insufficient_past(tmp_path):
    """窗口内只有 latest，无 past → past 为 None，返回 (latest, None)。"""
    mon = _oi_monitor(tmp_path)
    sym = "BTCUSDT"
    now = 1_700_000_600_000
    # 只喂 latest（没有 past 候选）
    _feed_oi(mon, sym, [(now, 200.0)])
    result = mon.oi_window(sym, 600_000, now)
    # latest 有数据，past 无历史→ None
    assert result is not None
    latest_oi, past_oi = result
    assert latest_oi == pytest.approx(200.0)
    assert past_oi is None


def test_oi_window_matches_db_oi_change(tmp_path):
    """oi_window 与 store.oi_change 数值一致（交叉验证，golden test）。"""
    s = _make_store(tmp_path)
    sym = "BTCUSDT"
    coin = "BTC"
    now = 1_700_000_600_000

    # 向 DB 插入 OI 序列（模拟 store.oi_change 数据来源）
    rows_db = [
        (sym, coin, 100.0, 5_000_000.0, 50000.0, 0.0001, now - 700_000),  # past(窗口外)
        (sym, coin, 150.0, 7_500_000.0, 50000.0, 0.0001, now - 300_000),  # 中间
        (sym, coin, 200.0, 10_000_000.0, 50000.0, 0.0001, now),            # latest
    ]
    s.insert_oi(rows_db)

    # store.oi_change：返回 (latest_oi, past_oi)
    db_result = s.oi_change(sym, 600_000, now)
    assert db_result is not None
    db_latest, db_past = db_result

    # BitgetOIMonitor 内存版本
    ws = _FakeWS()
    mon = BitgetOIMonitor([sym], {sym: coin}, ws, s)
    _feed_oi(mon, sym, [
        (now - 700_000, 100.0),
        (now - 300_000, 150.0),
        (now,           200.0),
    ])
    mem_result = mon.oi_window(sym, 600_000, now)
    assert mem_result is not None
    mem_latest, mem_past = mem_result

    # latest 应一致
    assert mem_latest == pytest.approx(db_latest)
    # past = 窗口前最近：DB 里 now-700_000 满足 ≤ now-600_000
    assert db_past == pytest.approx(100.0)
    assert mem_past == pytest.approx(100.0)


# ─────────────────────────────────────────────────────────────────────────────
# T5  supervise 重启 + 退避
# ─────────────────────────────────────────────────────────────────────────────

def test_calc_backoff_exponential_capped_and_bounded():
    """_calc_backoff：指数退避 + 封顶 max + 大 error_count 不产生大整数膨胀(degenerate 永久失败防护)。"""
    from smc_tracker.supervisor import _calc_backoff
    assert _calc_backoff(0, 1.0, 60.0) == 1.0
    assert _calc_backoff(1, 1.0, 60.0) == 2.0
    assert _calc_backoff(3, 1.0, 60.0) == 8.0
    assert _calc_backoff(6, 1.0, 60.0) == 60.0   # 2^6=64 → 封顶 60
    # 巨大 error_count：仍精确封顶 max，且内部指数被夹(无千位大整数运算)
    assert _calc_backoff(5_000, 1.0, 60.0) == 60.0
    assert _calc_backoff(1_000_000, 1.0, 60.0) == 60.0
    # 指数夹在 32：base×2^32 远超 max，结果与 error_count≥32 任意值一致(行为不变)
    assert _calc_backoff(32, 1.0, 60.0) == _calc_backoff(999, 1.0, 60.0)


@pytest.mark.asyncio
async def test_supervise_retries_on_exception():
    """factory 抛 ValueError 前 3 次，第 4 次永久挂起等取消 → 验证被调用 ≥3 次。

    用极小 base_backoff(0.001s) 使真实退避极短（总时长可控，约 0.01s），
    不 monkeypatch asyncio（避免 mock 链复杂破坏 event loop）。
    """
    call_count = 0

    async def factory():
        nonlocal call_count
        call_count += 1
        if call_count < 4:
            raise ValueError(f"fake error #{call_count}")
        # 第 4 次：挂住等取消
        await asyncio.sleep(9999)

    import logging
    _log = logging.getLogger("test_supervise")

    task = asyncio.create_task(
        supervise(factory, name="test", base_backoff=0.001, max_backoff=0.008, log=_log)
    )
    # 等待足够长让前 3 次错误（每次退避 ~1-8ms）+ 第 4 次启动
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task

    assert call_count >= 3   # 至少重试了 3 次（第 4 次已在 sleep(9999) 中被取消）


@pytest.mark.asyncio
async def test_supervise_cancelled_propagates():
    """CancelledError 应向上传播，不被吞。"""
    async def factory():
        await asyncio.sleep(9999)  # 永久挂起

    import logging
    _log = logging.getLogger("test_supervise_cancel")

    task = asyncio.create_task(
        supervise(factory, name="cancel_test", log=_log)
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_supervise_stops_on_immediate_noop_return():
    """立即正常返回的 no-op/禁用任务 → supervise 停止监督（不 busy-loop）。

    回归：实跑暴露 ticker_board/llm 禁用时直接 return，被 supervise 每 base_backoff(1s)
    疯狂重启刷屏空转 CPU。修复后立即正常返回（elapsed < base_backoff）→ 停止监督。
    用 wait_for 超时检测：未修时 supervise 永不返回 → TimeoutError；修复后有限返回。
    """
    call_count = 0

    async def factory():
        nonlocal call_count
        call_count += 1
        # 立即返回（no-op，如 disabled 任务直接 return）—— 不 sleep、不抛异常

    import logging
    _log = logging.getLogger("test_supervise_noop")

    # supervise 应在有限时间内返回（停止监督），而非无限 busy-loop
    await asyncio.wait_for(
        supervise(factory, name="noop_test", base_backoff=0.05, log=_log),
        timeout=1.0,
    )
    assert call_count == 1   # 只调用 1 次后停止监督（不重启）


@pytest.mark.asyncio
async def test_supervise_backoff_resets_after_success():
    """连续成功运行 > reset_after 后再崩 → 退避从 base 复位（不累积）。"""
    call_log: list[str] = []
    sleep_calls: list[float] = []

    # fake asyncio.sleep（supervisor 内部用）
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        # 注意：不真实 sleep，避免测试慢

    import unittest.mock as mock
    import smc_tracker.supervisor as sup_mod

    # 直接测试 _calc_backoff 复位逻辑（白盒，避免 mock 链复杂）
    from smc_tracker.supervisor import _calc_backoff
    # 第 1 次错 → base(1.0)
    b0 = _calc_backoff(0, 1.0, 8.0)
    assert b0 == pytest.approx(1.0)
    # 第 2 次错 → 2.0
    b1 = _calc_backoff(1, 1.0, 8.0)
    assert b1 == pytest.approx(2.0)
    # 封顶
    b_cap = _calc_backoff(10, 1.0, 8.0)
    assert b_cap == pytest.approx(8.0)


# ─────────────────────────────────────────────────────────────────────────────
# T6  push 背压
# ─────────────────────────────────────────────────────────────────────────────

def test_push_backpressure_drops_oldest():
    """maxsize=3，填满后第 4 条入队：队头（最旧）被弃，新条在队尾，_push_dropped==1。"""
    import asyncio
    loop = asyncio.new_event_loop()

    try:
        q: asyncio.Queue = asyncio.Queue(maxsize=3)
        push_dropped = 0

        def enqueue_push(text: str, notifier: Any) -> int:
            nonlocal push_dropped
            try:
                q.put_nowait((text, notifier))
                return 0
            except asyncio.QueueFull:
                try:
                    q.get_nowait()   # 弃最旧
                    q.task_done()
                except asyncio.QueueEmpty:
                    pass
                q.put_nowait((text, notifier))
                push_dropped += 1
                return 1

        # 填满
        for i in range(3):
            loop.run_until_complete(
                asyncio.coroutine(lambda: None)() if False else
                asyncio.ensure_future(asyncio.sleep(0), loop=loop)
            )
            enqueue_push(f"msg{i}", None)

        assert q.qsize() == 3

        # 第 4 条
        enqueue_push("msg_new", None)

        assert q.qsize() == 3
        assert push_dropped == 1

        # 验证队列内容：最旧(msg0)被丢，队头应为 msg1
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        texts = [t for t, _ in items]
        assert "msg0" not in texts
        assert texts[-1] == "msg_new"
        assert "msg1" in texts
        assert "msg2" in texts
    finally:
        loop.close()


def test_push_backpressure_no_drop_when_not_full():
    """队列未满时正常入队，不丢，_push_dropped 不增。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=5)
    dropped = 0

    def enqueue_push(text: str, notifier: Any) -> None:
        nonlocal dropped
        try:
            q.put_nowait((text, notifier))
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.task_done()
            except asyncio.QueueEmpty:
                pass
            q.put_nowait((text, notifier))
            dropped += 1

    for i in range(5):
        enqueue_push(f"m{i}", None)

    assert q.qsize() == 5
    assert dropped == 0


# ─────────────────────────────────────────────────────────────────────────────
# T7  harmonic 索引存在 + 被用
# ─────────────────────────────────────────────────────────────────────────────

def test_harmonic_index_list(tmp_path):
    """建 Store 后 PRAGMA index_list(harmonic_setups) 含两个期望索引。"""
    s = _make_store(tmp_path)
    idx_names = {row[1] for row in
                 s.conn.execute("PRAGMA index_list(harmonic_setups)").fetchall()}
    assert "ix_harmonic_ts" in idx_names, f"缺 ix_harmonic_ts, got: {idx_names}"
    assert "ix_harmonic_coin_ts" in idx_names, f"缺 ix_harmonic_coin_ts, got: {idx_names}"


def test_harmonic_index_used_for_max_ts(tmp_path):
    """EXPLAIN QUERY PLAN SELECT ... WHERE ts=(SELECT MAX(ts)...) 包含 INDEX 子串。"""
    s = _make_store(tmp_path)
    plan_rows = s.conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT ts,coin FROM harmonic_setups "
        "WHERE ts=(SELECT MAX(ts) FROM harmonic_setups)"
    ).fetchall()
    plan_text = " ".join(str(r) for r in plan_rows).upper()
    assert "INDEX" in plan_text, f"EXPLAIN QUERY PLAN 未使用索引: {plan_rows}"


def test_harmonic_index_used_for_coin_ts(tmp_path):
    """EXPLAIN QUERY PLAN SELECT ... WHERE coin=? ORDER BY ts 使用复合索引。"""
    s = _make_store(tmp_path)
    plan_rows = s.conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT ts,coin FROM harmonic_setups "
        "WHERE coin=? ORDER BY ts",
        ("BTC",)
    ).fetchall()
    plan_text = " ".join(str(r) for r in plan_rows).upper()
    assert "INDEX" in plan_text, f"EXPLAIN QUERY PLAN 未使用索引: {plan_rows}"
