"""MTF 分层入场决策单测（用户规范:顶层定向/中层确认/底层触发）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals.mtf_confluence import mtf_decision, fmt_mtf


def _d(direction, conf=0.5):
    return {"direction": direction, "confidence": conf}


def test_aligned_long_enters_highest_conf_bottom():
    """顶(12h+1d多)=中(1h+4h多)=long,底层两个都long→取最高confidence(15m 0.8)入场。"""
    decisions = {
        "12h": _d("long"), "1d": _d("long"),       # 顶层 long
        "1h": _d("long"), "4h": _d("long"),        # 中层 long
        "5m": _d("long", 0.6), "15m": _d("long", 0.8),  # 底层 long,15m 置信高
    }
    out = mtf_decision(decisions)
    assert out is not None and out["direction"] == "long"
    assert out["entry_tf"] == "15m" and abs(out["confidence"] - 0.8) < 1e-9


def test_top_mid_disagree_holds():
    """顶层 long 但中层 short → 不同向 → hold(None)。"""
    decisions = {
        "12h": _d("long"), "1d": _d("long"),
        "1h": _d("short"), "4h": _d("short"),
        "5m": _d("long"), "15m": _d("long"),
    }
    assert mtf_decision(decisions) is None


def test_top_tie_holds():
    """顶层平局(12h long / 1d short)→ 顶层无明确方向 → hold。"""
    decisions = {
        "12h": _d("long"), "1d": _d("short"),
        "1h": _d("long"), "4h": _d("long"),
        "15m": _d("long"),
    }
    assert mtf_decision(decisions) is None


def test_bottom_no_support_holds():
    """顶=中=long 对齐,但底层 5m/15m 都 short(无同向)→ hold。"""
    decisions = {
        "12h": _d("long"), "1d": _d("long"),
        "1h": _d("long"), "4h": _d("long"),
        "5m": _d("short"), "15m": _d("short"),
    }
    assert mtf_decision(decisions) is None


def test_bottom_one_support_enters():
    """底层仅 15m 支持(5m 反向)→ 仍入场(至少一个同向),entry_tf=15m。"""
    decisions = {
        "12h": _d("short"), "1d": _d("short"),
        "1h": _d("short"), "4h": _d("short"),
        "5m": _d("long", 0.9), "15m": _d("short", 0.4),
    }
    out = mtf_decision(decisions)
    assert out is not None and out["direction"] == "short" and out["entry_tf"] == "15m"


def test_majority_not_unanimous():
    """中层多数即可(1h long / 4h 无表态)→ mid=long;顶层同理。"""
    decisions = {
        "12h": _d("long"), "1d": {"direction": None},   # 顶层多数 long(1票)
        "1h": _d("long"), "4h": {"direction": None},    # 中层多数 long
        "15m": _d("long"),
    }
    out = mtf_decision(decisions)
    assert out is not None and out["direction"] == "long"


def test_case_insensitive_tf_keys():
    """大写 12H/1D/1H/4H 写法也匹配(系统 CANONICAL 用大写H)。"""
    decisions = {
        "12H": _d("long"), "1D": _d("long"),
        "1H": _d("long"), "4H": _d("long"),
        "15m": _d("long", 0.7),
    }
    out = mtf_decision(decisions)
    assert out is not None and out["direction"] == "long"


def test_fmt_hold_and_entry():
    assert "HOLD" in fmt_mtf("BTC", None)
    out = mtf_decision({"12h": _d("long"), "1d": _d("long"),
                        "1h": _d("long"), "4h": _d("long"), "15m": _d("long", 0.7)})
    assert "做多" in fmt_mtf("BTC", out) and "对齐" in fmt_mtf("BTC", out)


def test_cli_mtf_runs(tmp_path, capsys):
    """#202 CLI mtf 端到端:种 6 层周期 K 线 → 谐波 per-tf 决策 → MTF 报告(无网络)。"""
    import math
    from smc_tracker.cli import build_parser
    from smc_tracker.storage import Store
    db = str(tmp_path / "mtf.db")
    s = Store(Path(db))
    s.add_monitored_coins([("BTC", "BTCUSDT", 1, "")])
    rows = []
    for tf, ms in [("5m", 300_000), ("15m", 900_000), ("1H", 3_600_000),
                   ("4H", 14_400_000), ("12H", 43_200_000), ("1D", 86_400_000)]:
        px = 100.0
        for i in range(120):
            px *= math.exp(0.02 * math.sin(i / 6.0))
            rows.append(("BTC", tf, i * ms, px, px * 1.01, px * 0.99, px, 1.0))
    s.upsert_candles(rows)
    s.close()
    args = build_parser().parse_args(["mtf", "--db", db])
    args.handler(args)
    assert "MTF 分层入场决策" in capsys.readouterr().out
