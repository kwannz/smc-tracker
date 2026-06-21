"""庄家 vs 游资分类器 —— 基于行为画像的可解释启发式分类（纯函数，不联网）。

区分两种资金性质：
  - 庄家 (whale)：大资金持仓型，账户净值高 + 持仓时间长 + 换手率低。
  - 游资 (hot_money)：快进快出追涨，持仓极短 + 交易频繁 + 中等资金量。
  - 混合 (mixed)：行为介于庄家与游资之间，或数据不足以明确区分。

注意：本分类基于行为画像的启发式规则，非绝对判断。
同一地址在不同阶段可能呈现不同风格；建议结合实时持仓/协同数据综合研判。
"""
from __future__ import annotations

from ..util import to_float as _f


# ───────────────────────── 阈值常量（集中管理，便于调参） ─────────────────────────

# 庄家判定阈值
_WHALE_ACCOUNT_VALUE_MIN: float = 5_000_000.0   # 账户净值 ≥ $5M
_WHALE_HOLD_SEC_MIN: float = 14_400.0            # 均持仓 ≥ 4h (14400s)

# 游资判定阈值
_HOT_HOLD_SEC_MAX: float = 3_600.0              # 均持仓 < 1h (3600s)
_HOT_TRADES_MIN: int = 20                        # 近期成交 ≥ 20 笔

# 混合边界（介于庄家与游资之间的宽松区间）
_MID_ACCOUNT_VALUE_MIN: float = 500_000.0        # 账户净值 ≥ $500K 才有分类意义
_MID_HOLD_SEC_MAX: float = 21_600.0             # 均持仓 < 6h 偏游资
_MID_HOLD_SEC_MIN: float = 1_800.0              # 均持仓 ≥ 30m 偏庄家


