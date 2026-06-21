#!/usr/bin/env python3
"""Load aggregated features, run single-condition lift analysis + combined omen score backtest."""
import numpy as np
agg = np.load("/tmp/_agg.npy", allow_pickle=True).item()

pump = agg['pump']; dump = agg['dump']
rsi = agg['rsi']; vol_x = agg['vol_x']; pre_range = agg['pre_range']
ret24 = agg['ret24']; ret72 = agg['ret72']; atr = agg['atr']
dist_hi7 = agg['dist_hi7']; hours = agg['hours']

base_mask = ~np.isnan(pump)
N = int(base_mask.sum())
base_p = np.nanmean(pump[base_mask])
base_d = np.nanmean(dump[base_mask])
print(f"== BASE RATES (per-bar, forward {24}h) ==")
print(f"N usable bars = {N}")
print(f"pump base = {base_p*100:.3f}%   dump base = {base_d*100:.3f}%\n")

def lift(cond, target, label):
    m = base_mask & cond & ~np.isnan(cond.astype(float)*0)  # cond already bool
    m = base_mask & cond
    n = int(m.sum())
    if n < 50:
        return f"{label:45s} n={n:6d}  (too few)"
    rate = np.nanmean(target[m])
    base = base_p if target is pump else base_d
    return f"{label:45s} n={n:6d}  hit={rate*100:6.3f}%  lift={rate/base:5.2f}x"

print("===== SINGLE CONDITION LIFT — PUMP =====")
conds_pump = [
    ("vol_x < 0.7 (recent24h vol < 0.7x of 7d)", vol_x < 0.7),
    ("vol_x < 0.5 (deep quiet)", vol_x < 0.5),
    ("vol_x > 1.5 (vol expansion)", vol_x > 1.5),
    ("vol_x > 2.5 (strong vol expansion)", vol_x > 2.5),
    ("RSI 45-60 (neutral)", (rsi>=45)&(rsi<=60)),
    ("RSI 50-65", (rsi>=50)&(rsi<=65)),
    ("RSI > 70 (overbought-momentum)", rsi>70),
    ("RSI < 35 (oversold)", rsi<35),
    ("pre_range < 0.10 (tight 24h consolidation)", pre_range<0.10),
    ("pre_range < 0.06 (very tight)", pre_range<0.06),
    ("pre_range > 0.30 (already volatile)", pre_range>0.30),
    ("ret24 in [-0.05,0.05] (flat 24h)", (ret24>=-0.05)&(ret24<=0.05)),
    ("ret24 > 0.10 (already rising)", ret24>0.10),
    ("ret24 > 0.20 (strong momentum)", ret24>0.20),
    ("ret72 < -0.20 (prior 3d dump >=20%)", ret72<-0.20),
    ("ret72 < -0.35 (prior 3d crash)", ret72<-0.35),
    ("atr < 0.02 (low volatility)", atr<0.02),
    ("dist_hi7 > -0.05 (near 7d high)", dist_hi7>-0.05),
    ("dist_hi7 < -0.40 (deep below 7d high)", dist_hi7<-0.40),
    ("hour in {1,2,12,13,14} (active UTC)", np.isin(hours,[1,2,12,13,14])),
    ("hour in {0,1,2,3}", np.isin(hours,[0,1,2,3])),
]
for lab, c in conds_pump:
    print(lift(c, pump, lab))

print("\n===== SINGLE CONDITION LIFT — DUMP =====")
conds_dump = [
    ("vol_x > 1.5 (vol expansion)", vol_x>1.5),
    ("vol_x > 2.5", vol_x>2.5),
    ("vol_x < 0.6 (quiet)", vol_x<0.6),
    ("RSI > 70 (overbought)", rsi>70),
    ("RSI > 75", rsi>75),
    ("RSI < 40 (already weak)", rsi<40),
    ("RSI < 30 (oversold-capitulation)", rsi<30),
    ("ret24 > 0.30 (parabolic last 24h)", ret24>0.30),
    ("ret24 > 0.50 (vertical)", ret24>0.50),
    ("ret72 > 0.50 (3d pumped >=50%)", ret72>0.50),
    ("ret72 > 1.0 (3d doubled)", ret72>1.0),
    ("pre_range > 0.30 (high vol)", pre_range>0.30),
    ("dist_hi7 > -0.03 (at 7d high)", dist_hi7>-0.03),
    ("atr > 0.06 (high vol)", atr>0.06),
    ("hour in {10,11,13} (UTC)", np.isin(hours,[10,11,13])),
]
for lab, c in conds_dump:
    print(lift(c, dump, lab))
