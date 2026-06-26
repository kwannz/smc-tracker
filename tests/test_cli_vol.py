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


def test_vol_empty_watchlist_no_data(tmp_path, capsys):
    """空清单 + 无 K 线 → 诚实提示无可显示(不再死板'监控清单为空')。"""
    db = str(tmp_path / "t.db")
    Store(Path(db)).close()  # 建库但不加币、无 K 线
    ap = build_parser()
    args = ap.parse_args(["vol", "--db", db])
    args.handler(args)
    assert "无可显示币" in capsys.readouterr().out


def test_vol_empty_watchlist_falls_back_to_collected(tmp_path, capsys):
    """空清单但 DB 有 K 线 → fallback 出板(与 dashboard 共用 pick_coins，不再死胡同，#141)。"""
    db = str(tmp_path / "t.db")
    s = Store(Path(db))
    rows = [("ETH", "15m", i * 900_000, 100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i, 1.0)
            for i in range(40)]   # 关键：未 add_monitored_coins，清单空但已采 K 线
    s.upsert_candles(rows)
    s.close()
    ap = build_parser()
    args = ap.parse_args(["vol", "--tf", "15m", "--db", db])
    args.handler(args)
    out = capsys.readouterr().out
    assert "ETH" in out and "波动追踪" in out
