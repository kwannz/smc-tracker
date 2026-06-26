"""实时波动追踪（专业细节·逐周期）：监控清单币 → 已采集多周期 K 线 → 每周期独立指标。

设计（CLAUDE.md：低延迟 + 模块化扁平 + 极简；用户#：不做共振，每周期各显指标 + PDArray）：
  - vol_metrics：纯 numpy 向量化，单周期 HLC → rv/atr/range/velocity(1阶导)/accel(2阶导)。
  - pdarray：ICT 溢价/折价数组（Premium/Discount Array）——价在 dealing range 的位置（开源 ICT 标准）。
  - VolatilityMonitor：读 store.get_candles（复用已采 K 线，不重拉），**逐周期**算指标并展示，按运动分排序。
  - **诚实标注（信号性质，CLAUDE.md §二）**：本模块全部指标（rv/velocity/accel/vol_ratio/regime/pdarray）
    均为历史 K 线的**回望/同步描述量，非前瞻领先量**——accel 是收盘价二阶有限差分、regime 的"扩张"是
    波动已放大的*确认*而非预测。订单簿挂单意图(l2Book)/OI 速度理论上更领先(本模块不提供)——但
    **#167 实测警示**:聚合聪明钱净流向(corr~0)、OI velocity(corr+0.02)、funding 拥挤反转 标准独立**方向**预测力
    皆近乎为零(异于波动**水平**#153 可测/pump·谐波 #162-165 有 edge)。方向难测是结构性的,勿假设任何单信号强领先。
    压缩("蓄势")同为描述量：**实测压缩多持续低波动(波动聚集)、非预示突破**——#150 真实数据 150 币
    P(未来扩张|当前压缩) 各时间窗 lift≤1(K=5→0.02×、K=80→0.99×)，故"蓄势"是**当前低波动态标签，非买入/突破信号**。
    velocity/方向箭头(🟢↑🔴↓)同为描述量，且实测 **15m 短期均值反转非延续**——#152 真实数据 150 币
    corr(velocity,前向收益)各窗≈−0.07(高正velocity后续收益反为负)，箭头表"刚发生什么"非"将继续"，
    勿据单 tf 方向追涨杀跌(深折价弱反弹见 #151，同属短期反转族)。
    **前瞻价值的不对称(本系统核心结论)——信幅度别信方向**：波动**水平**有真实但**温和短记忆**的持续性
    (#177 null 对照重测纠 #153 偏差：原"扩张后仍扩张90%/lift7.6×/corr0.73"经 null 对照证实**主要是滚动窗重叠机械伪影**——
    打乱 logret 后 null corr 仍 0.711≈observed 0.725、真实增益仅 +0.014≈0；regime 持续超 null 仅 +2.7pp)。
    **诚实量(须分两个不同对象,#177→#178 修矫枉过正)**：
    ① 波动**水平预测**(EWMA→未来 h-bar 平均已实现波动)**有扎实技巧**：corr 0.30(1bar)→0.42(5bar)→0.45(10bar)，
       随视野**上升**(长视野的已实现波动更平滑更可测)——这是系统真实的幅度 edge(GARCH 同理:波动可测、收益不可测)。
    ② 逐 **bar |收益| 记忆**(ARCH 标准自相关,#149)**快速衰减**：lag-1≈0.28→lag-10≈0.05(null≈0=真实但短)。
    EWMA 相对朴素 rv-持续增益小且偏长视野(1bar 略输 −0.01、10bar +0.03，#155 温和一致)。**非"90%续/0.73"(那是窗口伪影#177)**；
    而**方向**(velocity/PD)短期反转不可赌(#150-152)。系统定位=测波动水平(可前瞻 corr~0.4)、非择时方向、非"高持续regime"。
    脚本 scripts/audit_expansion_persistence.py 可复现(扩张持续性 null 对照 + EWMA 预测技巧随视野)。
  - 可比性：velocity/accel/rv 用固定 _VEL_WIN/_RV_WIN 根，**同周期内跨币可比**；5 根在 15m 与 1W 时间跨度
    差 1~2 个数量级，**跨周期幅度不可直接比较**（rv∝√t）。pd_pct 是区间占比 [0,1]，跨周期可比。
    score=max(各周期) 偏向最长周期，仅作"是否在动"粗排，非精确强度。
"""
from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

