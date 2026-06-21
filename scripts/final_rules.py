#!/usr/bin/env python3
"""Final rule cards + dump actionability + per-coin robustness + reconcile event counts."""
import numpy as np, glob, os
agg=np.load("/tmp/_agg.npy",allow_pickle=True).item()
pump=agg['pump']; dump=agg['dump']; rsi=agg['rsi']; vol_x=agg['vol_x']
pre_range=agg['pre_range']; ret24=agg['ret24']; ret72=agg['ret72']
atr=agg['atr']; dist_hi7=agg['dist_hi7']; hours=agg['hours']
fmg=agg['fut_max_gain']; fmd=agg['fut_max_dd']
bm=~np.isnan(pump); base_p=np.nanmean(pump[bm]); base_d=np.nanmean(dump[bm])

files=sorted(glob.glob("/Volumes/ROG ESD-S1C Media/smc/data/history/*_1H.csv"))
offsets=[]; off=0
for fp in files:
    with open(fp) as f: n=sum(1 for _ in f)-1
    offsets.append((os.path.basename(fp).replace('_1H.csv',''),off,off+n)); off+=n

# reconcile pump count with per-bar labels (raw bar count where pump==1)
print(f"raw pump==1 bars={int(np.nansum(pump))}  raw dump==1 bars={int(np.nansum(dump))}")
print(f"(pump_analysis.json reported 1062 pumps/1597 dumps as de-dup EVENTS w/ their own gap rule)\n")

def card(name, cond, target, base):
    m=bm&cond; n=int(m.sum())
    if n<30: print(f"  {name}: n={n} too few"); return
    tp=int(np.nansum(target[m])); rate=tp/n
    rec=tp/int(np.nansum(target[bm]))
    mg=np.nanmedian(fmg[m])*100; dd=np.nanmedian(fmd[m])*100
    print(f"  {name}\n    fires {n} bars | hit={rate*100:.2f}% (base {base*100:.3f}%, {rate/base:.1f}x) | recall={rec*100:.1f}% | med fwd gain {mg:+.0f}% / med fwd dd {dd:+.0f}%")

print("===== FINAL PUMP RULE CARDS =====")
card("P1 BREAKOUT-MOMENTUM: ret24>20% AND vol_x>2.0", (ret24>0.20)&(vol_x>2.0), pump, base_p)
card("P2 RSI-THRUST: RSI>70 AND ret24>10%", (rsi>0.70*100)&(ret24>0.10), pump, base_p)
card("P3 VOL-RANGE-EXPANSION: pre_range>25% AND vol_x>1.5", (pre_range>0.25)&(vol_x>1.5), pump, base_p)
card("P4 STRICT (all 3): ret24>15% AND vol_x>1.8 AND RSI>62", (ret24>0.15)&(vol_x>1.8)&(rsi>62), pump, base_p)
card("P5 score>=6 (composite)", composite:=None or ( # placeholder built below
    (2*((vol_x>2.5))+1*(((vol_x>1.5)&(vol_x<=2.5)))+2*((ret24>0.20))+1*(((ret24>0.10)&(ret24<=0.20)))
     +2*((rsi>70))+1*(((rsi>=63)&(rsi<=70)))+2*((pre_range>0.30))+1*(((pre_range>0.18)&(pre_range<=0.30)))
     +1*((ret72<-0.25))+1*((dist_hi7>-0.05)))>=6 ), pump, base_p)

print("\n===== FINAL DUMP RULE CARDS =====")
card("D1 VERTICAL-EXHAUSTION: ret24>50%", ret24>0.50, dump, base_d)
card("D2 OVEREXTENDED: ret24>30% AND RSI>70", (ret24>0.30)&(rsi>70), dump, base_d)
card("D3 PARABOLIC-VOL: atr>5% AND ret24>20%", (atr>0.05)&(ret24>0.20), dump, base_d)
card("D4 DOWNTREND-CONT: RSI<35 AND ret24<-15%", (rsi<35)&(ret24<-0.15), dump, base_d)
card("D5 HIGH-VOL-TOP: pre_range>30% AND RSI>65", (pre_range>0.30)&(rsi>65), dump, base_d)

# Dump actionability: among D1/D2 signals that DID dump, remaining downside
md=bm&((ret24>0.30)&(rsi>70))&(dump==1)
if md.sum()>10:
    print(f"\n== DUMP ACTIONABILITY (D2 true-positives, n={int(md.sum())}): median fwd max dd={np.nanmedian(fmd[md])*100:.0f}% (downside still available)")

# Per-coin robustness of best pump rule (P4)
print("\n===== PER-COIN ROBUSTNESS of P4 (ret24>15% & vol_x>1.8 & RSI>62) =====")
condP4=(ret24>0.15)&(vol_x>1.8)&(rsi>62)
rows=[]
for name,a,b in offsets:
    seg=bm[a:b]&condP4[a:b]; n=int(seg.sum())
    if n<10: continue
    tp=int(np.nansum(pump[a:b][seg])); rate=tp/n
    bcoin=np.nanmean(pump[a:b][bm[a:b]]) if bm[a:b].sum()>0 else 0
    rows.append((name,n,rate*100, (rate/bcoin if bcoin>0 else 0)))
rows.sort(key=lambda x:-x[1])
pos=sum(1 for r in rows if r[3]>1)
print(f"  coins where P4 lift>1x: {pos}/{len(rows)}")
for name,n,rate,lf in rows[:12]:
    print(f"    {name:10s} n={n:4d} hit={rate:5.2f}% lift={lf:.1f}x")

# Cost of false positives: when score>=5 fires, what's median fwd dd (downside risk if you long)
ps=(2*((vol_x>2.5))+1*(((vol_x>1.5)&(vol_x<=2.5)))+2*((ret24>0.20))+1*(((ret24>0.10)&(ret24<=0.20)))
    +2*((rsi>70))+1*(((rsi>=63)&(rsi<=70)))+2*((pre_range>0.30))+1*(((pre_range>0.18)&(pre_range<=0.30)))
    +1*((ret72<-0.25))+1*((dist_hi7>-0.05)))
valid=bm&~np.isnan(vol_x)&~np.isnan(ret24)&~np.isnan(pre_range)&~np.isnan(ret72)
m5=valid&(ps>=5)
# simple long expectancy: enter at close, exit best-case +30% TP or worst -15% SL via fwd paths (approx using fmg/fmd)
tp_hit=(fmg[m5]>=0.30); sl_hit=(fmd[m5]<=-0.15)
# assume if both, SL first conservatively
win=tp_hit&~sl_hit
print(f"\n== Naive long@score>=5, TP+30%/SL-15% (SL-priority): win={np.nanmean(win)*100:.1f}% of {int(m5.sum())} | P(TP+30 reached)={np.nanmean(tp_hit)*100:.1f}% P(SL-15 hit)={np.nanmean(sl_hit)*100:.1f}%")
