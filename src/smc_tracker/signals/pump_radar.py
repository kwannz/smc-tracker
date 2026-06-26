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

# 规则与 hr/lift 标定于 1h K 线(data/history/analysis/SUMMARY.md 回测)。喂入非 1h K 线时
# 窗口实际跨度按周期换算(24 根≠24h)，且 hr/lift 未在该 TF 验证——fmt 据此诚实标注(修审计 P1)。
_CALIB_TF_MS = 3_600_000


def _bar_ms(candles: list[Any]) -> int:
    """从最后两根 open_time_ms 推实际 K 线周期(ms);不足/异常→0。"""
    if len(candles) < 2:
        return 0
    try:
        d = int(candles[-1].open_time_ms) - int(candles[-2].open_time_ms)
    except Exception:  # noqa: BLE001
        return 0
    return d if d > 0 else 0


@dataclass(slots=True)
class PumpAlert:
    coin: str
    kind: str            # 'pump' / 'dump'
    rule: str
    hit_rate: float
    lift: float          # 实测回测 lift(不含妖币加权)
    rsi: float
    atr_pct: float
    ret24: float
    ts: int
    bar_ms: int = 0      # 实际 K 线周期：诚实推真实窗口跨度 + 标定 TF 校验(修审计 P1)
    boost: float = 1.0   # 妖币主观加权(先验,非实测 lift；修审计 P2 虚高)

    def fmt(self) -> str:
        tag = "🚀暴涨预警" if self.kind == "pump" else "💥暴跌预警"
        # ret24 实为「近 24 根」位移；真实跨度=24 根×周期(5m 下=2h，非硬编码 24h)
        span_h = 24 * self.bar_ms / 3_600_000 if self.bar_ms > 0 else 0.0
        span = f"{span_h:.0f}h" if span_h >= 1 else (f"{span_h*60:.0f}m" if span_h > 0 else "?")
        # hr/lift 仅 1h 标定；非标定 TF 诚实标注「本 TF 未验证」，不冒充本 TF 胜率
        calib = self.bar_ms > 0 and abs(self.bar_ms - _CALIB_TF_MS) <= _CALIB_TF_MS * 0.1
        if calib:
            stat = f"历史命中{self.hit_rate*100:.1f}%/lift{self.lift:g}x"
        else:
            stat = f"[1h标定命中{self.hit_rate*100:.1f}%/lift{self.lift:g}x·本{span}级TF未验证]"
        boost = f" 妖币×{self.boost:g}(先验非实测)" if self.boost != 1.0 else ""
        return (f"{tag} {self.coin} [{self.rule}]{boost} {stat} "
                f"| RSI={self.rsi:.0f} ATR%={self.atr_pct:.1f} 近24根({span})={self.ret24*100:+.0f}%")


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
        bar = _bar_ms(candles)
        # 暴涨：黑名单(只跌型)直接跳过
        if canon not in BLACKLIST:
            for name, cond, hr, lift in PUMP_RULES:
                if cond(f):
                    boost = 2.0 if canon in WHITELIST else 1.0
                    # lift 存实测基值,boost 单独传(不再把×2折进 lift冒充历史lift)
                    return PumpAlert(coin, "pump", name, hr, lift,
                                     f["rsi"], f["atr_pct"], f["ret24"], now_ms, bar, boost)
        for name, cond, hr, lift in DUMP_RULES:
            if cond(f):
                return PumpAlert(coin, "dump", name, hr, lift,
                                 f["rsi"], f["atr_pct"], f["ret24"], now_ms, bar, 1.0)
        return None