# 指标窗口（根）：rv/atr 用近 _RV_WIN 根，速度用近 _VEL_WIN 根，PD dealing range 用近 _PD_WIN 根
_RV_WIN = 20
_RV_LONG = 60   # 波动 regime 长窗基线（短窗 σ / 长窗 σ → 压缩/扩张）
_VEL_WIN = 5
_PD_WIN = 60
# 波动 regime 阈值：短/长 σ 比值 < 压缩阈=蓄势(波动收敛,描述当前态;实测多续低波动非预示突破,#150)，
# > 扩张阈=放量(波动已放大,确认非预测)
_SQUEEZE, _EXPAND = 0.7, 1.4
# 运动分权重：近期动量变化量(accel)加权最高，其次速度，波动率辅助（均为回望量）
_W_VEL, _W_ACCEL, _W_RV = 1.0, 1.5, 0.5
# 各周期毫秒跨度（陈旧阈值动态化用；缺失回退 15m）
_TF_MS = {"15m": 900_000, "30m": 1_800_000, "1H": 3_600_000, "4H": 14_400_000,
          "6H": 21_600_000, "12H": 43_200_000, "1D": 86_400_000, "1W": 604_800_000}


_RM_LAMBDA = 0.94   # RiskMetrics EWMA 衰减(J.P.Morgan 行业标准；#156 在 15m 数据校验:λ∈[0.88,0.99] 扫描，
                    # 0.94 对多数币近最优——更低 λ mean corr 略高但仅 45% 币个体更优(均值被离群币拉高),不稳健,故不动)


def ewma_vol(c: Any, lam: float = _RM_LAMBDA) -> float:
    """RiskMetrics EWMA 波动率(%，开源标准)：σ²_t = λ·σ²_{t-1} + (1-λ)·r²_{t-1}。

    近端指数加权，比等权 rv 对新波动更**灵敏**(rv 把 spike 摊薄到 20 根等权)。
    seed=首 ≤20 根对数收益样本方差，其后逐根 λ 衰减更新。<3 根或含 NaN/inf → -1.0 哨兵。

    **本模块经验证的前瞻量(#154/#155/#177)**：波动水平有真实但温和短记忆(#149/#177 |logret| 自相关 lag-1≈0.28、
    lag-10≈0.05;原 #153 的 corr0.73/90%续经 null 对照证实是窗口重叠伪影,真实增益≈0,已纠正)。
    EWMA 在 IGARCH 下 h 步预测=当前 σ，故可读作**近端预期波动水平**——仍是水平非方向(方向短期反转,#152)。
    #155 自我证伪：EWMA 比等权 rv **更准预测未来已实现波动**(150币 corr 0.414 vs rv 0.387、MAE 更小、
    76% 币 EWMA 更优)，改进温和但一致——前瞻宣称有实测背书，非仅"行业标准"。
    #159 泛化:优势在 15m/1H/4H/1D **全周期 better-or-equal、从不更差**(15m+0.029/4H+0.038 最强，1D+0.010 边际
    ≈rv,因稀疏周期种子占比大)，故全周期适用;唯 1D 及更稀疏周期 EW≈σ,优势可忽略。
    """
    cc = np.asarray(c, dtype=float)
    if cc.size < 3 or not np.all(np.isfinite(cc)):
        return -1.0
    cc = np.clip(cc, 1e-12, None)
    r = np.diff(np.log(cc))
    seed_n = min(20, r.size)
    var = float(np.var(r[:seed_n], ddof=0))
    for x in r[seed_n:].tolist():
        var = lam * var + (1.0 - lam) * x * x
    return math.sqrt(var) * 100.0


def _wilder_rma(tr: np.ndarray, n: int) -> float:
    """Wilder RMA 平滑末值=开源标准 ATR(Wilder 1978；TA-Lib/TradingView ta.atr 默认)。

    seed=前 n 根 TR 的 SMA，其后 ATR_t=(ATR_{t-1}·(n-1)+TR_t)/n（含全历史指数衰减权，非仅近 n 根等权
    平均的 SMA-of-TR——后者在近端剧烈期系统性偏高 10~19%，见 #143 交叉验证）。数据不足→退化可用窗 SMA。

    与 indicators/technical.py 的 `_wilder` **有意各持一份**(#144 核实两者均对独立 Wilder 参考 Δ=0)：本函数
    保持 vol_metrics 自包含·纯 numpy·不耦合 indicators/ 层；technical._wilder 是 TA 层 RSI/ADX/ATR 共用版。
    勿盲目消重(跨包耦合得不偿失)；若改平滑法须两处同步。
    """
    sz = tr.size
    if sz == 0:
        return 0.0
    if sz <= n:
        return float(np.mean(tr))
    a = float(np.mean(tr[:n]))
    for t in tr[n:].tolist():
        a = (a * (n - 1) + t) / n
    return a


