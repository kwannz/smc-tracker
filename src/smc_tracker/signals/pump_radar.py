"""暴涨暴跌实时预警 —— 把 38 万根历史回测出的高 lift 规则操作化。

规则来自 data/history/analysis/SUMMARY.md（4 维交叉验证 + base-rate 校正）：
- 暴涨前兆是「动量延续」非「能量积蓄」：RSI 高位 + ATR% 扩张 + 放量阳线 / 已涨多。
- 暴跌风险集中两端：刚垂直暴涨(见顶) 与 已在跌中(中继)。
- 妖币白名单 lift 翻倍；只跌黑名单做多预警直接拉黑。
诚实：高 lift≠必涨(最严规则命中也仅~5%)，本预警是**尾部押注/仓位调节器**，非满仓开关。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..indicators.technical import atr, ohlcv_arrays, rsi
from ..memecoins import normalize

# 妖币(暴涨型)：同规则 lift 翻数倍，优先
WHITELIST = {"MOODENG", "PNUT", "TRUMP", "AIXBT", "TURBO", "GRIFFAIN"}
# 只跌型：做多/暴涨预警拉黑
BLACKLIST = {"PUMP", "BONK", "POPCAT", "SPX", "DOGE"}

# (规则名, 条件 lambda(f), 历史命中率, lift)
PUMP_RULES = [
    ("RSI>70&ATR%>3", lambda f: f["rsi"] > 70 and f["atr_pct"] > 3, 0.049, 18.0),
    ("ret24>20%&量>2x", lambda f: f["ret24"] > 0.20 and f["vol_x"] > 2.0, 0.041, 14.3),
    ("放量≥5×+阳线", lambda f: f["relvol"] >= 5 and f["bull"], 0.015, 5.3),
    ("深跌反抽(ret24<-20%)", lambda f: f["ret24"] < -0.20 and f["rsi"] > 45, 0.018, 6.2),
]
DUMP_RULES = [
    ("垂直见顶(ret24>50%)", lambda f: f["ret24"] > 0.50, 0.067, 15.8),
    ("下跌中继(RSI<35&ret24<-15%)", lambda f: f["rsi"] < 35 and f["ret24"] < -0.15, 0.034, 8.1),
    ("高位回落风险(ATR%>5&ret24>20%&阴线)",
     lambda f: f["atr_pct"] > 5 and f["ret24"] > 0.20 and not f["bull"], 0.039, 9.2),
]


@dataclass(slots=True)
class PumpAlert:
    coin: str
    kind: str            # 'pump' / 'dump'
    rule: str
    hit_rate: float
    lift: float
    rsi: float
    atr_pct: float
    ret24: float
    ts: int

    def fmt(self) -> str:
        tag = "🚀暴涨预警" if self.kind == "pump" else "💥暴跌预警"
        return (f"{tag} {self.coin} [{self.rule}] 历史命中{self.hit_rate*100:.1f}%/lift{self.lift:g}x "
                f"| RSI={self.rsi:.0f} ATR%={self.atr_pct:.1f} 24h={self.ret24*100:+.0f}%")


def features(candles: list[Any]) -> dict[str, float] | None:
    if len(candles) < 30:
        return None
    a = ohlcv_arrays(candles)
    h, l, c, v, o = a["h"], a["l"], a["c"], a["v"], a["o"]
    rsi_v = rsi(c, 14)[-1]
    atr_v = atr(h, l, c, 14)[-1]
    if not (np.isfinite(rsi_v) and np.isfinite(atr_v)) or c[-1] <= 0:
        return None
    ret24 = (c[-1] - c[-25]) / c[-25] if len(c) >= 25 and c[-25] > 0 else 0.0
    recent = v[-24:].mean()
    # prior 放量基准至少 24 根才稳健，否则 vol_x 置 1.0(中性)，不因小样本虚假放大
    PRIOR_MIN = 24
    prior_window = v[-168:-24] if len(v) >= 168 else v[:-24]
    prior = prior_window.mean() if len(prior_window) >= PRIOR_MIN and prior_window.mean() > 0 else 0.0
    vol_x = recent / prior if prior > 0 else 1.0
    relvol = v[-1] / v[-24:-1].mean() if len(v) >= 25 and v[-24:-1].mean() > 0 else 1.0
    return {"rsi": float(rsi_v), "atr_pct": float(atr_v / c[-1] * 100), "ret24": float(ret24),
            "vol_x": float(vol_x), "relvol": float(relvol), "bull": bool(c[-1] >= o[-1])}


class PumpRadar:
    def evaluate(self, coin: str, candles: list[Any], now_ms: int) -> PumpAlert | None:
        f = features(candles)
        if f is None:
            return None
        canon = normalize(coin)
        # 暴涨：黑名单(只跌型)直接跳过
        if canon not in BLACKLIST:
            for name, cond, hr, lift in PUMP_RULES:
                if cond(f):
                    boost = 2.0 if canon in WHITELIST else 1.0
                    return PumpAlert(coin, "pump", name, hr, lift * boost,
                                     f["rsi"], f["atr_pct"], f["ret24"], now_ms)
        for name, cond, hr, lift in DUMP_RULES:
            if cond(f):
                return PumpAlert(coin, "dump", name, hr, lift,
                                 f["rsi"], f["atr_pct"], f["ret24"], now_ms)
        return None
