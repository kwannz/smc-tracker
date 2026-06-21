#!/usr/bin/env python3
"""Predictability dimension: can we catch pumps/dumps in advance?
Builds a pre-event "omen score" and backtests hit-rate vs base rate.
"""
import csv, glob, os, math
import numpy as np

DATA_DIR = "/Volumes/ROG ESD-S1C Media/smc/data/history"
WINDOW = 24          # forward window (bars) for labeling
PUMP_TH = 0.50       # +50% future max gain
DUMP_TH = -0.30      # -30% future max drawdown

def load(path):
    ts, o, h, l, c, bv, qv = [], [], [], [], [], [], []
    with open(path) as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) < 7: continue
            try:
                ts.append(int(row[0])); o.append(float(row[1])); h.append(float(row[2]))
                l.append(float(row[3])); c.append(float(row[4]))
                bv.append(float(row[5])); qv.append(float(row[6]))
            except: continue
    return (np.array(ts), np.array(o), np.array(h), np.array(l),
            np.array(c), np.array(bv), np.array(qv))

def rsi(close, n=14):
    d = np.diff(close, prepend=close[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    ru = np.zeros_like(close); rd = np.zeros_like(close)
    # Wilder smoothing
    if len(close) <= n:
        return np.full_like(close, 50.0)
    ru[n] = up[1:n+1].mean(); rd[n] = dn[1:n+1].mean()
    for i in range(n+1, len(close)):
        ru[i] = (ru[i-1]*(n-1) + up[i]) / n
        rd[i] = (rd[i-1]*(n-1) + dn[i]) / n
    rs = np.where(rd == 0, 100.0, ru / np.where(rd==0, 1, rd))
    out = 100 - 100/(1+rs)
    out[:n] = 50.0
    return out

def build(path):
    ts, o, h, l, c, bv, qv = load(path)
    N = len(c)
    if N < 100: return None
    # forward labels
    fut_max_gain = np.full(N, np.nan)
    fut_max_dd   = np.full(N, np.nan)
    for i in range(N):
        j = min(i+1+WINDOW, N)
        if i+1 >= N: break
        seg_h = h[i+1:j]; seg_l = l[i+1:j]
        if len(seg_h)==0: continue
        fut_max_gain[i] = seg_h.max()/c[i] - 1.0
        fut_max_dd[i]   = seg_l.min()/c[i] - 1.0
    pump = (fut_max_gain >= PUMP_TH).astype(float)
    dump = (fut_max_dd  <= DUMP_TH).astype(float)
    pump[np.isnan(fut_max_gain)] = np.nan
    dump[np.isnan(fut_max_dd)]   = np.nan

    # ---- features ----
    R = rsi(c, 14)
    # rolling 24h volume mean (quote vol) and current/mean ratio
    vol = qv.copy()
    vol_ma = np.full(N, np.nan)
    for i in range(N):
        a = max(0, i-23)
        vol_ma[i] = vol[a:i+1].mean()
    # longer baseline 7d (168h) vol for "quiet" detection
    vol_ma168 = np.full(N, np.nan)
    for i in range(N):
        a = max(0, i-167)
        vol_ma168[i] = vol[a:i+1].mean()
    vol_x = np.where(vol_ma168>0, vol_ma/vol_ma168, np.nan)  # recent24 vs 7d => <1 = quiet/contracting

    # prior 24h realized range (consolidation tightness): (maxH-minL)/close
    pre_range = np.full(N, np.nan)
    for i in range(N):
        a = max(0, i-23)
        pre_range[i] = (h[a:i+1].max()-l[a:i+1].min())/c[i]
    # prior 24h return
    ret24 = np.full(N, np.nan)
    for i in range(N):
        if i-24 >= 0:
            ret24[i] = c[i]/c[i-24]-1.0
    # prior 72h return (3d) for "prior dump" candidate
    ret72 = np.full(N, np.nan)
    for i in range(N):
        if i-72 >= 0:
            ret72[i] = c[i]/c[i-72]-1.0
    # ATR-like: avg true range over 24h / close (volatility)
    tr = np.maximum(h-l, np.maximum(np.abs(h-np.roll(c,1)), np.abs(l-np.roll(c,1))))
    tr[0] = h[0]-l[0]
    atr = np.full(N, np.nan)
    for i in range(N):
        a = max(0, i-23)
        atr[i] = tr[a:i+1].mean()/c[i]
    # distance from 7d high (how compressed near top)
    dist_hi7 = np.full(N, np.nan)
    for i in range(N):
        a = max(0, i-167)
        hh = h[a:i+1].max()
        dist_hi7[i] = c[i]/hh - 1.0  # 0 = at high, negative = below
    # hour of day (UTC)
    hours = ((ts//1000)//3600) % 24

    return dict(ts=ts, c=c, N=N, pump=pump, dump=dump,
                fut_max_gain=fut_max_gain, fut_max_dd=fut_max_dd,
                rsi=R, vol_x=vol_x, pre_range=pre_range, ret24=ret24,
                ret72=ret72, atr=atr, dist_hi7=dist_hi7, hours=hours)

# Aggregate all coins into stacked arrays
files = sorted(glob.glob(os.path.join(DATA_DIR, "*_1H.csv")))
cols = ['pump','dump','rsi','vol_x','pre_range','ret24','ret72','atr','dist_hi7','hours',
        'fut_max_gain','fut_max_dd']
agg = {k: [] for k in cols}
percoin = {}
for fp in files:
    name = os.path.basename(fp).replace('_1H.csv','')
    d = build(fp)
    if d is None: continue
    for k in cols:
        agg[k].append(d[k])
    percoin[name] = d
    print(f"loaded {name} N={d['N']}")

for k in cols:
    agg[k] = np.concatenate(agg[k])

# valid mask = labels present and features present
def vmask(extra_keys):
    m = ~np.isnan(agg['pump'])
    for k in extra_keys:
        m &= ~np.isnan(agg[k])
    return m

np.save("/tmp/_agg.npy", agg, allow_pickle=True)
print("TOTAL bars:", len(agg['pump']))
mP = ~np.isnan(agg['pump'])
print("base pump rate:", round(np.nanmean(agg['pump'][mP])*100,3), "% n=", int(mP.sum()))
print("base dump rate:", round(np.nanmean(agg['dump'][mP])*100,3), "%")