def vol_metrics(h: Any, l: Any, c: Any, *,
                rv_win: int = _RV_WIN, vel_win: int = _VEL_WIN,
                rv_long_win: int = _RV_LONG) -> dict:
    """单周期 HLC → 波动专业指标（numpy 向量化）。数据 <3 根返回 {}。（open 不参与，故不收）

    返回：rv(已实现波动率=对数收益σ,%)、atr_pct(Wilder ATR/价,%；开源标准 RMA 平滑)、range_pct(当前 bar 区间,%)、
         velocity(近窗%变化=1 阶导)、accel(速度差=2 阶导，前序窗不足时=0 不虚增)、
         vol_ratio(短窗σ/长窗σ)、regime(压缩/扩张/常态=波动状态，回望确认非预测)、
         ewma_vol(RiskMetrics EWMA 预期波动水平，本模块唯一前瞻量，#154)。
    数据含 NaN/inf 时返回 {}（数据质量守卫，避免 NaN 污染排名）。
    """
    c = np.asarray(c, dtype=float)
    n = c.size
    if n < 3 or not np.all(np.isfinite(c)):
        return {}
    cc = np.clip(c, 1e-12, None)                      # 防 log(0)/除 0
    logret = np.diff(np.log(cc))
    rv = float(np.std(logret[-rv_win:], ddof=0)) * 100.0
    rv_long = float(np.std(logret[-rv_long_win:], ddof=0)) * 100.0
    vol_ratio = rv / rv_long if rv_long > 1e-9 else 1.0
    regime = "压缩" if vol_ratio < _SQUEEZE else ("扩张" if vol_ratio > _EXPAND else "常态")
    hi, lo = np.asarray(h, float), np.asarray(l, float)
    prev = cc[:-1]
    tr = np.maximum.reduce([hi[1:] - lo[1:], np.abs(hi[1:] - prev), np.abs(lo[1:] - prev)])
    last = cc[-1]
    atr_pct = float(_wilder_rma(tr, rv_win) / last) * 100.0  # 开源标准 Wilder ATR(非 SMA-of-TR，修#143)
    range_pct = float((hi[-1] - lo[-1]) / last) * 100.0
    k = min(vel_win, n - 1)
    velocity = float((cc[-1] - cc[-1 - k]) / cc[-1 - k]) * 100.0
    # 前序窗不足（n < 2k+1）→ accel=0（"无加速信息"），不退化为 velocity 虚增运动分（修 P1-1）
    if n >= 2 * k + 1:
        vel_prev = float((cc[-1 - k] - cc[-1 - 2 * k]) / cc[-1 - 2 * k]) * 100.0
        accel = velocity - vel_prev
    else:
        accel = 0.0
    return {"rv": rv, "atr_pct": atr_pct, "range_pct": range_pct,
            "velocity": velocity, "accel": accel,
            "vol_ratio": vol_ratio, "regime": regime,
            "vol_pct": vol_percentile(c),    # 历史波动百分位(HVP，-1=数据不足)
            "ewma_vol": ewma_vol(c)}         # RiskMetrics EWMA 预期波动水平(唯一前瞻量,#154)


def pdarray(h: Any, l: Any, c: Any, *, win: int = _PD_WIN, band: float = 0.03) -> dict:
    """ICT 溢价/折价数组（PD Array）：当前价在 dealing range（近 win 根高低）的位置。

    返回：pd_pct∈[0,1]（0=折价极值，0.5=均衡 EQ，1=溢价极值）、pd_zone(溢价/折价/均衡，EQ±band)。
    区间为 0 时归为均衡。

    诚实标注（#151 实测 150 币前向收益）：PD 是**位置描述非买卖信号**，且实测**不对称**——深折价(PD<0.15)
    后有**弱反弹**(+0.36pp/10bar、80%币为正，但样本含幸存者偏差存疑)；深溢价(PD>0.85)后**无下跌**(≈基线)。
    故对称"买区/卖区"框架一半无据(卖区不成立)，措辞统一用中性"区间下/上半段"，勿据 PD 单独反向交易。
    """
    hh = np.asarray(h, float)[-win:]
    ll = np.asarray(l, float)[-win:]
    price = float(np.asarray(c, float)[-1])
    hi, lo = float(np.max(hh)), float(np.min(ll))
    rng = hi - lo
    if rng <= 0:
        return {"pd_pct": 0.5, "pd_zone": "均衡"}
    pd = (price - lo) / rng
    zone = "溢价" if pd > 0.5 + band else ("折价" if pd < 0.5 - band else "均衡")
    return {"pd_pct": pd, "pd_zone": zone}


