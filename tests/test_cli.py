"""CLI 子命令解析与 dispatch 单元测试（无网络，无真实 DB 连接）。

覆盖：
1. argparse 解析正确性（各子命令 + 参数默认值/覆盖）。
2. dispatch 路由（handler 被正确绑定到 args.handler）。
3. `report` 子命令对临时合成 SQLite 能正常打印（端到端但无网络）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# 保证 src 在 path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.cli import (  # noqa: E402
    _cmd_report,
    _cmd_run,
    _cmd_poll,
    _cmd_address,
    _cmd_discover,
    _cmd_bench,
    _cmd_llm,
    _cmd_dashboard,
    _cmd_evaluate,
    _cmd_cycle,
    _cmd_signals,
    _poll_once_async,
    _evaluate_once_async,
    _forecast_once_async,
    _FORECAST_IMB_THRESHOLD,
    build_parser,
    main,
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _parse(argv: list[str]) -> "argparse.Namespace":  # type: ignore[name-defined]
    import argparse  # noqa: F401
    return build_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# 解析正确性
# ---------------------------------------------------------------------------

class TestParsing:
    def test_run_default_config(self):
        args = _parse(["run"])
        assert args.cmd == "run"
        assert "config.yaml" in args.config

    def test_run_custom_config(self):
        args = _parse(["run", "--config", "/tmp/x.yaml"])
        assert args.config == "/tmp/x.yaml"
        assert args.handler is _cmd_run

    def test_poll_defaults(self):
        args = _parse(["poll"])
        assert args.cmd == "poll"
        assert args.loop is False
        assert args.interval == 3600.0
        assert "smc.db" in args.db
        assert args.handler is _cmd_poll

    def test_poll_loop_interval(self):
        args = _parse(["poll", "--loop", "--interval", "600"])
        assert args.loop is True
        assert args.interval == 600.0

    def test_report_defaults(self):
        args = _parse(["report"])
        assert args.cmd == "report"
        assert args.hours == 24.0
        assert "smc.db" in args.db
        assert args.handler is _cmd_report

    def test_report_hours_and_db(self):
        args = _parse(["report", "--hours", "6", "--db", "/tmp/t.db"])
        assert args.hours == 6.0
        assert args.db == "/tmp/t.db"

    def test_address_positional(self):
        args = _parse(["address", "0xABCD"])
        assert args.cmd == "address"
        assert args.addr == "0xABCD"
        assert "smc.db" in args.db
        assert args.handler is _cmd_address

    def test_address_custom_db(self):
        args = _parse(["address", "0xDEF", "--db", "/tmp/a.db"])
        assert args.db == "/tmp/a.db"

    def test_discover_default_top(self):
        args = _parse(["discover"])
        assert args.cmd == "discover"
        assert args.top == 15
        assert args.handler is _cmd_discover

    def test_discover_top(self):
        args = _parse(["discover", "--top", "20"])
        assert args.top == 20

    def test_bench_defaults(self):
        args = _parse(["bench"])
        assert args.cmd == "bench"
        assert args.bars == 300
        assert args.iters == 3000
        assert args.handler is _cmd_bench

    def test_bench_custom(self):
        args = _parse(["bench", "100", "500"])
        assert args.bars == 100
        assert args.iters == 500

    def test_llm_defaults(self):
        args = _parse(["llm"])
        assert args.cmd == "llm"
        assert args.hours == 6.0
        assert args.model == ""
        assert "smc.db" in args.db
        assert args.handler is _cmd_llm

    def test_llm_model_hours(self):
        args = _parse(["llm", "--hours", "12", "--model", "gpt-5.4"])
        assert args.hours == 12.0
        assert args.model == "gpt-5.4"

    def test_dashboard_defaults(self):
        args = _parse(["dashboard"])
        assert args.cmd == "dashboard"
        assert args.host == "127.0.0.1"
        assert args.port == 8787
        assert "smc.db" in args.db
        assert args.handler is _cmd_dashboard

    def test_dashboard_custom_port(self):
        args = _parse(["dashboard", "--port", "9000"])
        assert args.port == 9000

    def test_dashboard_host_port_db(self):
        args = _parse(["dashboard", "--host", "0.0.0.0", "--port", "9090",
                       "--db", "/data/x.db"])
        assert args.host == "0.0.0.0"
        assert args.port == 9090
        assert args.db == "/data/x.db"

    def test_evaluate_defaults(self):
        """evaluate 子命令默认值正确（--hours 168，--push False，handler 正确绑定）。"""
        args = _parse(["evaluate"])
        assert args.cmd == "evaluate"
        assert args.hours == 168.0
        assert args.push is False
        assert "smc.db" in args.db
        assert "config.yaml" in args.config
        assert args.handler is _cmd_evaluate

    def test_evaluate_custom_hours(self):
        args = _parse(["evaluate", "--hours", "48"])
        assert args.hours == 48.0

    def test_evaluate_push_flag(self):
        args = _parse(["evaluate", "--push"])
        assert args.push is True

    def test_evaluate_custom_db_and_config(self):
        args = _parse(["evaluate", "--db", "/tmp/e.db", "--config", "/tmp/e.yaml"])
        assert args.db == "/tmp/e.db"
        assert args.config == "/tmp/e.yaml"

    def test_cycle_defaults(self):
        """cycle 子命令默认值正确（--hours 168，--push False，handler 绑定 _cmd_cycle）。"""
        args = _parse(["cycle"])
        assert args.cmd == "cycle"
        assert args.hours == 168.0
        assert args.push is False
        assert "smc.db" in args.db
        assert "config.yaml" in args.config
        assert args.handler is _cmd_cycle

    def test_cycle_push_flag(self):
        args = _parse(["cycle", "--push"])
        assert args.push is True

    def test_cycle_custom_hours(self):
        args = _parse(["cycle", "--hours", "48"])
        assert args.hours == 48.0

    def test_cycle_custom_db_and_config(self):
        args = _parse(["cycle", "--db", "/tmp/c.db", "--config", "/tmp/c.yaml"])
        assert args.db == "/tmp/c.db"
        assert args.config == "/tmp/c.yaml"

    def test_no_subcommand_prints_help(self, capsys):
        """无子命令时 main() 打印 help 并 exit(0)。"""
        import pytest
        with patch("sys.argv", ["smc_tracker"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "子命令" in captured.out or "usage" in captured.out.lower()


# ---------------------------------------------------------------------------
# dispatch 路由（只验证 handler 被调，不真正执行）
# ---------------------------------------------------------------------------

class TestDispatch:
    def _run_with_mock(self, argv: list[str], handler_attr: str):
        """用 monkeypatch 替换 handler，验证 main() 是否 dispatch 到它。"""
        sentinel = MagicMock()
        args = _parse(argv)
        import types
        ns_copy = types.SimpleNamespace(**vars(args))
        ns_copy.handler = sentinel
        with patch("smc_tracker.cli.build_parser") as mock_bp:
            mock_ap = MagicMock()
            mock_ap.parse_args.return_value = ns_copy
            mock_bp.return_value = mock_ap
            with patch("sys.argv", ["smc_tracker"] + argv):
                main()
        sentinel.assert_called_once_with(ns_copy)

    def test_dispatch_run(self):
        self._run_with_mock(["run"], "handler")

    def test_dispatch_poll(self):
        self._run_with_mock(["poll"], "handler")

    def test_dispatch_report(self):
        self._run_with_mock(["report"], "handler")

    def test_dispatch_discover(self):
        self._run_with_mock(["discover"], "handler")

    def test_dispatch_bench(self):
        self._run_with_mock(["bench"], "handler")

    def test_dispatch_llm(self):
        self._run_with_mock(["llm"], "handler")

    def test_dispatch_dashboard(self):
        self._run_with_mock(["dashboard"], "handler")

    def test_dispatch_evaluate(self):
        self._run_with_mock(["evaluate"], "handler")

    def test_dispatch_cycle(self):
        self._run_with_mock(["cycle"], "handler")


# ---------------------------------------------------------------------------
# report 子命令端到端（临时合成 SQLite，无网络）
# ---------------------------------------------------------------------------

class TestReportCmd:
    """_cmd_report 对空库能正常打印（不 crash，不联网）。"""

    def _make_args(self, db_path: str, hours: float = 1.0) -> "argparse.Namespace":
        import argparse
        args = argparse.Namespace()
        args.cmd = "report"
        args.hours = hours
        args.db = db_path
        args.handler = _cmd_report
        return args

    def test_report_empty_db(self, capsys, tmp_path):
        """空库运行 report 不报错，输出包含「SMC 摘要」。"""
        db = str(tmp_path / "t.db")
        # 先初始化 schema（Store 会自动建表）
        from smc_tracker.storage import Store
        s = Store(Path(db))
        s.close()

        args = self._make_args(db, hours=24.0)
        _cmd_report(args)
        captured = capsys.readouterr()
        assert "SMC 摘要" in captured.out

    def test_report_with_signal_data(self, capsys, tmp_path):
        """库里有 signals 记录时，report 能打印该条信号。"""
        import time as _time
        db = str(tmp_path / "t.db")
        from smc_tracker.storage import Store
        s = Store(Path(db))
        now_ms = int(_time.time() * 1000)
        # 插入一条信号（不走 insert_signal 封装，直接插原始 SQL 避免依赖不稳定的内部 API）
        s.conn.execute(
            "INSERT INTO signals(ts,coin,direction,score,entry,stop,target,rr) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now_ms, "BTC", "long", 2.5, 50000.0, 49000.0, 52000.0, 2.0),
        )
        s.conn.commit()
        s.close()

        args = self._make_args(db, hours=24.0)
        _cmd_report(args)
        captured = capsys.readouterr()
        assert "BTC" in captured.out
        assert "做多" in captured.out

    def test_report_hours_6(self, capsys, tmp_path):  # noqa: D102 — keep original test
        """--hours 6 能正常执行（时间范围缩小），输出包含标题。"""
        db = str(tmp_path / "t.db")
        from smc_tracker.storage import Store
        s = Store(Path(db))
        s.close()

        args = self._make_args(db, hours=6.0)
        _cmd_report(args)
        captured = capsys.readouterr()
        # 输出应含 SMC 摘要标题（近 X 分钟字样）
        assert "分钟" in captured.out or "SMC" in captured.out


# ---------------------------------------------------------------------------
# evaluate 子命令端到端（临时合成 SQLite + fake price_of，无网络）
# ---------------------------------------------------------------------------

class TestEvaluateCmd:
    """_cmd_evaluate 使用 monkeypatch 注入 fake allMids，无网络。"""

    def _make_args(self, db_path: str, hours: float = 168.0,
                   push: bool = False, config: str = "/tmp/cfg.yaml") -> "argparse.Namespace":
        import argparse
        args = argparse.Namespace()
        args.cmd = "evaluate"
        args.hours = hours
        args.db = db_path
        args.push = push
        args.config = config
        args.handler = _cmd_evaluate
        return args

    def test_evaluate_empty_db_no_crash(self, capsys, tmp_path):
        """空库（无到期预测）：evaluate 正常输出"评估了 0 条"，不 crash。"""
        from unittest.mock import patch as _patch
        from pathlib import Path as _Path
        from smc_tracker.storage import Store
        from smc_tracker.review import PredictionReview

        db = str(tmp_path / "eval_empty.db")
        # 初始化库（Store 建 signals/candles 表；PredictionReview 建 predictions 表）
        s = Store(_Path(db))
        PredictionReview(s)  # 建表
        s.conn.commit()
        s.close()

        fake_mids: dict[str, str] = {"BTC": "50000.0", "ETH": "3000.0"}

        # patch _post 避免真实网络调用；_fetch_prices 内 HyperliquidInfo.__aenter__ 返回 self，
        # 再 await all_mids() → _post({"type":"allMids"}) → fake_mids
        with _patch(
            "smc_tracker.hyperliquid.info_client.HyperliquidInfo._post",
            return_value=fake_mids,
        ):
            args = self._make_args(db)
            _cmd_evaluate(args)

        captured = capsys.readouterr()
        assert "评估了 0 条" in captured.out
        assert "预测准确率回顾" in captured.out

    def test_evaluate_with_due_prediction(self, capsys, tmp_path):
        """库中有一条已到期预测：evaluate_due 应评估它，输出"评估了 1 条"。

        使用合成 db + patch HyperliquidInfo._post 注入 fake allMids，无网络。
        """
        import time as _time
        from pathlib import Path as _Path
        from unittest.mock import patch as _patch
        from smc_tracker.storage import Store
        from smc_tracker.review import PredictionReview

        db = str(tmp_path / "eval_due.db")
        s = Store(_Path(db))
        rev = PredictionReview(s)

        # 插入一条已到期的预测（2 小时前发出，1 小时水平线，已过期）
        now_ms = int(_time.time() * 1000)
        old_ts = now_ms - 7_200_000      # 2 小时前
        horizon_ms = 3_600_000           # 1 小时水平线
        # 直接调 record 落库
        rev.record(
            ts=old_ts,
            coin="BTC",
            kind="跟庄",
            direction="long",
            hl_px=48000.0,
            bg_px=48100.0,
            horizon_ms=horizon_ms,
            note="test",
        )
        s.conn.commit()
        s.close()

        fake_mids = {"BTC": "50000.0"}

        # patch _post 避免真实网络；_cmd_evaluate 用 asyncio.run(_fetch_prices())，
        # _fetch_prices 内走 HyperliquidInfo.__aenter__ → all_mids() → _post(...)
        with _patch(
            "smc_tracker.hyperliquid.info_client.HyperliquidInfo._post",
            return_value=fake_mids,
        ):
            args = self._make_args(db)
            _cmd_evaluate(args)

        captured = capsys.readouterr()
        assert "评估了 1 条" in captured.out
        assert "预测准确率回顾" in captured.out


# ---------------------------------------------------------------------------
# cycle 子命令端到端（mock poll + mock evaluate，无网络）
# ---------------------------------------------------------------------------

class TestCycleCmd:
    """_cmd_cycle 对合成 DB 正常执行，复用 _poll_once_async / _evaluate_once_async。"""

    def _make_args(self, db_path: str, hours: float = 168.0,
                   push: bool = False, config: str = "/tmp/cfg.yaml") -> "argparse.Namespace":
        import argparse
        args = argparse.Namespace()
        args.cmd = "cycle"
        args.hours = hours
        args.db = db_path
        args.push = push
        args.config = config
        args.handler = _cmd_cycle
        return args

    def test_cycle_no_push_uses_shared_helpers(self, capsys, tmp_path):
        """_cmd_cycle 调用 _poll_once_async 和 _evaluate_once_async（不推送版）。

        mock 两个共享 helper，验证均被调用、输出包含 digest 和准确率摘要。
        """
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, patch as _patch

        fake_digest = "📡 fake poll digest"
        fake_summary = "📊 预测准确率回顾\n样本不足"
        fake_n = 0

        async def _fake_poll(cfg, store):
            return fake_digest

        async def _fake_eval(store, hours):
            return fake_n, fake_summary

        db = str(tmp_path / "cycle_test.db")
        from smc_tracker.storage import Store
        from pathlib import Path as _Path
        s = Store(_Path(db))
        s.close()

        with _patch("smc_tracker.cli._poll_once_async", side_effect=_fake_poll), \
             _patch("smc_tracker.cli._evaluate_once_async", side_effect=_fake_eval):
            args = self._make_args(db)
            _cmd_cycle(args)

        captured = capsys.readouterr()
        assert fake_digest in captured.out
        assert "评估了 0 条" in captured.out
        assert "预测准确率回顾" in captured.out

    def test_cycle_poll_failure_still_runs_evaluate(self, capsys, tmp_path):
        """采集失败时，evaluate 仍正常执行（异常隔离，不因采集中断整轮闭环）。"""
        from unittest.mock import patch as _patch

        fake_summary = "📊 预测准确率回顾\n样本不足"

        async def _failing_poll(cfg, store):
            raise RuntimeError("网络超时")

        async def _fake_eval(store, hours):
            return 0, fake_summary

        db = str(tmp_path / "cycle_err.db")
        from smc_tracker.storage import Store
        from pathlib import Path as _Path
        s = Store(_Path(db))
        s.close()

        with _patch("smc_tracker.cli._poll_once_async", side_effect=_failing_poll), \
             _patch("smc_tracker.cli._evaluate_once_async", side_effect=_fake_eval):
            args = self._make_args(db)
            _cmd_cycle(args)

        captured = capsys.readouterr()
        # 采集失败消息应含错误描述
        assert "采集失败" in captured.out or "RuntimeError" in captured.out
        # 评估摘要仍应出现
        assert "预测准确率回顾" in captured.out

    def test_poll_once_and_evaluate_once_are_shared(self):
        """验证共享 helper 可直接从 smc_tracker.cli 导入（去重证据）。"""
        # 已在文件顶部 import，此处只验证可导入、是协程函数
        import inspect
        assert inspect.iscoroutinefunction(_poll_once_async)
        assert inspect.iscoroutinefunction(_evaluate_once_async)


# ---------------------------------------------------------------------------
# _forecast_once_async 单测（合成 l2Book，无网络）
# ---------------------------------------------------------------------------

class TestForecastOnceAsync:
    """_forecast_once_async 单测：注入 fake info + fake mids，验证前瞻信号落库。

    局限标注（CLAUDE.md 诚实原则）：
    - 测试仅覆盖订单簿挂单意图路径（单次快照），不测试流速/加速度（cron 模式无时序）。
    - 不联网（mock info._post）。
    """

    def _make_store(self, tmp_path):
        from pathlib import Path as _Path
        from smc_tracker.storage import Store
        from smc_tracker.review import PredictionReview
        db = str(tmp_path / "fc_test.db")
        s = Store(_Path(db))
        PredictionReview(s)  # 建 predictions 表
        s.conn.commit()
        return s

    def _make_fake_info(self, levels: list) -> MagicMock:
        """构造合成 info：_post 返回 {"levels": levels}。"""
        from unittest.mock import AsyncMock
        info = MagicMock()
        # l2Book 返回 {"levels": [bids, asks]}
        info._post = AsyncMock(return_value={"levels": levels})
        return info

    def test_strong_bid_imbalance_produces_long_signal(self, tmp_path):
        """强买盘失衡(imb > 0.25) → 落 flow_predictions(direction=long) + predictions(kind=前瞻)。

        去重证据：复用 orderbook_imbalance（signals/flow_predictor.py），不重写。
        """
        import asyncio
        from pathlib import Path as _Path

        store = self._make_store(tmp_path)

        # 合成买盘厚挂单（bid 名义 >> ask 名义，imb 约 0.82 >> 0.25 阈值）
        bids = [{"px": "100", "sz": "10"}] * 15   # 名义 100*10*15 = 15000
        asks = [{"px": "101", "sz": "1"}] * 15    # 名义 101*1*15 = 1515
        levels = [bids, asks]                       # levels[0]=bids, levels[1]=asks

        fake_info = self._make_fake_info(levels)
        fake_mids = {"DOGE": "0.15", "WIF": "2.5"}

        # patch meme_markets.yaml 读取，只测 DOGE/WIF 避免大量 mock
        fake_yaml = {"meme_markets": ["DOGE", "WIF"]}
        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", return_value=fake_yaml):
                n = asyncio.run(_forecast_once_async(store, fake_info, fake_mids))

        # 两个 coin 都强买盘失衡 → 各产生一条信号
        assert n == 2, f"期望 2 条信号，实际 {n}"

        # 验证 flow_predictions 落库
        fp_rows = store.conn.execute(
            "SELECT coin, direction, score FROM flow_predictions ORDER BY coin"
        ).fetchall()
        assert len(fp_rows) == 2
        for coin, direction, score in fp_rows:
            assert direction == "long", f"{coin} 方向应为 long，实际 {direction}"
            assert score > _FORECAST_IMB_THRESHOLD, f"{coin} score={score} 应 > 阈值"

        # 验证 predictions 落库，kind='前瞻'
        pred_rows = store.conn.execute(
            "SELECT coin, kind, direction FROM predictions WHERE kind='前瞻' ORDER BY coin"
        ).fetchall()
        assert len(pred_rows) == 2
        for coin, kind, direction in pred_rows:
            assert kind == "前瞻"
            assert direction == "long"

        store.close()

    def test_strong_ask_imbalance_produces_short_signal(self, tmp_path):
        """强卖盘失衡(imb < -0.25) → 产生 direction=short 信号。"""
        import asyncio

        store = self._make_store(tmp_path)

        # 卖盘厚（ask 名义 >> bid 名义，imb 约 -0.82）
        bids = [{"px": "100", "sz": "1"}] * 15
        asks = [{"px": "101", "sz": "10"}] * 15
        levels = [bids, asks]

        fake_info = self._make_fake_info(levels)
        fake_mids = {"DOGE": "0.15"}

        fake_yaml = {"meme_markets": ["DOGE"]}
        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", return_value=fake_yaml):
                n = asyncio.run(_forecast_once_async(store, fake_info, fake_mids))

        assert n == 1
        fp_rows = store.conn.execute("SELECT direction FROM flow_predictions").fetchall()
        assert fp_rows[0][0] == "short"

        store.close()

    def test_weak_imbalance_no_signal(self, tmp_path):
        """弱失衡(abs(imb) < 0.25) → 不产生信号，不落库。"""
        import asyncio

        store = self._make_store(tmp_path)

        # 接近均衡（imb ≈ 0）
        bids = [{"px": "100", "sz": "5"}] * 15
        asks = [{"px": "101", "sz": "5"}] * 15
        levels = [bids, asks]

        fake_info = self._make_fake_info(levels)
        fake_mids = {"DOGE": "0.15"}

        fake_yaml = {"meme_markets": ["DOGE"]}
        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", return_value=fake_yaml):
                n = asyncio.run(_forecast_once_async(store, fake_info, fake_mids))

        assert n == 0, "弱失衡不应产生信号"
        cnt = store.conn.execute("SELECT COUNT(*) FROM flow_predictions").fetchone()[0]
        assert cnt == 0
        cnt_p = store.conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE kind='前瞻'"
        ).fetchone()[0]
        assert cnt_p == 0

        store.close()

    def test_no_price_in_mids_skips_coin(self, tmp_path):
        """coin 无有效价格(mids 中缺失或 0) → 跳过，不落库。"""
        import asyncio

        store = self._make_store(tmp_path)

        # 强买盘失衡但 mids 无价格
        bids = [{"px": "100", "sz": "10"}] * 15
        asks = [{"px": "101", "sz": "1"}] * 15
        levels = [bids, asks]

        fake_info = self._make_fake_info(levels)
        fake_mids: dict[str, str] = {}  # 无价格

        fake_yaml = {"meme_markets": ["DOGE"]}
        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", return_value=fake_yaml):
                n = asyncio.run(_forecast_once_async(store, fake_info, fake_mids))

        assert n == 0, "无价格时不应落库"
        store.close()

    def test_info_post_failure_handled_gracefully(self, tmp_path):
        """单 coin l2Book 拉取失败 → 跳过该 coin，不抛异常，其他 coin 正常处理。"""
        import asyncio
        from unittest.mock import AsyncMock

        store = self._make_store(tmp_path)

        # DOGE 抛异常，WIF 正常返回强买盘失衡
        bids = [{"px": "100", "sz": "10"}] * 15
        asks = [{"px": "101", "sz": "1"}] * 15

        call_count = 0

        async def _mock_post(body):
            nonlocal call_count
            coin = body.get("coin", "")
            call_count += 1
            if coin == "DOGE":
                raise RuntimeError("网络超时")
            return {"levels": [bids, asks]}

        fake_info = MagicMock()
        fake_info._post = _mock_post
        fake_mids = {"DOGE": "0.15", "WIF": "2.5"}

        fake_yaml = {"meme_markets": ["DOGE", "WIF"]}
        with patch("builtins.open", MagicMock()):
            with patch("yaml.safe_load", return_value=fake_yaml):
                n = asyncio.run(_forecast_once_async(store, fake_info, fake_mids))

        # DOGE 失败跳过，WIF 正常产生
        assert n == 1, f"期望 1 条信号(WIF)，实际 {n}"
        rows = store.conn.execute("SELECT coin FROM flow_predictions").fetchall()
        assert rows[0][0] == "WIF"

        store.close()

    def test_forecast_is_coroutine_and_exported(self):
        """_forecast_once_async 可从 smc_tracker.cli 导入，且是协程函数（去重证据）。"""
        import inspect
        assert inspect.iscoroutinefunction(_forecast_once_async)
        # 阈值常量也可导入（避免调用方魔法数字）
        assert _FORECAST_IMB_THRESHOLD == 0.25


# ---------------------------------------------------------------------------
# build_all_signals_report 单元测试（tmp Store 插样本，无网络）
# ---------------------------------------------------------------------------

class TestBuildAllSignalsReport:
    """build_all_signals_report：tmp Store 插各类型样本行 → 断言输出含各类型与证据串。

    TDD：先写测试（此处），再实现（notify/report.py）。
    合成数据确定性；不联网；表可能不存在 → _safe_fetchall 优雅跳过（已在 all_signals.py 实现）。
    """

    def _make_store(self, tmp_path):
        from pathlib import Path as _Path
        from smc_tracker.storage import Store
        db = str(tmp_path / "all_sig_test.db")
        return Store(_Path(db))

    def _now_ms(self) -> int:
        import time as _t
        return int(_t.time() * 1000)

    def test_empty_db_returns_no_signal_text(self, tmp_path):
        """空库时输出含「无信号」提示，不 crash。"""
        from smc_tracker.notify import build_all_signals_report
        store = self._make_store(tmp_path)
        now = self._now_ms()
        out = build_all_signals_report(store, now - 3_600_000, now)
        store.close()
        assert "全信号汇总" in out or "无信号" in out

    def test_disclaimer_always_present(self, tmp_path):
        """免责声明（1h≈随机/非投资建议）无论有无数据都必须出现在输出中。"""
        from smc_tracker.notify import build_all_signals_report
        store = self._make_store(tmp_path)
        now = self._now_ms()
        out = build_all_signals_report(store, now - 3_600_000, now)
        store.close()
        assert "非投资建议" in out

    def test_signal_row_appears_in_output(self, tmp_path):
        """signals 表有一条记录 → 输出含 coin、方向、分数与证据串。"""
        from smc_tracker.notify import build_all_signals_report
        store = self._make_store(tmp_path)
        now = self._now_ms()
        store.conn.execute(
            "INSERT INTO signals(ts,coin,direction,score,entry,stop,target,rr,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now - 1000, "ETH", "long", 3.5, 3000.0, 2900.0, 3300.0, 3.0, "结构+流向共振"),
        )
        store.conn.commit()
        out = build_all_signals_report(store, now - 3_600_000, now)
        store.close()
        assert "ETH" in out
        assert "long" in out
        assert "结构+流向共振" in out

    def test_divergence_row_appears_in_output(self, tmp_path):
        """divergence 表有一条记录 → 输出含背离类型标签与 coin。"""
        from smc_tracker.notify import build_all_signals_report
        store = self._make_store(tmp_path)
        now = self._now_ms()
        store.conn.execute(
            "INSERT INTO divergence(ts,coin,direction,score,funding,oi_change_pct,dex_flow_usd,reason) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now - 2000, "SOL", "bullish", 1.8, 0.0005, 0.03, 500000.0, "资金费异常"),
        )
        store.conn.commit()
        out = build_all_signals_report(store, now - 3_600_000, now)
        store.close()
        assert "SOL" in out
        assert "背离" in out

    def test_whale_signal_row_appears_in_output(self, tmp_path):
        """whale_signals 表有记录 → 输出含「跟庄」标签与 coin。"""
        from smc_tracker.notify import build_all_signals_report
        store = self._make_store(tmp_path)
        now = self._now_ms()
        store.conn.execute(
            "INSERT INTO whale_signals(ts,address,label,coin,action,direction,notional,px,pos_after,taker) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now - 500, "0x1234567890abcdef", "庄王", "BTC", "OPEN", "long",
             2_000_000.0, 50000.0, 2_000_000.0, 1),
        )
        store.conn.commit()
        out = build_all_signals_report(store, now - 3_600_000, now)
        store.close()
        assert "BTC" in out
        assert "跟庄" in out

    def test_multiple_types_all_grouped(self, tmp_path):
        """signals + divergence + whale_signals 均插样本 → 输出含所有三种类型标签。"""
        from smc_tracker.notify import build_all_signals_report
        store = self._make_store(tmp_path)
        now = self._now_ms()
        # signals 行
        store.conn.execute(
            "INSERT INTO signals(ts,coin,direction,score,entry,stop,target,rr) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now - 1000, "AVAX", "short", -2.1, 30.0, 31.0, 27.0, 1.5),
        )
        # divergence 行
        store.conn.execute(
            "INSERT INTO divergence(ts,coin,direction,score,funding,oi_change_pct,dex_flow_usd,reason) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now - 2000, "MATIC", "bearish", 1.2, -0.001, -0.05, -300000.0, "CEX空头主导"),
        )
        # whale_signals 行
        store.conn.execute(
            "INSERT INTO whale_signals(ts,address,label,coin,action,direction,notional,px,pos_after,taker) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now - 3000, "0xdeadbeef00000000", "巨鲸A", "DOGE", "ADD", "long",
             500_000.0, 0.12, 800_000.0, 0),
        )
        store.conn.commit()
        out = build_all_signals_report(store, now - 3_600_000, now)
        store.close()
        assert "SMC共振" in out
        assert "背离" in out
        assert "跟庄" in out
        # 各 coin 均应出现
        assert "AVAX" in out
        assert "MATIC" in out
        assert "DOGE" in out

    def test_evidence_text_included_in_each_line(self, tmp_path):
        """每条信号行必须包含「—」分隔符后的证据文本（evidence_text 非空）。"""
        from smc_tracker.notify import build_all_signals_report
        store = self._make_store(tmp_path)
        now = self._now_ms()
        store.conn.execute(
            "INSERT INTO signals(ts,coin,direction,score,entry,stop,target,rr,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now - 100, "BNB", "long", 1.0, 300.0, 290.0, 330.0, 2.0, "BNB特殊证据"),
        )
        store.conn.commit()
        out = build_all_signals_report(store, now - 3_600_000, now)
        store.close()
        # 每个信号行应含「 — 」分隔的证据
        signal_lines = [ln for ln in out.splitlines() if "BNB" in ln and " — " in ln]
        assert len(signal_lines) >= 1, f"未找到含证据的 BNB 信号行：\n{out}"
        assert "BNB特殊证据" in out

    def test_rows_outside_window_excluded(self, tmp_path):
        """窗口外（too old）的行不纳入输出。"""
        from smc_tracker.notify import build_all_signals_report
        store = self._make_store(tmp_path)
        now = self._now_ms()
        # 插入 2 小时前的行（窗口是 1 小时）
        old_ts = now - 7_200_000
        store.conn.execute(
            "INSERT INTO signals(ts,coin,direction,score,entry,stop,target,rr) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (old_ts, "LINK", "long", 1.0, 10.0, 9.0, 12.0, 2.0),
        )
        store.conn.commit()
        out = build_all_signals_report(store, now - 3_600_000, now)
        store.close()
        # LINK 在窗口外，不应出现在信号明细中
        assert "LINK" not in out

    def test_custom_title_appears(self, tmp_path):
        """自定义 title 参数出现在输出头部。"""
        from smc_tracker.notify import build_all_signals_report
        store = self._make_store(tmp_path)
        now = self._now_ms()
        out = build_all_signals_report(store, now - 3_600_000, now, title="自定义报告标题")
        store.close()
        assert "自定义报告标题" in out

    def test_exported_from_notify_init(self):
        """build_all_signals_report 已从 notify/__init__.py 导出（符号可达性）。"""
        from smc_tracker.notify import build_all_signals_report as _fn
        assert callable(_fn)


# ---------------------------------------------------------------------------
# signals 子命令解析与端到端测试
# ---------------------------------------------------------------------------

class TestSignalsCmd:
    """signals 子命令：解析正确性 + 端到端（临时合成 SQLite，无网络）。"""

    def _make_args(self, db_path: str, hours: float = 1.0) -> "argparse.Namespace":
        import argparse
        args = argparse.Namespace()
        args.cmd = "signals"
        args.hours = hours
        args.db = db_path
        args.handler = _cmd_signals
        return args

    def test_signals_parsing_defaults(self):
        """argparse: signals 子命令默认值正确（--hours 24，handler 绑定 _cmd_signals）。"""
        args = build_parser().parse_args(["signals"])
        assert args.cmd == "signals"
        assert args.hours == 24.0
        assert "smc.db" in args.db
        assert args.handler is _cmd_signals

    def test_signals_parsing_custom_hours_db(self):
        """argparse: --hours / --db 可覆盖默认值。"""
        args = build_parser().parse_args(["signals", "--hours", "6", "--db", "/tmp/s.db"])
        assert args.hours == 6.0
        assert args.db == "/tmp/s.db"

    def test_signals_dispatch(self):
        """dispatch：main() 正确路由到 _cmd_signals handler。"""
        from types import SimpleNamespace
        sentinel = MagicMock()
        ns = SimpleNamespace(cmd="signals", hours=1.0, db="/tmp/x.db", handler=sentinel)
        with patch("smc_tracker.cli.build_parser") as mock_bp:
            mock_ap = MagicMock()
            mock_ap.parse_args.return_value = ns
            mock_bp.return_value = mock_ap
            with patch("sys.argv", ["smc_tracker", "signals"]):
                main()
        sentinel.assert_called_once_with(ns)

    def test_signals_empty_db_no_crash(self, capsys, tmp_path):
        """空库执行 _cmd_signals 不 crash，输出含标题与免责。"""
        from pathlib import Path as _Path
        from smc_tracker.storage import Store
        db = str(tmp_path / "sig_empty.db")
        Store(_Path(db)).close()
        args = self._make_args(db, hours=1.0)
        _cmd_signals(args)
        captured = capsys.readouterr()
        assert "全信号汇总" in captured.out
        assert "非投资建议" in captured.out

    def test_signals_with_data_prints_type_and_evidence(self, capsys, tmp_path):
        """库中有 signals 行时，输出包含 SMC共振 标签与证据串。"""
        import time as _t
        from pathlib import Path as _Path
        from smc_tracker.storage import Store
        db = str(tmp_path / "sig_data.db")
        s = Store(_Path(db))
        now_ms = int(_t.time() * 1000)
        s.conn.execute(
            "INSERT INTO signals(ts,coin,direction,score,entry,stop,target,rr,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now_ms - 500, "XRP", "long", 2.0, 0.6, 0.55, 0.7, 2.0, "XRP结构确认"),
        )
        s.conn.commit()
        s.close()
        args = self._make_args(db, hours=1.0)
        _cmd_signals(args)
        captured = capsys.readouterr()
        assert "XRP" in captured.out
        assert "SMC共振" in captured.out
        assert "XRP结构确认" in captured.out
