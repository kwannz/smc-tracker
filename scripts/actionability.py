#!/usr/bin/env python3
"""Actionability & leakage checks:
 1) When pump_score>=5 fires & a pump follows, how much upside remains AFTER the signal bar?
 2) Proper de-dup event recall (events separated by >=24 quiet bars).
 3) Out-of-sample: build thresholds on half the coins, test on the other half.
 4) Forward-return distribution of high-score bars (expectancy, not just hit-rate).
"""
import numpy as np, glob, os, csv
agg = np.load("/tmp/_agg.npy", allow_pickle=True).item()
pump=agg['pump']; dump=agg['dump']; rsi=agg['rsi']; vol_x=agg['vol_x']
pre_range=agg['pre_range']; ret24=agg['ret24']; ret72=agg['ret72']
atr=agg['atr']; dist_hi7=agg['dist_hi7']; hours=agg['hours']
fmg=agg['fut_max_gain']; fmd=agg['fut_max_dd']
bm=~np.isnan(pump)
base_p=np.nanmean(pump[bm]); base_d=np.nanmean(dump[bm])

def pump_score_arr():
    s=np.zeros(len(pump))
    s+=2*((vol_x>2.5).astype(float)); s+=1*(((vol_x>1.5)&(vol_x<=2.5)).astype(float))
    s+=2*((ret24>0.20).astype(float)); s+=1*(((ret24>0.10)&(ret24<=0.20)).astype(float))
    s+=2*((rsi>70).astype(float)); s+=1*(((rsi>=63)&(rsi<=70)).astype(float))
    s+=2*((pre_range>0.30).astype(float)); s+=1*(((pre_range>0.18)&(pre_range<=0.30)).astype(float))
    s+=1*((ret72<-0.25).astype(float)); s+=1*((dist_hi7>-0.05).astype(float))
    return s
ps=pump_score_arr()
valid=bm&~np.isnan(vol_x)&~np.isnan(ret24)&~np.isnan(pre_range)&~np.isnan(ret72)

# 1) Upside remaining after signal among true-positive signals (score>=5)
m=valid&(ps>=5)&(pump==1)
print("== ACTIONABILITY: among score>=5 signals that DID pump (+50% in 24h) ==")
print(f"   count={int(m.sum())}")
print(f"   median fwd max gain = {np.nanmedian(fmg[m])*100:.1f}%  (this is upside still available AFTER signal bar)")
print(f"   25/50/75 pct fwd gain = {np.nanpercentile(fmg[m],25)*100:.0f}% / {np.nanpercentile(fmg[m],50)*100:.0f}% / {np.nanpercentile(fmg[m],75)*100:.0f}%")

# Expectancy: ALL score>=5 bars, forward max gain & drawdown distribution
ma=valid&(ps>=5)
print("\n== EXPECTANCY: ALL score>=5 bars (whether or not they hit +50%) ==")
print(f"   n={int(ma.sum())}")
print(f"   mean fwd max gain={np.nanmean(fmg[ma])*100:.1f}%  median={np.nanmedian(fmg[ma])*100:.1f}%")
print(f"   mean fwd max dd  ={np.nanmean(fmd[ma])*100:.1f}%  median={np.nanmedian(fmd[ma])*100:.1f}%")
print(f"   P(fwd gain>=20%)={np.nanmean((fmg[ma]>=0.20))*100:.1f}%  P(fwd dd<=-20%)={np.nanmean((fmd[ma]<=-0.20))*100:.1f}%")
# baseline for comparison
print(f"   [baseline all bars] mean fwd max gain={np.nanmean(fmg[bm])*100:.1f}% median={np.nanmedian(fmg[bm])*100:.1f}%")

# 2) Proper de-dup events: collapse pump labels into events separated by >=24 quiet bars, per coin
def load_coin_boundaries():
    files=sorted(glob.glob("/Volumes/ROG ESD-S1C Media/smc/data/history/*_1H.csv"))
    lens=[]
    for fp in files:
        with open(fp) as f:
            n=sum(1 for _ in f)-1
        lens.append((os.path.basename(fp).replace('_1H.csv',''),n))
    return lens
lens=load_coin_boundaries()
# rebuild offsets matching aggregation order (sorted glob == same order)
offsets=[]; off=0
for name,n in lens:
    offsets.append((name,off,off+n)); off+=n
assert off==len(pump), f"offset mismatch {off} vs {len(pump)}"

def dedup_events(target_idx_sorted, gap=24):
    ev=[]; last=-10**9; start=None; prev=None
    for i in target_idx_sorted:
        if start is None:
            start=i; prev=i
        elif i-prev<=gap:
            prev=i
        else:
            ev.append((start,prev)); start=i; prev=i
    if start is not None: ev.append((start,prev))
    return ev

def coin_event_recall(score, target, thr):
    tot_ev=0; caught=0
    for name,a,b in offsets:
        seg_t=target[a:b]; seg_s=score[a:b]; seg_v=valid[a:b]
        idx=np.where(np.nan_to_num(seg_t)==1)[0]
        if len(idx)==0: continue
        evs=dedup_events(idx, gap=24)
        for (s,e) in evs:
            tot_ev+=1
            lo=max(0,s-24)
            fired=(seg_v[lo:e+1]&(seg_s[lo:e+1]>=thr)).any()
            if fired: caught+=1
    return caught, tot_ev

for thr in [4,5,6]:
    c,e=coin_event_recall(ps,pump,thr)
    print(f"\n[DEDUP24 EVENT RECALL] pump_score>={thr}: caught {c}/{e} events ({c/e*100:.1f}%)  [fired within event or 24h before start]")

# 3) Out-of-sample split by coin
names=[o[0] for o in offsets]
half=set(names[::2])  # train coins
trainmask=np.zeros(len(pump),bool); testmask=np.zeros(len(pump),bool)
for name,a,b in offsets:
    if name in half: trainmask[a:b]=True
    else: testmask[a:b]=True
print("\n== OUT-OF-SAMPLE (odd coins=train threshold, even coins=test) ==")
for thr in [4,5,6]:
    mt=valid&trainmask&(ps>=thr); me=valid&testmask&(ps>=thr)
    rt=np.nanmean(pump[mt]); re_=np.nanmean(pump[me])
    bp_tr=np.nanmean(pump[bm&trainmask]); bp_te=np.nanmean(pump[bm&testmask])
    print(f"  score>={thr}: TRAIN hit={rt*100:.2f}% lift={rt/bp_tr:.2f}x | TEST hit={re_*100:.2f}% lift={re_/bp_te:.2f}x (n_test={int(me.sum())})")