def vol_percentile(c: Any, *, win: int = _RV_WIN, lookback: int = 120) -> float:
    """历史波动率百分位（HVP，开源 TradingView 思路）：当前滚动 rv 在近 lookback 根历史 rv 分布中的位次。

    返回 ∈[0,1]（1=当前波动处历史最高位=异常剧烈；0=历史最低=极度平静）；
    数据不足(<win+3 根有效 logret)或含 NaN/inf → -1.0 哨兵（不冒充百分位，诚实标注）。
    校准维度：补 rv(绝对值)/vol_ratio(变化方向) 之外的「当前波动 vs 自身历史」相对水平。

    约定（#148 核实）：用 `mean(rvs<=cur)` **≤(含自身)** 百分位秩——故永不返回 0、最小=1/N。
    与 Pine `ta.percentrank` 的**严格<**约定差恰为 1/N(lookback=120 时≈0.008)，属合法约定差异非 bug，
    选 ≤ 因"当前即历史最高"应得 1.0 而非 (N-1)/N。勿因"对 Pine 不为零"误判。
    """
    cc = np.asarray(c, dtype=float)
    if cc.size < win + 3 or not np.all(np.isfinite(cc)):
        return -1.0
    cc = np.clip(cc, 1e-12, None)
    logret = np.diff(np.log(cc))
    n = logret.size
    if n < win + 1:
        return -1.0
    # 滚动 rv 序列（每个窗口末位算 σ）；n≤~120 廉价，直接列表推导
    rvs = np.array([float(np.std(logret[i - win:i], ddof=0)) for i in range(win, n + 1)])
    rvs = rvs[-lookback:]
    if rvs.size < 3:
        return -1.0
    cur = rvs[-1]
    return float(np.mean(rvs <= cur))   # 含自身的百分位秩 ∈[0,1]


def move_score(m: dict) -> float:
    """运动分：|速度|·_W_VEL + |加速度|·_W_ACCEL + 波动率·_W_RV（均为回望量，表当前运动强度非前瞻）。"""
    return (_W_VEL * abs(m.get("velocity", 0.0))
            + _W_ACCEL * abs(m.get("accel", 0.0))
            + _W_RV * m.get("rv", 0.0))


def volatility_highlights(rows: list[dict], *, max_each: int = 5) -> dict:
    """把逐周期矩阵综合成动向摘要（可操作情报）。纯函数。

    - squeeze：处于压缩(蓄势=当前波动收敛；**实测多续低波动，非预示突破**，#150)的 (coin,tf)，按 vol_ratio 升序。
    - expansion：处于扩张(放量启动)的 (coin,tf)，按 |velocity| 降序（最大动量在前）。
    - extreme_pd：PD≤10%(深折价)或≥90%(深溢价)的 (coin,tf)，按偏离 EQ 程度降序。
    """
    sq: list[dict] = []
    ex: list[dict] = []
    epd: list[dict] = []
    for r in rows:
        for tf, m in r.get("by_tf", {}).items():
            rg = m.get("regime")
            if rg == "压缩":
                sq.append({"coin": r["coin"], "tf": tf, "vol_ratio": m.get("vol_ratio", 1.0)})
            elif rg == "扩张":
                ex.append({"coin": r["coin"], "tf": tf, "velocity": m.get("velocity", 0.0)})
            p = m.get("pd_pct", 0.5)
            if p <= 0.1 or p >= 0.9:
                epd.append({"coin": r["coin"], "tf": tf, "pd_pct": p,
                            "pd_zone": m.get("pd_zone", "")})
    sq.sort(key=lambda x: x["vol_ratio"])
    ex.sort(key=lambda x: abs(x["velocity"]), reverse=True)
    epd.sort(key=lambda x: abs(x["pd_pct"] - 0.5), reverse=True)
    return {"squeeze": sq[:max_each], "expansion": ex[:max_each], "extreme_pd": epd[:max_each]}


