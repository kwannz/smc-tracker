"""审计修复验证测试（合成数据，无网络）。

覆盖：
  - Fix 2: whale_pnl 写入往返——WhaleMomentum.snapshot → insert_whale_pnl → 可查询
  - Fix 3: _periodic_discover / _periodic_whale_pnl 方法存在 + 已在 gather 注册
  - Fix 4: review.evaluate_due 跳过计数（无价格时静默变日志）
  - Fix 1: work-first 任务 sleep 已移至末尾（方法结构验证）
"""
from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

# 确保 src 在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.monitor.whale_momentum import WhaleMomentum, pnl_rows_from
from smc_tracker.review import PredictionReview
from smc_tracker.storage import Store


# ---- 辅助：轻量 Store 存根（仅需 conn） ----
class _FakeStore:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)


def _real_store() -> Store:
    """真实 Store（临时目录），用于 whale_pnl 往返测试。"""
    d = tempfile.mkdtemp()
    return Store(Path(d) / "t.db")


# ======================================================
# Fix 2: whale_pnl 写入往返
# ======================================================

class TestWhalePnlRoundtrip:
    """WhaleMomentum.snapshot → store.insert_whale_pnl → whale_pnl_latest 可查询。"""

    def test_snapshot_writes_and_readable(self) -> None:
        """snapshot 后 whale_pnl_latest 应能读回相同地址的记录（表非空）。"""
        s = _real_store()
        wm = WhaleMomentum(s)
        now_ms = int(time.time() * 1000)
        rows = [("0xabc", "庄A", 100.0, 500.0, 1000.0, 5_000_000.0, 2_000_000.0)]
        wm.snapshot(rows, now_ms)
        result = s.whale_pnl_latest("0xabc")
        assert result is not None, "snapshot 后 whale_pnl_latest 不应返回 None（表永空 bug 已修）"
        assert result[0] == "0xabc"
        assert abs(result[1] - 5_000_000.0) < 1  # alltime_pnl
        assert abs(result[2] - 2_000_000.0) < 1  # account_value
        s.close()

    def test_snapshot_multiple_addresses(self) -> None:
        """多地址同批写入均可查回。"""
        s = _real_store()
        wm = WhaleMomentum(s)
        now_ms = int(time.time() * 1000)
        rows = [
            ("0xaaa", "庄A", 10.0, 50.0, 100.0, 1_000_000.0, 500_000.0),
            ("0xbbb", "庄B", 20.0, 60.0, 200.0, 2_000_000.0, 800_000.0),
        ]
        wm.snapshot(rows, now_ms)
        assert s.whale_pnl_latest("0xaaa") is not None
        assert s.whale_pnl_latest("0xbbb") is not None
        s.close()

    def test_pnl_rows_from_filters_small_accounts(self) -> None:
        """pnl_rows_from 应过滤净值低于 min_account 的行。"""
        raw = [
            {"ethAddress": "0xbig", "accountValue": "500000",
             "windowPerformances": [["day", {"pnl": "100"}], ["allTime", {"pnl": "5000000"}],
                                    ["week", {"pnl": "500"}], ["month", {"pnl": "1000"}]]},
            {"ethAddress": "0xsmall", "accountValue": "100",  # 低于 min_account
             "windowPerformances": [["day", {"pnl": "1"}], ["allTime", {"pnl": "1000"}],
                                    ["week", {"pnl": "1"}], ["month", {"pnl": "1"}]]},
        ]
        result = pnl_rows_from(raw, top_n=10, min_account=300_000.0)
        addrs = [r[0] for r in result]
        assert "0xbig" in addrs
        assert "0xsmall" not in addrs, "低净值地址应被过滤"


# ======================================================
# Fix 3: 新 _periodic_* 方法存在 + 已在 gather 注册
# ======================================================

class TestNewPeriodicMethods:
    """_periodic_whale_pnl 和 _periodic_discover 存在并注册到 gather 中。"""

    def _get_app_source(self) -> str:
        from smc_tracker import app as app_mod
        return Path(inspect.getfile(app_mod)).read_text(encoding="utf-8")

    def test_periodic_whale_pnl_method_exists(self) -> None:
        from smc_tracker.app import TradingSystem
        assert hasattr(TradingSystem, "_periodic_whale_pnl"), (
            "_periodic_whale_pnl 方法不存在（Fix 2 未实施）"
        )

    def test_periodic_discover_method_exists(self) -> None:
        from smc_tracker.app import TradingSystem
        assert hasattr(TradingSystem, "_periodic_discover"), (
            "_periodic_discover 方法不存在（Fix 3 未实施）"
        )

    def test_periodic_whale_pnl_registered_in_gather(self) -> None:
        src = self._get_app_source()
        assert "_periodic_whale_pnl()" in src, (
            "_periodic_whale_pnl() 未注册到 asyncio.gather（Fix 2 不生效）"
        )

    def test_periodic_discover_registered_in_gather(self) -> None:
        src = self._get_app_source()
        assert "_periodic_discover()" in src, (
            "_periodic_discover() 未注册到 asyncio.gather（Fix 3 不生效）"
        )

    def test_whale_momentum_attr_on_trading_system(self) -> None:
        """TradingSystem.__init__ 应构造 self.whale_momentum（WhaleMomentum 实例占位）。"""
        import smc_tracker.app as app_mod
        src = Path(inspect.getfile(app_mod)).read_text(encoding="utf-8")
        assert "self.whale_momentum = WhaleMomentum(store)" in src, (
            "TradingSystem.__init__ 缺少 self.whale_momentum 构造（Fix 2 占位缺失）"
        )


