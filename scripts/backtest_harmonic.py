"""谐波形态历史精确度验证（**因果前向**回测，诚实，非投资建议）。

⚠️ 为什么不用「完整形态」回测：完整形态的 D 是**已确认的摆动点**（其定义即价格在此反转），
   「在确认反转点入场看是否反转」是循环论证 → 虚高胜率(~91%)，非预测精度。

✅ 因果方法（与生产「成形前瞻」一致）：
   - 用每组 XABC（仅过去 4 个已确认枢轴，D 未知）经 project_prz 投射 PRZ。
   - 入场时点 = C 确认后（C_idx+order），等价格**前向进入** PRZ 才入场（D 真未知）。
   - stop = X 失效位，target = entry ± rr·risk。前向 walk 看先止盈还是止损。
   - 这避免了 look-ahead：入场前不知道 D 是不是摆动点。

诚实局限：小样本/无手续费滑点/单一固定出场规则/未含资金费。仅供算法自检，非投资建议。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.bitget.rest import BitgetREST
from smc_tracker.indicators.harmonic import find_pivots, project_prz

COINS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "BNB": "BNBUSDT",
         "XRP": "XRPUSDT", "DOGE": "DOGEUSDT"}
TFS = ["1H", "4H", "1D"]
ORDER = 3
TOL = 0.05
RR = 2.0
MAX_WAIT = 40         # 等价格进入 PRZ 的最大前向根数
MAX_HOLD = 60         # 入场后判定窗口
MIN_STOP_PCT = 0.002
MAX_STOP_PCT = 0.08


def simulate(candles, x_idx, x_px, a_px, b_px, c_idx, c_px, pat):
    """因果前向：C 确认后等价格进 PRZ 入场，再 walk 至止盈/止损。

    返回 'win'/'loss'/'no_entry'/None(劣质)。
    """
    direction = pat["direction"]
    prz_lo, prz_hi = pat["prz"]
    if prz_hi <= prz_lo:
        return None
    start = c_idx + ORDER + 1                  # C 确认后才可行动（因果）
    entry = None
    entry_j = None
    # 1) 等价格前向进入 PRZ
    for j in range(start, min(start + MAX_WAIT, len(candles))):
        c = candles[j]
        if c.l <= prz_hi and c.h >= prz_lo:    # 价格触及 PRZ
            entry = (prz_lo + prz_hi) / 2.0    # 假设 PRZ 中点成交
            entry_j = j
            break
    if entry is None:
        return "no_entry"
    # 2) stop=X 失效位, target=rr
    if direction == "bull":
        stop = x_px
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + RR * risk
    else:
        stop = x_px
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - RR * risk
    sp = abs(entry - stop) / entry if entry else 1.0
    if sp < MIN_STOP_PCT or sp > MAX_STOP_PCT:
        return None
    # 3) 入场后前向判定
    for j in range(entry_j + 1, min(entry_j + 1 + MAX_HOLD, len(candles))):
        c = candles[j]
        if direction == "bull":
            if c.l <= stop:
                return "loss"
            if c.h >= target:
                return "win"
        else:
            if c.h >= stop:
                return "loss"
            if c.l <= target:
                return "win"
    return None  # 未判定（持有超时）


async def main():
    tally: dict[str, dict[str, int]] = {}
    overall = {"win": 0, "loss": 0}
    by_dir = {"bull": {"win": 0, "loss": 0}, "bear": {"win": 0, "loss": 0}}
    no_entry = 0
    async with BitgetREST() as bg:
        for coin, sym in COINS.items():
            for tf in TFS:
                try:
                    candles = await bg.klines(sym, tf, bars=1000, coin=coin)
                except Exception as e:
                    print(f"  {coin}/{tf} 拉取失败: {e}")
                    continue
                pivots = find_pivots(candles, order=ORDER)
                if len(pivots) < 4:
                    continue
                # 遍历每组连续 XABC（仅用过去枢轴）
                for k in range(3, len(pivots)):
                    X_idx, X_px, _ = pivots[k - 3]
                    A_idx, A_px, _ = pivots[k - 2]
                    B_idx, B_px, _ = pivots[k - 1]
                    C_idx, C_px, _ = pivots[k]
                    direction = "bull" if A_px > X_px else "bear"
                    # 结构次序（与生产 detect 一致）
                    if direction == "bull" and not (X_px < B_px < C_px < A_px):
                        continue
                    if direction == "bear" and not (X_px > B_px > C_px > A_px):
                        continue
                    forming = project_prz(X_px, A_px, B_px, C_px, direction, tol=TOL)
                    if not forming:
                        continue
                    pat = forming[0]   # 最高置信
                    res = simulate(candles, X_idx, X_px, A_px, B_px, C_idx, C_px, pat)
                    if res is None:
                        continue
                    if res == "no_entry":
                        no_entry += 1
                        continue
                    p = pat["pattern"]
                    tally.setdefault(p, {"win": 0, "loss": 0})[res] += 1
                    overall[res] += 1
                    by_dir[direction][res] += 1

    print("=" * 66)
    print(f"谐波【成形前瞻】因果前向精确度（{len(COINS)}币×{len(TFS)}周期×1000根, RR={RR}）")
    print("=" * 66)

    def line(name, w, l):
        n = w + l
        wr = 100 * w / n if n else 0.0
        return f"  {name:12} 命中 {w:3}/{n:3}  胜率 {wr:5.1f}%"

    print("【按形态】")
    for p in ["Gartley", "Bat", "Butterfly", "Crab"]:
        t = tally.get(p)
        if t:
            note = "  ⚠Crab(预期偏低)" if p == "Crab" else ""
            print(line(p, t["win"], t["loss"]) + note)
    print("【按方向】")
    print(line("看多(bull)", by_dir["bull"]["win"], by_dir["bull"]["loss"]))
    print(line("看空(bear)", by_dir["bear"]["win"], by_dir["bear"]["loss"]))
    print("【总计】")
    print(line("ALL", overall["win"], overall["loss"]))
    n = overall["win"] + overall["loss"]
    be = 100 / (1 + RR)
    print(f"\n成交 {n} 笔 + 未进场 {no_entry} 笔。RR={RR} 盈亏平衡胜率={be:.0f}%。")
    if n:
        wr = 100 * overall["win"] / n
        edge = "有正期望" if wr > be else "无正期望(≤盈亏平衡)"
        print(f"总胜率 {wr:.1f}% vs 平衡 {be:.0f}% → {edge}。")
    print("诚实: 小样本/无手续费滑点/固定出场; 文献独立回测谐波 53-66%。仅算法自检, 非投资建议。")


asyncio.run(main())
