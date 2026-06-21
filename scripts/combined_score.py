#!/usr/bin/env python3
"""Combined omen score + two-condition interactions + precision/recall + de-duplicated event recall."""
import numpy as np
agg = np.load("/tmp/_agg.npy", allow_pickle=True).item()
pump=agg['pump']; dump=agg['dump']; rsi=agg['rsi']; vol_x=agg['vol_x']
pre_range=agg['pre_range']; ret24=agg['ret24']; ret72=agg['ret72']
atr=agg['atr']; dist_hi7=agg['dist_hi7']; hours=agg['hours']
fmg=agg['fut_max_gain']; fmd=agg['fut_max_dd']

bm=~np.isnan(pump)
base_p=np.nanmean(pump[bm]); base_d=np.nanmean(dump[bm])

def stats(cond, target, base, label):
    m=bm&cond
    n=int(m.sum())
    if n<30:
        print(f"{label:55s} n={n:6d}  (too few)"); return
    tp=int(np.nansum(target[m]))
    rate=tp/n
    total_pos=int(np.nansum(target[bm]))
    recall=tp/total_pos
    print(f"{label:55s} n={n:6d} hit={rate*100:6.2f}% lift={rate/base:5.2f}x recall={recall*100:5.1f}%")

print(f"BASE pump={base_p*100:.3f}%  dump={base_d*100:.3f}%")
print("\n===== TWO-WAY INTERACTIONS — PUMP (momentum thesis) =====")
stats((ret24>0.10)&(vol_x>1.5), pump, base_p, "ret24>10% AND vol_x>1.5")
stats((ret24>0.10)&(rsi>65), pump, base_p, "ret24>10% AND RSI>65")
stats((ret24>0.20)&(vol_x>2.0), pump, base_p, "ret24>20% AND vol_x>2.0")
stats((rsi>70)&(vol_x>1.5), pump, base_p, "RSI>70 AND vol_x>1.5")
stats((rsi>70)&(ret24>0.10), pump, base_p, "RSI>70 AND ret24>10%")
stats((pre_range>0.25)&(vol_x>1.5), pump, base_p, "pre_range>25% AND vol_x>1.5")
stats((pre_range>0.25)&(ret24>0.10), pump, base_p, "pre_range>25% AND ret24>10%")
stats((ret72<-0.20)&(ret24>0.05)&(vol_x>1.2), pump, base_p, "3d dump>20% + 24h bounce>5% + vol>1.2 (reversal)")
stats((ret72<-0.20)&(rsi<40), pump, base_p, "3d dump>20% AND RSI<40 (capitulation bounce)")
stats((dist_hi7>-0.05)&(ret24>0.10), pump, base_p, "near 7d high AND ret24>10% (breakout)")

print("\n===== TWO-WAY INTERACTIONS — DUMP =====")
stats((ret24>0.30)&(rsi>70), dump, base_d, "ret24>30% AND RSI>70 (overextended)")
stats((ret24>0.30)&(vol_x>2.0), dump, base_d, "ret24>30% AND vol_x>2.0")
stats((ret72>0.50)&(rsi>70), dump, base_d, "3d pump>50% AND RSI>70")
stats((ret24>0.50), dump, base_d, "ret24>50% (vertical, any)")
stats((rsi<30)&(vol_x>1.5), dump, base_d, "RSI<30 AND vol_x>1.5 (breakdown accel)")
stats((rsi<35)&(ret24<-0.15), dump, base_d, "RSI<35 AND ret24<-15% (downtrend continuation)")
stats((pre_range>0.30)&(rsi>65), dump, base_d, "pre_range>30% AND RSI>65")
stats((atr>0.05)&(ret24>0.20), dump, base_d, "atr>5% AND ret24>20%")

# ---------- COMBINED OMEN SCORE (PUMP) ----------
print("\n===== COMBINED PUMP OMEN SCORE (additive points) =====")
def safe(x):
    y=x.copy(); y[np.isnan(y)]=0; return y
