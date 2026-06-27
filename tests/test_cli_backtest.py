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


def test_harmonic_backtest_runs_no_repaint():
    """#201 谐波回测:HarmonicState 重放→build_setups→run_setups,返回 BacktestResult(结构性,无崩溃)。"""
    from smc_tracker.backtest import harmonic_backtest, BacktestResult
    from smc_tracker.models import Candle
    px, cs = 100.0, []
    for i in range(200):
        px *= math.exp(0.02 * math.sin(i / 6.0))
        cs.append(Candle("BTC", "1H", i * 3_600_000, (i + 1) * 3_600_000,
                         px, px * 1.01, px * 0.99, px, 1.0, 0))
    res = harmonic_backtest("BTC", "1H", cs, target_rr=2.0)
    assert isinstance(res, BacktestResult)            # 跑通返回结果(谐波形态有无视数据而定)
    assert all(t.entry_idx >= 0 for t in res.trades)  # no-repaint:entry_idx 有效


def test_sfg_consensus_and_require_sfg_filter():
    """#203 充分使用 SFG:sfg_consensus 返回零前视共识 bias;require_sfg 过滤后交易 ≤ 不过滤。"""
    from smc_tracker.backtest import harmonic_backtest
    from smc_tracker.backtest.harmonic import sfg_consensus
    from smc_tracker.models import Candle
    px, cs = 100.0, []
    for i in range(220):
        px *= math.exp(0.025 * math.sin(i / 7.0))
        cs.append(Candle("BTC", "1H", i * 3_600_000, (i + 1) * 3_600_000,
                         px, px * 1.012, px * 0.988, px, 1.0, 0))
    bias = sfg_consensus(cs)
    assert len(bias) == len(cs) and all(abs(b) <= 10.0 for b in bias)   # 10 因子 ∈[-10,10]
    base = harmonic_backtest("BTC", "1H", cs)
    filt = harmonic_backtest("BTC", "1H", cs, require_sfg=True)
    assert len(filt.trades) <= len(base.trades)        # SFG 确认只减不增入场
    # #208 S/R 确认同为过滤器(只减不增);_near_sr 同向匹配
    from smc_tracker.backtest.harmonic import _near_sr
    sr = harmonic_backtest("BTC", "1H", cs, require_sr=True)
    assert len(sr.trades) <= len(base.trades)
    assert _near_sr(100.0, "long", {"support": [(100.3, 2)], "resistance": []})       # 0.3%内
    assert not _near_sr(100.0, "long", {"support": [(105.0, 2)], "resistance": []})   # 5%外


def test_backtest_harmonic_flag(tmp_path, capsys):
    db = str(tmp_path / "h.db")
    s = Store(Path(db))
    s.add_monitored_coins([("BTC", "BTCUSDT", 1, "")])
    px, rows = 100.0, []
    for i in range(150):
        px *= math.exp(0.015 * math.sin(i / 5.0))
        rows.append(("BTC", "1H", i * 3_600_000, px, px * 1.01, px * 0.99, px, 1.0))
    s.upsert_candles(rows)
    s.close()
    args = build_parser().parse_args(["backtest", "--tf", "1H", "--harmonic", "--db", db])
    args.handler(args)
    assert "谐波 setup" in capsys.readouterr().out     # --harmonic 走谐波分支
