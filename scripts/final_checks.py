#!/usr/bin/env python3
import numpy as np, glob, os
agg=np.load("/tmp/_agg.npy",allow_pickle=True).item()
pump=agg['pump']; dump=agg['dump']; rsi=agg['rsi']; vol_x=agg['vol_x']
pre_range=agg['pre_range']; ret24=agg['ret24']; ret72=agg['ret72']
atr=agg['atr']; dist_hi7=agg['dist_hi7']; fmg=agg['fut_max_gain']; fmd=agg['fut_max_dd']
bm=~np.isnan(pump); base_p=np.nanmean(pump[bm]); base_d=np.nanmean(dump[bm])

# Myth-bust: tight-consolidation + neutral RSI + quiet vol (the proposed hypothesis)
hyp=(vol_x<0.7)&(rsi>=45)&(rsi<=60)&(pre_range<0.10)
m=bm&hyp
print("=== PROPOSED HYPOTHESIS (quiet vol<0.7x + RSI 45-60 + tight range<10%) ===")
print(f"  n={int(m.sum())} hit={np.nanmean(pump[m])*100:.3f}% vs base {base_p*100:.3f}% -> lift {np.nanmean(pump[m])/base_p:.2f}x  (CALM-BEFORE-STORM IS FALSE)")

# Pump probability by activity decile (vol_x) and momentum decile (ret24)
print("\n=== PUMP RATE BY 24h RETURN BUCKET ===")
for lo,hi,lbl in [(-1,-0.2,"<-20%"),(-0.2,-0.05,"-20..-5%"),(-0.05,0.05,"-5..+5% (flat)"),
                  (0.05,0.10,"+5..10%"),(0.10,0.20,"+10..20%"),(0.20,0.5,"+20..50%"),(0.5,99,">+50%")]:
    c=bm&(ret24>lo)&(ret24<=hi)
    n=int(c.sum())
    if n<50: continue
    print(f"  ret24 {lbl:14s}: n={n:6d} pump={np.nanmean(pump[c])*100:.3f}% ({np.nanmean(pump[c])/base_p:.1f}x)")

print("\n=== DUMP RATE BY 24h RETURN BUCKET ===")
for lo,hi,lbl in [(-1,-0.2,"<-20%"),(-0.2,-0.05,"-20..-5%"),(-0.05,0.05,"flat"),
                  (0.05,0.20,"+5..20%"),(0.20,0.50,"+20..50%"),(0.5,99,">+50%")]:
    c=bm&(ret24>lo)&(ret24<=hi)
    n=int(c.sum())
    if n<50: continue
    print(f"  ret24 {lbl:14s}: n={n:6d} dump={np.nanmean(dump[c])*100:.3f}% ({np.nanmean(dump[c])/base_d:.1f}x)")

# How many pump events have NO warning at all (silent pumps): score<3 on the bar before move start
ps=(2*((vol_x>2.5))+1*(((vol_x>1.5)&(vol_x<=2.5)))+2*((ret24>0.20))+1*(((ret24>0.10)&(ret24<=0.20)))
    +2*((rsi>70))+1*(((rsi>=63)&(rsi<=70)))+2*((pre_range>0.30))+1*(((pre_range>0.18)&(pre_range<=0.30)))
    +1*((ret72<-0.25))+1*((dist_hi7>-0.05)))
files=sorted(glob.glob("/Volumes/ROG ESD-S1C Media/smc/data/history/*_1H.csv"))
offsets=[]; off=0
for fp in files:
    with open(fp) as f: n=sum(1 for _ in f)-1
    offsets.append((os.path.basename(fp).replace('_1H.csv',''),off,off+n)); off+=n
valid=bm&~np.isnan(vol_x)&~np.isnan(ret24)&~np.isnan(pre_range)&~np.isnan(ret72)
def dedup(idx,gap=24):
    ev=[]; s=None;p=None
    for i in idx:
        if s is None: s=i;p=i
        elif i-p<=gap: p=i
        else: ev.append((s,p)); s=i;p=i
    if s is not None: ev.append((s,p))
    return ev
tot=0; silent=0
for name,a,b in offsets:
    idx=np.where(np.nan_to_num(pump[a:b])==1)[0]
    if len(idx)==0: continue
    for s,e in dedup(idx):
        tot+=1
        lo=max(0,s-24)
        maxscore=ps[a:b][lo:e+1].max() if e+1>lo else 0
        if maxscore<3: silent+=1
print(f"\n=== SILENT PUMPS: {silent}/{tot} ({silent/tot*100:.1f}%) pump events had score<3 in the 24h before/during (NO warning) ===")
print(f"=== => max theoretical event recall at score>=3 ~ {(tot-silent)/tot*100:.0f}% ===")