def market_regime(rows: list[dict]) -> dict:
    """聚合**展示币集**逐周期 regime/PD → 监控集级波动态势（广度/regime）。纯函数。

    诚实(修 #140)：聚合限于传入的 rows(监控清单币或空清单时的近24h最剧烈币)，是**选择性样本**非
    代表性全市场样本——空清单 fallback 下币偏向高波动，故标签用"监控集态势"不夸大全市场代表性。

    返回 {n, regime:{压缩,扩张,常态}, pd:{折价,溢价,均衡}, label}。
    label：主导 regime + 主导 PD（如"蓄势(压缩) 12/21 · 普遍折价(区间下半段) 15/21"）；n=0 时 label=""。
    诚实标注（修 P1-5）：pd_zone 仅是近 60 根区间内的价格位置，**不蕴含超买/超卖(均值回归)语义**——
    价格可在区间下半段持续下行刷新下沿，故用中性"区间下/上半段"措辞。
    """
    rc = {"压缩": 0, "扩张": 0, "常态": 0}
    pc = {"折价": 0, "溢价": 0, "均衡": 0}
    tc = {"倒挂": 0, "平坦": 0, "顺挂": 0}   # 期限结构广度(按币计,非按 cell)
    n = n_term = 0
    for r in rows:
        for _tf, m in r.get("by_tf", {}).items():
            n += 1
            rc[m.get("regime", "常态")] = rc.get(m.get("regime", "常态"), 0) + 1
            pc[m.get("pd_zone", "均衡")] = pc.get(m.get("pd_zone", "均衡"), 0) + 1
        sh = (r.get("term") or {}).get("shape")
        if sh:
            n_term += 1   # 含"缺"——分母计入数据不足的币，不掩盖覆盖缺口(诚实,修 P2)
        if sh in tc:
            tc[sh] += 1
    if n == 0:
        return {"n": 0, "regime": rc, "pd": pc, "term": tc, "label": ""}
    reg_dom = max(rc, key=lambda k: rc[k])
    pd_dom = max(pc, key=lambda k: pc[k])
    reg_lbl = {"压缩": "蓄势(压缩)", "扩张": "放量(扩张)", "常态": "常态"}[reg_dom]
    pd_lbl = {"折价": "普遍折价(区间下半段)", "溢价": "普遍溢价(区间上半段)", "均衡": "均衡"}[pd_dom]
    label = f"{reg_lbl} {rc[reg_dom]}/{n} · {pd_lbl} {pc[pd_dom]}/{n}"
    # 期限结构广度：仅在主导为可操作的倒挂/顺挂时追加(全市场近端是否普遍应激)
    if n_term:
        td = max(tc, key=lambda k: tc[k])
        # tc[td]>0 守卫：全"缺"(tc 全 0)时不追加"0/N币"假广度
        if td != "平坦" and tc[td]:
            tlbl = {"倒挂": "近端应激(期限倒挂)", "顺挂": "远端主导(期限顺挂)"}[td]
            label += f" · {tlbl} {tc[td]}/{n_term}币"
    return {"n": n, "regime": rc, "pd": pc, "term": tc, "label": label}


# 多周期一致性阈值：主导方向占比 ≥ 此值才判明确多/空，否则分歧
_ALIGN_TH = 0.7


def mtf_alignment(by_tf: dict) -> dict:
    """单币跨周期速度一致性（MTF trend alignment）：各周期 velocity 同向，冲突=分歧。纯函数。

    返回 {bias:多/空/分歧, aligned:主导方向周期数, total:非零周期数, score:主导占比[0,1]}。
    score 越接近 1 越一致（多周期方向共识；**回望量，描述当前跨周期一致，非预测未来**）。
    **诚实(#158 实测150币)：多周期一致≠高确信趋势**——加密在 15m~多日各尺度收益**全程反转**
    (corr(过去L收益,未来L收益)各 L∈[5,400]bar 全负、长周期更甚 L400→−0.17)，无动量延续区。
    故"N周期一致"是当前共识描述，**多尺度同向反偏向反转**，勿读作"高确信看多/空"。
    """
    up = down = 0
    for m in by_tf.values():
        v = m.get("velocity", 0.0)
        if v > 0:
            up += 1
        elif v < 0:
            down += 1
    total = up + down
    if total == 0:
        return {"bias": "分歧", "aligned": 0, "total": 0, "score": 0.0}
    dominant = max(up, down)
    score = dominant / total
    if score >= _ALIGN_TH:
        # score>=0.7 时 up==down 不可能(那样 score=0.5)，故 up>down 必为多、否则空（nit-1）
        bias = "多" if up > down else "空"
    else:
        bias = "分歧"
    return {"bias": bias, "aligned": dominant, "total": total, "score": score}


