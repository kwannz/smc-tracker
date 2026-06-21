"""分析 meme 历史 K 线的暴涨暴跌规律（读 data/history/*.csv，纯 numpy）。

暴涨/暴跌事件：未来 window 根内最大涨幅 ≥ pump_th（暴涨）/ 最大跌幅 ≤ -dump_th（暴跌）。
对每个事件统计前置条件：前窗放量倍数、前窗波动(盘整)、起点 RSI、UTC 时段、星期。
汇总跨币规律，存 data/history/pump_analysis.json。

运行：./.venv/bin/python scripts/analyze_pumps.py [window=24] [pump_th=0.5] [dump_th=0.3]
"""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "data" / "history"

WINDOW = int(sys.argv[1]) if len(sys.argv) > 1 else 24
PUMP_TH = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
DUMP_TH = float(sys.argv[3]) if len(sys.argv) > 3 else 0.3


def load(path: Path):
    ts, o, h, l, c, v = [], [], [], [], [], []
    with path.open(encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            ts.append(int(row[0])); o.append(float(row[1])); h.append(float(row[2]))
            l.append(float(row[3])); c.append(float(row[4])); v.append(float(row[5]))
    return (np.array(ts), np.array(o), np.array(h), np.array(l), np.array(c), np.array(v))


def rsi(close, n=14):
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    d = np.diff(close)
    g = np.where(d > 0, d, 0.0); ls = np.where(d < 0, -d, 0.0)
    ag = np.zeros(len(d)); al = np.zeros(len(d))
    ag[n - 1] = g[:n].mean(); al[n - 1] = ls[:n].mean()
    for i in range(n, len(d)):
        ag[i] = (ag[i - 1] * (n - 1) + g[i]) / n
        al[i] = (al[i - 1] * (n - 1) + ls[i]) / n
    rs = np.divide(ag, al, out=np.full_like(ag, np.inf), where=al != 0)
    out[1:] = 100 - 100 / (1 + rs)
    return out


def analyze_coin(path: Path):
    ts, o, h, l, c, v = load(path)
    n = len(c)
    if n < WINDOW + 50:
        return None
    rsi_s = rsi(c)
    vol_ma = np.convolve(v, np.ones(WINDOW) / WINDOW, mode="same")
    pumps, dumps = [], []
    for i in range(30, n - WINDOW):
        fwd_h = h[i + 1:i + 1 + WINDOW].max()
        fwd_l = l[i + 1:i + 1 + WINDOW].min()
        up = (fwd_h - c[i]) / c[i]
        dn = (fwd_l - c[i]) / c[i]
        pre_vol = v[i] / vol_ma[i] if vol_ma[i] > 0 else 0.0   # 起点放量倍数
        pre_range = (h[i - 24:i].max() - l[i - 24:i].min()) / c[i] if i >= 24 else 0.0
        tm = time.gmtime(ts[i] / 1000)
        ev = {"ret": float(up if up >= PUMP_TH else dn), "pre_vol": float(pre_vol),
              "pre_range": float(pre_range), "rsi": float(rsi_s[i]) if np.isfinite(rsi_s[i]) else None,
              "hour": tm.tm_hour, "wday": tm.tm_wday}
        if up >= PUMP_TH:
            pumps.append(ev)
        elif dn <= -DUMP_TH:
            dumps.append(ev)
    return {"coin": path.stem, "bars": n, "pumps": pumps, "dumps": dumps}


def summarize(events: list[dict]) -> dict:
    if not events:
        return {"n": 0}
    pv = [e["pre_vol"] for e in events if e["pre_vol"]]
    pr = [e["pre_range"] for e in events if e["pre_range"]]
    rs = [e["rsi"] for e in events if e["rsi"] is not None]
    return {
        "n": len(events),
        "avg_pre_vol_x": round(float(np.mean(pv)), 2) if pv else None,
        "avg_pre_range_pct": round(float(np.mean(pr)) * 100, 1) if pr else None,
        "avg_rsi": round(float(np.mean(rs)), 1) if rs else None,
        "top_hours_utc": [hh for hh, _ in Counter(e["hour"] for e in events).most_common(3)],
        "top_wdays": [wd for wd, _ in Counter(e["wday"] for e in events).most_common(3)],
    }


def main() -> int:
    files = sorted(HIST.glob("*.csv"))
    if not files:
        print(f"⚠ {HIST} 无历史数据，先跑 scripts/fetch_bitget_history.py")
        return 1
    all_pumps, all_dumps, per_coin = [], [], []
    print(f"分析 {len(files)} 个币 | 暴涨阈值 +{PUMP_TH*100:.0f}% / 暴跌 -{DUMP_TH*100:.0f}% (未来{WINDOW}根)\n")
    for path in files:
        r = analyze_coin(path)
        if not r:
            continue
        all_pumps += r["pumps"]; all_dumps += r["dumps"]
        per_coin.append({"coin": r["coin"], "bars": r["bars"],
                         "pumps": len(r["pumps"]), "dumps": len(r["dumps"])})
        print(f"  {r['coin']:<14} 暴涨{len(r['pumps']):>3} 暴跌{len(r['dumps']):>3} ({r['bars']}根)")

    psum, dsum = summarize(all_pumps), summarize(all_dumps)
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    print("\n" + "=" * 60)
    print(f"📈 暴涨规律 (n={psum['n']}): 前置放量 {psum.get('avg_pre_vol_x')}× | "
          f"前24根波幅 {psum.get('avg_pre_range_pct')}% | 起点RSI {psum.get('avg_rsi')}")
    print(f"   高发 UTC 时段 {psum.get('top_hours_utc')} | 星期 {[wd[i] for i in psum.get('top_wdays',[])]}")
    print(f"📉 暴跌规律 (n={dsum['n']}): 前置放量 {dsum.get('avg_pre_vol_x')}× | "
          f"前24根波幅 {dsum.get('avg_pre_range_pct')}% | 起点RSI {dsum.get('avg_rsi')}")
    print(f"   高发 UTC 时段 {dsum.get('top_hours_utc')} | 星期 {[wd[i] for i in dsum.get('top_wdays',[])]}")
    print("=" * 60)

    out = {"window": WINDOW, "pump_th": PUMP_TH, "dump_th": DUMP_TH,
           "pump_summary": psum, "dump_summary": dsum, "per_coin": per_coin}
    (HIST / "pump_analysis.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"已存 {HIST / 'pump_analysis.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
