"""autosearch Round 1: ATR2 多维度汇合是否提升谐波前向胜率(causal)。

方法(与 backtest_harmonic.py 因果前向一致 + 入场点加 ATR2):
  XABC(仅过去枢轴)→project_prz→等价格进入PRZ入场→入场点算 atr2_confirmation(仅过去candles)→
  按 ATR2 bias 与 setup 方向是否同向分桶→stop=X/target=rr 前向walk→胜率。
诚实: 小样本/无手续费滑点/in-sample枢轴; 仅算法自检。
"""
import asyncio, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.bitget.rest import BitgetREST
from smc_tracker.indicators.harmonic import find_pivots, project_prz
from smc_tracker.indicators.atr2_signals import atr2_confirmation

COINS = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","BNB":"BNBUSDT","XRP":"XRPUSDT","DOGE":"DOGEUSDT"}
TFS = ["1H","4H","1D"]
ORDER=3; TOL=0.05; RR=2.0; MAX_WAIT=40; MAX_HOLD=60; MIN_SP=0.002; MAX_SP=0.08


def simulate(candles, x_px, c_idx, pat):
    direction = pat["direction"]
    prz_lo, prz_hi = pat["prz"]
    if prz_hi <= prz_lo:
        return None, None
    start = c_idx + ORDER + 1
    entry = entry_j = None
    for j in range(start, min(start+MAX_WAIT, len(candles))):
        cc = candles[j]
        if cc.l <= prz_hi and cc.h >= prz_lo:
            entry = (prz_lo+prz_hi)/2.0; entry_j = j; break
    if entry is None:
        return "no_entry", None
    if direction == "bull":
        stop = x_px; risk = entry-stop
        if risk<=0: return None,None
        target = entry+RR*risk
    else:
        stop = x_px; risk = stop-entry
        if risk<=0: return None,None
        target = entry-RR*risk
    sp = abs(entry-stop)/entry if entry else 1.0
    if sp<MIN_SP or sp>MAX_SP: return None,None
    # 入场点 ATR2(仅过去 candles[:entry_j+1], 因果)
    a2 = atr2_confirmation(candles[:entry_j+1])
    atr2_bias = a2["bias"] if a2 else "none"
    # 前向判定
    res = None
    for j in range(entry_j+1, min(entry_j+1+MAX_HOLD, len(candles))):
        cc = candles[j]
        if direction=="bull":
            if cc.l<=stop: res="loss"; break
            if cc.h>=target: res="win"; break
        else:
            if cc.h>=stop: res="loss"; break
            if cc.l<=target: res="win"; break
    if res is None: return None,None
    # ATR2 同向? long↔bias long / short↔bias short
    setup_ls = "long" if direction=="bull" else "short"
    if atr2_bias == setup_ls: agree="同向"
    elif atr2_bias == "neutral" or atr2_bias=="none": agree="中性"
    else: agree="反向"
    return res, agree


async def main():
    buckets = collections.defaultdict(lambda:[0,0])  # agree -> [win,total]
    overall=[0,0]; no_entry=0
    async with BitgetREST() as bg:
        for coin,sym in COINS.items():
            for tf in TFS:
                try: candles = await bg.klines(sym,tf,bars=1000,coin=coin)
                except Exception: continue
                pivots = find_pivots(candles, order=ORDER)
                for k in range(3, len(pivots)):
                    X=pivots[k-3]; A=pivots[k-2]; B=pivots[k-1]; C=pivots[k]
                    direction = "bull" if A[1]>X[1] else "bear"
                    if direction=="bull" and not (X[1]<B[1]<C[1]<A[1]): continue
                    if direction=="bear" and not (X[1]>B[1]>C[1]>A[1]): continue
                    forming = project_prz(X[1],A[1],B[1],C[1],direction,tol=TOL)
                    if not forming: continue
                    res, agree = simulate(candles, X[1], C[0], forming[0])
                    if res=="no_entry": no_entry+=1; continue
                    if res is None: continue
                    buckets[agree][1]+=1; overall[1]+=1
                    if res=="win": buckets[agree][0]+=1; overall[0]+=1

    print("="*64)
    print(f"Round1: ATR2 多维度汇合 vs 谐波前向胜率(RR={RR}, 基线单谐波74.1%)")
    print("="*64)
    def wr(w,t): return f"{100*w/t:.1f}%" if t else "—"
    for agree in ["同向","反向","中性"]:
        w,t = buckets[agree]
        print(f"  ATR2{agree}: n={t:4} 胜={w:4} 胜率={wr(w,t)}")
    print(f"  全部(无ATR2过滤): n={overall[1]} 胜率={wr(*overall)} (基线74.1%)")
    print(f"  未进场 {no_entry} 笔")
    # keep/discard
    sw,st = buckets["同向"]
    if st>=50 and st and 100*sw/st >= 77.0:
        print(f"\n→ KEEP: ATR2同向子集 {wr(sw,st)} ≥77% 且样本{st}≥50 → 多维度汇合有效, 接入门槛")
    elif st<30:
        print(f"\n→ DISCARD(样本不足): 同向样本{st}<30 无统计意义")
    else:
        print(f"\n→ DISCARD: 同向{wr(sw,st)} 未达77%门槛, ATR2汇合不显著提升(诚实)")
    print("诚实: 小样本/无手续费滑点/in-sample枢轴; 仅算法自检, 非投资建议。")

asyncio.run(main())