# ======================================================
# Fix 4: evaluate_due 跳过计数可见性
# ======================================================

class TestEvaluateDueSkipVisibility:
    """evaluate_due 跳过无价格条目时应产生日志而非静默。"""

    def test_skipped_count_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """无价格时 evaluate_due 返回 0 但日志体现跳过数目。"""
        import logging
        fs = _FakeStore()
        rev = PredictionReview(fs)
        now = int(time.time() * 1000)
        # 记录一条已到期预测（有效 HL 价）
        rev.record(ts=now - 7200_000, coin="NOPRICE", kind="SMC", direction="long",
                   hl_px=1.0, bg_px=0.0, horizon_ms=3600_000)
        # price_of 返回 None（无价格），应跳过
        with caplog.at_level(logging.INFO, logger="smc_tracker.review"):
            n = rev.evaluate_due(lambda c: None, now)
        assert n == 0, "无价格时 evaluated_count 应为 0"
        # 日志中应体现跳过计数
        skip_msgs = [r.message for r in caplog.records if "跳过" in r.message]
        assert skip_msgs, (
            "evaluate_due 跳过无价格条目时应产生 log.info（消除静默），"
            f"但 caplog 中无相关日志。records={[r.message for r in caplog.records]}"
        )

    def test_no_skip_log_when_price_available(self, caplog: pytest.LogCaptureFixture) -> None:
        """价格可用时不应产生跳过日志。"""
        import logging
        fs = _FakeStore()
        rev = PredictionReview(fs)
        now = int(time.time() * 1000)
        rev.record(ts=now - 7200_000, coin="BTC", kind="SMC", direction="long",
                   hl_px=50000.0, bg_px=0.0, horizon_ms=3600_000)
        with caplog.at_level(logging.INFO, logger="smc_tracker.review"):
            n = rev.evaluate_due(lambda c: 51000.0, now)
        assert n == 1, "价格可用时应评估 1 条"
        skip_msgs = [r.message for r in caplog.records if "跳过" in r.message]
        assert not skip_msgs, "有价格时不应产生跳过日志"

    def test_mixed_price_availability(self, caplog: pytest.LogCaptureFixture) -> None:
        """部分有价格、部分无价格：已评估计数 + 跳过日志均正确。"""
        import logging
        fs = _FakeStore()
        rev = PredictionReview(fs)
        now = int(time.time() * 1000)
        rev.record(ts=now - 7200_000, coin="BTC", kind="SMC", direction="long",
                   hl_px=50000.0, bg_px=0.0, horizon_ms=3600_000)
        rev.record(ts=now - 7200_000, coin="GHOST", kind="共识", direction="short",
                   hl_px=1.0, bg_px=0.0, horizon_ms=3600_000)

        def price_of(coin: str) -> float | None:
            return 51000.0 if coin == "BTC" else None

        with caplog.at_level(logging.INFO, logger="smc_tracker.review"):
            n = rev.evaluate_due(price_of, now)
        assert n == 1, "只有 BTC 有价格，应评估 1 条"
        skip_msgs = [r.message for r in caplog.records if "跳过" in r.message]
        assert skip_msgs, "GHOST 无价格应产生跳过日志"


# ======================================================
# Fix 1: work-first 结构验证（AST 检查关键方法 sleep 位置）
# ======================================================

class TestWorkFirstStructure:
    """通过 AST 解析验证关键 _periodic_* 的 while 循环内 sleep 在末尾（非开头）。

    检验方式：在目标方法的 while body 里，第一个语句不是 await asyncio.sleep(...)。
    """

    def _method_while_body(self, method_name: str) -> list[ast.stmt]:
        """返回指定方法 while body 的语句列表。"""
        import smc_tracker.app as app_mod
        src = Path(inspect.getfile(app_mod)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == method_name:
                for stmt in node.body:
                    if isinstance(stmt, ast.While):
                        return stmt.body
        return []

    def _first_stmt_is_sleep(self, body: list[ast.stmt]) -> bool:
        """判断 body 第一个语句是否是 `await asyncio.sleep(...)`。"""
        if not body:
            return False
        first = body[0]
        if not isinstance(first, ast.Expr):
            return False
        val = first.value
        if not isinstance(val, ast.Await):
            return False
        call = val.value
        if not isinstance(call, ast.Call):
            return False
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "sleep":
            return True
        return False

    @pytest.mark.parametrize("method_name", [
        "_periodic_hl_digest",
        "_periodic_review",
        "_periodic_efficacy",
        "_periodic_health",
        "_periodic_report",
        "_periodic_exchange_flow",
        "_periodic_wallet_portfolio",
        "_periodic_whale_pnl",
        "_periodic_discover",
    ])
    def test_no_sleep_at_start_of_while(self, method_name: str) -> None:
        body = self._method_while_body(method_name)
        assert body, f"{method_name} 找不到 while 循环体（方法不存在或结构异常）"
        assert not self._first_stmt_is_sleep(body), (
            f"{method_name} 的 while 循环首句仍是 await asyncio.sleep(...) "
            f"（sleep-first bug 未修，应改为 work-first）"
        )