def coin_vol_state(by_tf: dict) -> str:
    """把单币多周期指标(regime/HVP/PD)合成一个可操作的一词状态(决策级,非新计算)。纯函数。

    优先级(进行中最优先)：🔶放量(任一周期扩张且 HVP≥0.7) > 🔥高位剧烈(多数周期 HVP≥0.9)
    > 🔸蓄势(多数压缩) > 深折价/深溢价(多数周期 PD 极端) > 常态。
    """
    ms = list(by_tf.values())
    n = len(ms)
    if n == 0:
        return "常态"
    half = (n + 1) // 2   # 过半
    expansion = [m for m in ms if m.get("regime") == "扩张"]
    if expansion and any(m.get("vol_pct", -1.0) >= 0.7 for m in expansion):
        return "🔶放量"
    if sum(1 for m in ms if m.get("vol_pct", -1.0) >= 0.9) >= half:
        return "🔥高位剧烈"
    if sum(1 for m in ms if m.get("regime") == "压缩") >= half:
        return "🔸蓄势"
    if sum(1 for m in ms if m.get("pd_pct", 0.5) <= 0.15) >= half:
        return "深折价"
    if sum(1 for m in ms if m.get("pd_pct", 0.5) >= 0.85) >= half:
        return "深溢价"
    return "常态"


# 期限结构阈值：短端/长端归一波动比 >此=倒挂(近端急)，<其倒数=顺挂(远端主导)
_TS_BACKWARD = 1.2
_TS_CONTANGO = 1 / _TS_BACKWARD   # 严格倒数(0.8333)，保期限结构上下对称(修 nit)


