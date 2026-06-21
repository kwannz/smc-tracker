"""OKX 永续实时 streaming 监控独立脚本（无 API key）。

核心逻辑已移至 smc_tracker.okx.stream，本脚本仅提供 argparse 入口。
用法：PYTHONPATH=src ./.venv/bin/python scripts/okx_stream.py [--top N] [--secs S]
"""
from __future__ import annotations

import argparse
import asyncio

from smc_tracker.okx.stream import run_stream


def main() -> None:
    ap = argparse.ArgumentParser(description="OKX perp realtime streaming monitor")
    ap.add_argument("--top", type=int, default=10, help="monitor top N perps by OI")
    ap.add_argument("--secs", type=float, default=15.0, help="run seconds")
    args = ap.parse_args()
    asyncio.run(run_stream(args.top, args.secs))


if __name__ == "__main__":
    main()
