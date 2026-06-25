"""CLI watch 子命令单测：解析 + handler 直跑（tmp db，无网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.cli import build_parser
from smc_tracker.storage import Store


def test_watch_add_then_list(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    ap = build_parser()
    # add
    args = ap.parse_args(["watch", "add", "BTC", "ETH", "--note", "core", "--db", db])
    args.handler(args)
    assert Store(Path(db)).get_monitored_coins() == {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
    # list 打印含 BTC
    args = ap.parse_args(["watch", "list", "--db", db])
    args.handler(args)
    assert "BTC" in capsys.readouterr().out


def test_watch_rm(tmp_path):
    db = str(tmp_path / "t.db")
    ap = build_parser()
    args = ap.parse_args(["watch", "add", "BTC", "ETH", "--db", db])
    args.handler(args)
    args = ap.parse_args(["watch", "rm", "BTC", "--db", db])
    args.handler(args)
    assert Store(Path(db)).get_monitored_coins() == {"ETH": "ETHUSDT"}


def test_watch_add_lowercase_normalized(tmp_path):
    """小写输入归一为大写 coin + symbol。"""
    db = str(tmp_path / "t.db")
    ap = build_parser()
    args = ap.parse_args(["watch", "add", "sol", "--db", db])
    args.handler(args)
    assert Store(Path(db)).get_monitored_coins() == {"SOL": "SOLUSDT"}