def classify_trader(
    *,
    account_value: float,
    avg_hold_sec: float,
    n_trades: int,
    turnover: float = 0.0,
    win_rate: float = 0.0,
) -> dict:
    """基于行为指标对交易者进行庄家/游资分类（纯函数，可测）。

    参数均通过 to_float 守卫，防止上游 NaN/None/inf 污染。

    :param account_value:  账户净值（USD），来自 profile["account_value"]。
    :param avg_hold_sec:   平均持仓时长（秒），由持仓生命周期估算。
    :param n_trades:       近期成交笔数，来自 profile["n_trades"]。
    :param turnover:       换手率（可选，0 表示未提供）。
    :param win_rate:       胜率（0-1，可选）。
    :return:               分类结果 dict，含 type/score_whale/score_hot/reason。
    """
    # 数据质量守卫：统一用 to_float，防止 NaN/inf/None
    av = _f(account_value)
    hold = _f(avg_hold_sec)
    n = int(max(_f(n_trades), 0))
    to = _f(turnover)
    wr = _f(win_rate)

    # ─── 庄家分 (0-100)：净值大 + 持仓长 + 低频 ───
    # 净值贡献：$5M 起线性增长，$50M 封顶，权重 40
    score_whale = 0.0
    if av >= _WHALE_ACCOUNT_VALUE_MIN:
        score_whale += min((av - _WHALE_ACCOUNT_VALUE_MIN) / 45_000_000.0, 1.0) * 40 + 10  # 达标基础分10
    # 持仓时长贡献：≥4h 起线性增长，≥24h 封顶，权重 40
    if hold >= _WHALE_HOLD_SEC_MIN:
        score_whale += min((hold - _WHALE_HOLD_SEC_MIN) / (86_400.0 - _WHALE_HOLD_SEC_MIN), 1.0) * 40 + 10
    # 低频贡献：n_trades 越少越好，≤5笔满分，权重 20
    freq_factor = max(1.0 - n / 100.0, 0.0)  # 100笔降至0
    score_whale += freq_factor * 20

    # ─── 游资分 (0-100)：持仓短 + 高频 + 中等资金 ───
    score_hot = 0.0
    # 持仓短贡献：< 1h 基础分，越短越高，权重 50
    if hold < _HOT_HOLD_SEC_MAX:
        score_hot += (1.0 - hold / _HOT_HOLD_SEC_MAX) * 50
    # 高频贡献：交易笔数越多越高，≥100 笔封顶，权重 30
    score_hot += min(n / 100.0, 1.0) * 30
    # 资金量适中贡献（游资通常中等规模，>$50M 反而不像游资），权重 20
    if av > 0:
        av_factor = min(av / 5_000_000.0, 1.0) * (1.0 - min(av / 50_000_000.0, 1.0))
        score_hot += av_factor * 20

    score_whale = round(min(max(score_whale, 0.0), 100.0), 1)
    score_hot = round(min(max(score_hot, 0.0), 100.0), 1)

    # ─── 确定分类（庄家/游资/混合）───
    diff = score_whale - score_hot

    # 硬规则优先（可解释、边界明确）
    is_whale_hard = (
        av >= _WHALE_ACCOUNT_VALUE_MIN
        and hold >= _WHALE_HOLD_SEC_MIN
    )
    is_hot_hard = (
        hold < _HOT_HOLD_SEC_MAX
        and n >= _HOT_TRADES_MIN
    )

    if is_whale_hard and not is_hot_hard:
        trader_type = "whale"
        reason = (
            f"大资金庄家：账户${av/1e6:.1f}M≥${_WHALE_ACCOUNT_VALUE_MIN/1e6:.0f}M门槛，"
            f"均持仓{hold/3600:.1f}h≥{_WHALE_HOLD_SEC_MIN/3600:.0f}h，"
            f"成交{n}笔(低频)，持仓型资金"
        )
    elif is_hot_hard and not is_whale_hard:
        trader_type = "hot_money"
        reason = (
            f"游资：均持仓{hold/60:.0f}m<{_HOT_HOLD_SEC_MAX/3600:.0f}h，"
            f"成交{n}笔≥{_HOT_TRADES_MIN}笔(高频)，快进快出追涨风格"
        )
    elif is_whale_hard and is_hot_hard:
        # 极少见：大资金但也高频 → 用分数差决定，偏庄家因为资金规模更关键
        if diff >= 0:
            trader_type = "whale"
            reason = f"大资金主导(净值${av/1e6:.1f}M)兼有高频操作，行为偏庄家(庄分{score_whale}/热钱分{score_hot})"
        else:
            trader_type = "hot_money"
            reason = f"高频主导(成交{n}笔)即使资金量大，行为偏游资(热钱分{score_hot}/庄分{score_whale})"
    else:
        # 混合：根据分数差进一步区分偏向
        if diff >= 15:
            trader_type = "mixed"
            reason = f"混合偏庄(均持{hold/3600:.1f}h，净值${av/1e6:.1f}M，庄分{score_whale}>热钱分{score_hot}，但未达硬阈值)"
        elif diff <= -15:
            trader_type = "mixed"
            reason = f"混合偏游资(均持{hold/60:.0f}m，成交{n}笔，热钱分{score_hot}>庄分{score_whale}，但未达硬阈值)"
        else:
            trader_type = "mixed"
            if av == 0 and hold == 0 and n == 0:
                reason = "数据不足，无法判断类型（账户净值/持仓时长/交易笔数均为0）"
            else:
                reason = (
                    f"混合型：均持{hold/3600:.1f}h，账户${av/1e6:.2f}M，成交{n}笔，"
                    f"庄分{score_whale}/热钱分{score_hot}，行为特征无明显偏向"
                )

    return {
        "type": trader_type,           # 'whale' / 'hot_money' / 'mixed'
        "score_whale": score_whale,
        "score_hot": score_hot,
        "reason": reason,
        # 附带输入摘要，供上层格式化使用
        "_account_value": av,
        "_avg_hold_sec": hold,
        "_n_trades": n,
    }


def fmt_classify(result: dict) -> str:
    """把 classify_trader 结果渲染为中文标签字符串（含 emoji）。

    :param result:  classify_trader 返回的 dict。
    :return:        可读中文标签，例 "🐋庄家(大资金$10.0M持仓型,均持4.2h)"。
    """
    t = result.get("type", "mixed")
    av = _f(result.get("_account_value", 0.0))
    hold = _f(result.get("_avg_hold_sec", 0.0))
    n = int(_f(result.get("_n_trades", 0.0)))

    av_str = f"${av/1e6:.1f}M" if av >= 1e6 else f"${av/1e3:.0f}K" if av >= 1e3 else f"${av:.0f}"

    if hold < 60:
        hold_str = f"{hold:.0f}s"
    elif hold < 3600:
        hold_str = f"{hold/60:.0f}m"
    else:
        hold_str = f"{hold/3600:.1f}h"

    if t == "whale":
        return f"🐋庄家(大资金{av_str}持仓型,均持{hold_str})"
    elif t == "hot_money":
        return f"🔥游资(快进快出,均持{hold_str},频繁{n}笔)"
    else:
        sw = result.get("score_whale", 0.0)
        sh = result.get("score_hot", 0.0)
        if sw > sh:
            return f"🔀混合偏庄(均持{hold_str},{av_str})"
        elif sh > sw:
            return f"🔀混合偏游资(均持{hold_str},{n}笔)"
        else:
            return f"🔀混合型(均持{hold_str},{av_str},{n}笔)"
