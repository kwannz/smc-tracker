"""CLI backtest 子命令单测（#201,谐波/SMC 回测机器人,读 DB 无网络）。"""
from __future__ import annotations

import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.cli import build_parser
from smc_tracker.storage import Store


def test_backtest_subcommand_registered():
    args = build_parser().parse_args(["backtest", "--tf", "1H", "--rr", "2.0"])
    assert args.tf == "1H" and args.rr == 2.0 and hasattr(args, "handler")


def test_backtest_runs_on_stored_candles(tmp_path, capsys):
    """端到端:种入 K 线 → backtest CLI 跑出 freqtrade 式绩效报告(或诚实无信号提示)。"""
    db = str(tmp_path / "bt.db")
    s = Store(Path(db))
    s.add_monitored_coins([("BTC", "BTCUSDT", 1, "")])
    # 120 根带摆动结构的 K 线(制造 BOS 给回测器)
    rows = []
    px = 100.0
    for i in range(120):
        px *= math.exp(0.01 * math.sin(i / 5.0) + (0.004 if i % 30 < 15 else -0.004))
        h, l = px * 1.006, px * 0.994
        rows.append(("BTC", "1H", i * 3_600_000, px, h, l, px, 1.0))
    s.upsert_candles(rows)
    s.close()
    args = build_parser().parse_args(["backtest", "--tf", "1H", "--db", db])
    args.handler(args)
    out = capsys.readouterr().out
    assert "回测" in out and "freqtrade" in out      # 报告头出现=命令端到端跑通