def vol_term_structure(by_tf: dict) -> dict:
    """波动率期限结构：各周期 rv 用 √t 归一到同一时间基准后，比短端 vs 长端（方差期限结构思路）。

    跨周期 rv 不可直接比(rv∝√t，见模块头)——先除 √(周期时长)归一为「单位时间波动强度」再比，
    这正是化解那条「不可比」警告的标准做法。返回 {shape, ratio, short, long, n}：
      shape∈{倒挂,平坦,顺挂,缺}；ratio=短端/长端归一波动。
      倒挂(ratio>1.2)=近端波动高于远端→急性应激/事件驱动；
      顺挂(ratio<0.83)=近端低于远端→风暴后趋缓/长周期主导；平坦=期限结构均衡。
    取首尾各 ~1/3 周期为短/长端(中段排除使对比更锐)。<2 个有效周期→缺(不冒充结构)。
    诚实：归一假设波动∝√t(GBM)真实有偏；rv 为回望量，描述当前非预测。
    """
    items = sorted(
        ((tf, m) for tf, m in by_tf.items()
         if isinstance(m.get("rv"), (int, float)) and math.isfinite(m.get("rv", float("nan")))
         and _TF_MS.get(tf)),
        key=lambda kv: _TF_MS[kv[0]],
    )
    n = len(items)
    if n < 2:
        return {"shape": "缺", "ratio": 0.0, "short": 0.0, "long": 0.0, "n": n}
    k = max(1, n // 3)
    short = [m["rv"] / math.sqrt(_TF_MS[tf]) for tf, m in items[:k]]
    long = [m["rv"] / math.sqrt(_TF_MS[tf]) for tf, m in items[-k:]]
    sv, lv = sum(short) / len(short), sum(long) / len(long)
    if lv <= 1e-12:
        return {"shape": "缺", "ratio": 0.0, "short": sv, "long": lv, "n": n}
    ratio = sv / lv
    shape = ("倒挂" if ratio > _TS_BACKWARD
             else ("顺挂" if ratio < _TS_CONTANGO else "平坦"))
    return {"shape": shape, "ratio": ratio, "short": sv, "long": lv, "n": n}


_FALLBACK_TOP = 50              # 清单空时展示币数上界(性能：rank 每币算 7 周期，不能全 665)
_FALLBACK_WIN_MS = 86_400_000  # 振幅预筛窗口(近 24h)：只在近端真实波动里选


def pick_coins(store: Any) -> dict[str, str]:
    """选波动板展示币集(dashboard+CLI 共用单一源，消除两前端选币分叉)：优先监控清单；
    清单空则回退「近 24h 振幅最大的 N 币」——波动板该突出**正在剧烈波动**的币，而非任意 50 个。

    振幅预筛是 SQL 廉价代理(~12ms：(MAX(h)-MIN(l))/MIN(l))，昂贵的全指标 rank 仅作用这最该看的
    N 币——性能上界与信息质量兼得。近端无 bar(合成/采集停摆)→ 降级 DISTINCT，保证不空。
    """
    coins = store.get_monitored_coins()
    if coins:
        return coins
    from ..config import CANONICAL_TIMEFRAMES  # noqa: PLC0415 — 惰性导入避顶层环
    try:
        cutoff = int(time.time() * 1000) - _FALLBACK_WIN_MS
        rows = store.conn.execute(
            "SELECT coin FROM bitget_candles WHERE tf=? AND open_ms>=? "
            "GROUP BY coin ORDER BY (MAX(h)-MIN(l))/NULLIF(MIN(l),0) DESC LIMIT ?",
            (CANONICAL_TIMEFRAMES[0], cutoff, _FALLBACK_TOP),
        ).fetchall()
        if not rows:   # 无近端数据(合成测试/采集停摆)→ 降级任意已采币，保证不空
            rows = store.conn.execute(
                "SELECT DISTINCT coin FROM bitget_candles LIMIT ?", (_FALLBACK_TOP,)
            ).fetchall()
        return {r[0]: f"{r[0]}USDT" for r in rows}
    except Exception:  # noqa: BLE001
        return {}


class VolatilityMonitor:
    """逐周期读已采 K 线算波动+PD 指标，按运动分排序出当前在动的监控清单币。"""

    __slots__ = ("coin_to_symbol", "timeframes", "store", "bars")

    def __init__(self, coin_to_symbol: dict[str, str], timeframes: list[str],
                 store: Any, bars: int = 120) -> None:
        self.coin_to_symbol = coin_to_symbol
        self.timeframes = list(timeframes) or ["15m"]
        self.store = store
        self.bars = bars

    def _tf_metrics(self, coin: str, tf: str) -> dict | None:
        """单 coin/tf：vol_metrics + pdarray 合并；不足或异常返回 None。"""
        try:
            cs = self.store.get_candles(coin, tf, self.bars) if self.store else []
        except Exception:  # noqa: BLE001 — 单组合失败不影响整体
            return None
        if len(cs) < 3:
            return None
        h = [x.h for x in cs]; l = [x.l for x in cs]; c = [x.c for x in cs]
        m = vol_metrics(h, l, c)
        if not m:
            return None
        m.update(pdarray(h, l, c))
        return m

    def _fastest_tf(self) -> str:
        """监控周期里时长最短的（用于新鲜度：最短周期 bar 最频繁，最能反映数据延迟）。修 P1-2。"""
        if not self.timeframes:
            return "15m"
        return min(self.timeframes, key=lambda t: _TF_MS.get(t, 900_000))

    def _latest_bar_ms(self, coin: str) -> int:
        """该币**最短周期**最新 bar 的 open_ms；store 无此能力或异常→0（不误判新鲜度）。"""
        fn = getattr(self.store, "latest_candle_ms", None)
        if fn is None or not self.timeframes:
            return 0
        try:
            return int(fn(coin, self._fastest_tf()) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def rank(self, now_ms: int = 0) -> list[dict]:
        """每币逐周期算指标 → {coin, score(各周期运动分取最大), by_tf}，按 score 降序。

        now_ms 预留（与兄弟监控板 bb/harmonic 统一签名；当前排序不依赖时间）。
        score 非有限(NaN/inf，理论上 vol_metrics 已守卫)时置 0，防 sort 非确定排序（修 P2-3）。
        """
        rows: list[dict] = []
        for coin in self.coin_to_symbol:
            by_tf = {tf: m for tf in self.timeframes
                     if (m := self._tf_metrics(coin, tf)) is not None}
            if not by_tf:
                continue
            sc = max(move_score(m) for m in by_tf.values())
            if not math.isfinite(sc):
                sc = 0.0
            rows.append({"coin": coin,
                         "score": sc,
                         "align": mtf_alignment(by_tf),
                         "state": coin_vol_state(by_tf),   # 多周期合成状态(决策级)
                         "term": vol_term_structure(by_tf),  # 波动率期限结构(√t 归一)
                         "last_ms": self._latest_bar_ms(coin),
                         "by_tf": by_tf})
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    def render(self, rows: list[dict], now_ms: int = 0, top: int = 8) -> str:
        """逐周期渲染波动追踪板（每周期一行：速度/加速度/σ/ATR/区间/PD 溢价折价）。空返回 ""。"""
        if not rows:
            return ""
        from ..util import fmt_ts  # noqa: PLC0415
        ts = fmt_ts(now_ms) if now_ms else ""
        lines = [f"🌀 实时波动追踪板 [{ts}] · 每周期指标(速度+加速度+区间+PD溢价折价) Top {top}"]
        # 数据新鲜度（诚实标注：实时板不静默展示陈旧数据）：最短周期最新 bar 时间 + 陈旧告警。
        # 阈值按最短周期时长动态(2×bar 时长)，避免 1H+ 周期被 30min 固定阈值误报陈旧（修 P1-2）。
        fresh = max((r.get("last_ms", 0) for r in rows), default=0)
        if fresh > 0:
            stale_ms = 2 * _TF_MS.get(self._fastest_tf(), 900_000)
            stale = now_ms > 0 and (now_ms - fresh) > stale_ms
            note = "  ⚠️数据陈旧(采集器可能停摆)" if stale else ""
            lines.append(f"🕒 数据更新至 {fmt_ts(fresh)}{note}")
        # 市场级态势：把矩阵聚合成全市场波动广度
        mr = market_regime(rows)
        if mr["label"]:
            lines.append(f"📊 监控集态势: {mr['label']}")  # 聚合限于展示币集，非全市场样本(诚实)
        # 动向摘要：把矩阵综合成可操作情报
        hl = volatility_highlights(rows)
        if hl["squeeze"]:
            lines.append("🔸蓄势(压缩): " + " ".join(
                f"{x['coin']}/{x['tf']}" for x in hl["squeeze"]))
        if hl["expansion"]:
            # 扩张=波动已放大的**当前态确认**;#177 null 对照纠 #153:原"90%续/0.73"是窗口重叠伪影(真实增益≈0),
            # 真实只有近端**弱短记忆**(|logret|自相关 lag-1≈0.28→lag-10≈0.05);且**方向不定**(方向类皆~0 #150-158)。
            lines.append("🔶放量(扩张·已放大;波动水平可前瞻corr~0.4、非90%续、方向不定): " + " ".join(
                f"{x['coin']}/{x['tf']}({x['velocity']:+.1f}%)" for x in hl["expansion"]))
        if hl["extreme_pd"]:
            lines.append("⚡极端PD: " + " ".join(
                f"{x['coin']}/{x['tf']}({x['pd_zone']}{x['pd_pct'] * 100:.0f}%)"
                for x in hl["extreme_pd"]))
        for r in rows[:top]:
            al = r.get("align") or {"bias": "分歧", "aligned": 0, "total": 0}
            bias_mark = {"多": "🟢多", "空": "🔴空", "分歧": "⚪分歧"}[al["bias"]]
            # 期限结构：仅显示可操作的倒挂(近端急)/顺挂(远端主导)，平坦/缺略去不扰
            ts_mark = {"倒挂": " ⏫期限倒挂(近端急)", "顺挂": " ⏬期限顺挂(远端主导)"}.get(
                (r.get("term") or {}).get("shape"), "")
            lines.append(
                f"━ {r['coin']:<8} [{r.get('state', '常态')}] 运动分 {r['score']:.1f}"
                f" · {bias_mark}({al['aligned']}/{al['total']}周期一致){ts_mark}")
            for tf in self.timeframes:
                m = r["by_tf"].get(tf)
                if not m:
                    continue
                v, a = m["velocity"], m["accel"]
                vdir = "🟢↑" if v >= 0 else "🔴↓"
                adir = "加速" if a * v > 0 else ("减速" if a * v < 0 else "—")
                vp = m.get("vol_pct", -1.0)
                # HVP：当前波动 vs 自身历史分位(🔥≥90% 异常剧烈 / ❄️≤10% 极静蓄势 / 无标记常态)；-1=数据不足略
                vp_str = ""
                if vp >= 0:
                    vp_mark = "🔥" if vp >= 0.9 else ("❄️" if vp <= 0.1 else "")
                    vp_str = f" HVP{vp * 100:.0f}%{vp_mark}"
                # EWMA 预期波动水平(唯一前瞻量,#154;比σ更准预测未来波动#155)。EW>σ仅描述近端波动高于均值,
                # **非预示续升**——#157 实测"EW vs σ 升/降"信号对未来波动无净预测力(混淆于水平),勿读作趋势
                ew = m.get("ewma_vol", -1.0)
                ew_str = f" EW{ew:.2f}%" if ew >= 0 else ""
                lines.append(
                    f"  {tf:<4} {vdir}{abs(v):.2f}% a{a:+.2f}{adir}"
                    f" σ{m['rv']:.2f}%[{m['regime']}]{ew_str} ATR{m['atr_pct']:.2f}% 幅{m['range_pct']:.2f}%"
                    f" PD{m['pd_pct'] * 100:.0f}%{m['pd_zone']}{vp_str}"
                )
        return "\n".join(lines)
