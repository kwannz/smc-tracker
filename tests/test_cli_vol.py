"""CLI vol 子命令单测：监控清单 + DB 合成 K 线 → 波动板（无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.cli import build_parser
from smc_tracker.storage import Store


def _seed(db: str):
    """种入监控清单 + 一段上行 15m K 线（供 vol 计算）。"""
    s = Store(Path(db))
    s.add_monitored_coins([("BTC", "BTCUSDT", 1, "")])
    rows = []
    for i in range(40):
        px = 100.0 + i  # 线性上行 → 正速度
        rows.append(("BTC", "15m", i * 900_000, px, px, px, px, 1.0))
    s.upsert_candles(rows)
    s.close()


def test_vol_prints_board(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    _seed(db)
    ap = build_parser()
    args = ap.parse_args(["vol", "--tf", "15m", "--db", db])
    args.handler(args)
    out = capsys.readouterr().out
    assert "BTC" in out and "波动追踪" in out


def test_vol_empty_watchlist(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    Store(Path(db)).close()  # 建库但不加币
    ap = build_parser()
    args = ap.parse_args(["vol", "--db", db])
    args.handler(args)
    assert "监控清单为空" in capsys.readouterr().out