# points
pscore = np.zeros(len(pump))
pscore += 2*((vol_x>2.5).astype(float))      # strong vol expansion
pscore += 1*(((vol_x>1.5)&(vol_x<=2.5)).astype(float))
pscore += 2*((ret24>0.20).astype(float))     # strong momentum
pscore += 1*(((ret24>0.10)&(ret24<=0.20)).astype(float))
pscore += 2*((rsi>70).astype(float))         # momentum RSI
pscore += 1*(((rsi>=63)&(rsi<=70)).astype(float))
pscore += 2*((pre_range>0.30).astype(float)) # already-volatile regime
pscore += 1*(((pre_range>0.18)&(pre_range<=0.30)).astype(float))
pscore += 1*((ret72<-0.25).astype(float))    # deep prior dump (reversal kicker)
pscore += 1*((dist_hi7>-0.05).astype(float)) # near 7d high breakout
# zero out where features nan-driven (vol_x/ret24 may be nan at start)
valid = bm & ~np.isnan(vol_x) & ~np.isnan(ret24) & ~np.isnan(pre_range) & ~np.isnan(ret72)
for thr in [3,4,5,6,7]:
    m=valid&(pscore>=thr)
    n=int(m.sum())
    if n<30: print(f"score>={thr}: n={n} too few"); continue
    tp=int(np.nansum(pump[m])); rate=tp/n
    recall=tp/int(np.nansum(pump[valid]))
    print(f"pump_score>={thr}: n={n:6d} hit={rate*100:6.2f}% lift={rate/base_p:5.2f}x recall={recall*100:5.1f}%")

# ---------- COMBINED OMEN SCORE (DUMP) ----------
print("\n===== COMBINED DUMP OMEN SCORE (additive points) =====")
dscore=np.zeros(len(dump))
dscore += 2*((ret24>0.40).astype(float))
dscore += 1*(((ret24>0.20)&(ret24<=0.40)).astype(float))
dscore += 2*((ret72>0.80).astype(float))
dscore += 1*(((ret72>0.40)&(ret72<=0.80)).astype(float))
dscore += 2*((pre_range>0.35).astype(float))
dscore += 1*(((pre_range>0.22)&(pre_range<=0.35)).astype(float))
dscore += 2*((atr>0.05).astype(float))
dscore += 1*(((atr>0.035)&(atr<=0.05)).astype(float))
dscore += 1*((rsi>72).astype(float))
dscore += 1*((rsi<30).astype(float))  # capitulation accel (separate regime)
validd=bm & ~np.isnan(ret24)&~np.isnan(ret72)&~np.isnan(pre_range)&~np.isnan(atr)
for thr in [3,4,5,6]:
    m=validd&(dscore>=thr)
    n=int(m.sum())
    if n<30: print(f"score>={thr}: n={n} too few"); continue
    tp=int(np.nansum(dump[m])); rate=tp/n
    recall=tp/int(np.nansum(dump[validd]))
    print(f"dump_score>={thr}: n={n:6d} hit={rate*100:6.2f}% lift={rate/base_d:5.2f}x recall={recall*100:5.1f}%")

# ---- De-dup EVENT-level recall: cluster consecutive pump==1 bars into events ----
def event_recall(target, sig, base_mask):
    # event = maximal run of target==1 separated by >=WINDOW gaps; here simpler: contiguous runs
    t=np.where(np.isnan(target),0,target).astype(int)
    events=[]; i=0; Nn=len(t)
    while i<Nn:
        if t[i]==1:
            j=i
            while j<Nn and t[j]==1: j+=1
            events.append((i,j)); i=j
        else: i+=1
    caught=0
    for (a,b) in events:
        # caught if signal fired somewhere within the event window OR up to 24 bars before its start
        lo=max(0,a-24)
        if sig[lo:b].any(): caught+=1
    return caught, len(events)

print("\n===== EVENT-LEVEL RECALL (pump_score>=5 fired within event or 24h prior) =====")
sigp=valid&(pscore>=5)
c,e=event_recall(pump, sigp, bm)
print(f"pump events={e} caught={c} ({c/e*100:.1f}%)")
sigp4=valid&(pscore>=4)
c,e=event_recall(pump, sigp4, bm)
print(f"pump events caught at score>=4: {c}/{e} ({c/e*100:.1f}%)")
sigd=validd&(dscore>=4)
c,e=event_recall(dump, sigd, bm)
print(f"dump events={e} caught at score>=4={c} ({c/e*100:.1f}%)")
