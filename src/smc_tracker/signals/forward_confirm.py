"""前瞻置信合成器 —— 把领先信号合成一个对谐波 setup 置信的乘子。

设计依据（CLAUDE.md §二 前瞻 positioning + 诚实铁律；QA 审计修复）：
- **三个互不重叠分量**（防双计：各自独立来源，无重复信号）：
  · `flow_score`：资金流加速度（BitgetTradeMonitor，**仅加速度一项**，不含盘口/OI）。
  · `oi_signal`：方向化 OI 速度（OI↑+价↑=新多；按 has_oi 门控）。
  · `funding_extreme`：funding 极值反转（按 has_funding 门控——纯股票 funding=0 跳过，QA 实证）。
- **缺数据 = 中性**（乘子 1.0），绝不对无数据币佯装确认。
- **有界**：乘子 ∈ [0.80, 1.30]，保守区间，上线后用 review 闭环回校权重。
- **方向化对齐**：信号值 ∈[-1,1]（正=看涨），与 setup 方向同号→加权为正（boost），异号→负（penalize）。

纯函数：接收**已计算**的信号标量 + profile，不触网、不读 DB，可确定性单测。
"""
from __future__ import annotations

from .coin_profile import CoinSignalProfile

# 前瞻分量权重（保守初值；max boost = 0.12+0.10+0.10 = 0.32 → 封顶 1.30）
_W_FLOW: float = 0.12      # 资金流加速度（BitgetTradeMonitor）
_W_OI: float = 0.10        # 方向化 OI 速度
_W_FUNDING: float = 0.10   # funding 极值（拥挤反转）
_MULT_LO: float = 0.80
_MULT_HI: float = 1.30


def _clip(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def forward_mult(
    direction: str,
    profile: CoinSignalProfile,
    *,
    flow_score: float | None = None,
    oi_signal: float | None = None,
    funding_extreme: float | None = None,
) -> tuple[float, str]:
    """合成前瞻置信乘子（三个互不重叠分量）。

    参数：
        direction        — setup 方向 "long"/"short"（其它→中性）。
        profile          — 该币信号画像（门控 oi/funding 分量）。
        flow_score       — 资金流加速度 ∈[-1,1]（正=加速流入）；None=无数据。
        oi_signal        — 方向化 OI 速度 ∈[-1,1]（正=新多 positioning）；None=无数据。
        funding_extreme  — funding 极值反转 ∈[-1,1]（正=看涨反转）；None=无数据。

    返回：
        (mult, note) —— 乘子 ∈[0.80,1.30] 与诚实标注（用了/跳过了哪些分量）。
    """
    dir_sign = 1.0 if direction == "long" else (-1.0 if direction == "short" else 0.0)
    if dir_sign == 0.0:
        return 1.0, "方向未知，前瞻确认中性"

    delta = 0.0
    used: list[str] = []
    skipped: list[str] = []

    # 资金流加速度（仅此一项）
    if flow_score is not None:
        align = _clip(flow_score, -1.0, 1.0) * dir_sign
        delta += _W_FLOW * align
        used.append(f"资金流{'同向' if align >= 0 else '反向'}({flow_score:+.2f})")
    else:
        skipped.append("资金流(无数据)")

    # 方向化 OI（按 profile.has_oi 门控）
    if oi_signal is None:
        skipped.append("OI(无数据)")
    elif not profile.has_oi:
        skipped.append("OI(该币无OI跳过)")
    else:
        align = _clip(oi_signal, -1.0, 1.0) * dir_sign
        delta += _W_OI * align
        used.append(f"OI{'同向' if align >= 0 else '反向'}({oi_signal:+.2f})")

    # funding 极值（按 profile.has_funding 门控）
    if funding_extreme is None:
        skipped.append("funding(无数据)")
    elif not profile.has_funding:
        skipped.append("funding(该币funding=0跳过)")
    else:
        align = _clip(funding_extreme, -1.0, 1.0) * dir_sign
        delta += _W_FUNDING * align
        used.append(f"funding极值{'同向' if align >= 0 else '反向'}({funding_extreme:+.2f})")

    mult = _clip(1.0 + delta, _MULT_LO, _MULT_HI)

    note_parts: list[str] = []
    if used:
        note_parts.append("前瞻确认: " + " + ".join(used))
    if skipped:
        note_parts.append("跳过: " + ", ".join(skipped))
    note = "；".join(note_parts) if note_parts else "前瞻确认中性"
    return mult, note


def apply_forward(setups, get_signals, *, max_conf: float = 0.90) -> None:
    """对一批 setup **就地**施加前瞻置信乘子（completed + forming 都生效）。

    QA 修复（解除 completed 门控）：旧版 orderflow boost 仅对 completed 生效，导致最该用
    领先信号的 forming（前瞻预警）反而拿不到任何前瞻确认。本函数对**全部** setup 一视同仁。

    参数：
        setups       — 含 .coin/.direction/.confidence（读写）/.forward（写）属性的对象列表（鸭子类型）。
        get_signals  — 回调 (coin, direction) -> (profile, flow_score, oi_signal, funding_extreme) | None。
                       返回 None 或 profile 为 None → 该 setup 不调整（诚实，缺数据=中性）。
        max_conf     — 置信封顶。
    """
    for s in setups:
        sig = get_signals(s.coin, s.direction)
        if not sig:
            continue
        profile, flow_score, oi_signal, funding_extreme = sig
        if profile is None:
            continue
        mult, note = forward_mult(
            s.direction, profile,
            flow_score=flow_score, oi_signal=oi_signal, funding_extreme=funding_extreme,
        )
        s.confidence = min(max_conf, s.confidence * mult)
        s.forward = note
