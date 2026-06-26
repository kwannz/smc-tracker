"""SMC 结构信号回测引擎（事件驱动、逐根重放）。

对一段历史 K 线：
  1. 逐根喂 MarketStructure / ZoneEngine / LiquidityEngine（与实盘同一套）。
  2. 每出现结构突破(BOS/CHoCH)，按 compute_risk 生成交易计划（入场=突破根收盘，
     止损=结构位，目标=2R）；可要求 OB/FVG 或 流动性扫荡 共振过滤。
  3. 从突破下一根起逐根模拟：先触止损记 -1R，先触目标记 +target_rr R（保守：同根止损优先）。
  4. 汇总胜率 / 平均 R / 盈亏比 / 期望。

诚实边界：聪明钱流向/OI/链上为实时数据，无历史回填，故不参与回测；本回测校验的是
信号的 SMC 骨架（结构+区域+扫荡+风险），共振过滤可用于检验「确认是否提升胜率」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Candle
from ..signals.risk import compute_risk
from ..smc.liquidity import LiquidityEngine
from ..smc.structure import MarketStructure
from ..smc.zones import ZoneEngine


@dataclass(slots=True)
class Trade:
    coin: str
    direction: str          # 'long' / 'short'
    entry: float
    stop: float
    target: float
    entry_idx: int          # 信号(结构突破)所在 K 线
    entry_mode: str = "break"   # 'break'(突破即入) / 'retrace'(回撤到 OB 限价入)
    triggered_idx: int = -1     # retrace 模式实际成交的 K 线
    exit_idx: int = -1
    outcome: str = "open"   # 'win' / 'loss' / 'open' / 'expired'(回撤未触发)
    r: float = 0.0          # 实现盈亏（以 R 计）
    rr: float = 0.0         # 该笔目标盈亏比（win 记 +rr R；0=回退 _simulate 的 target_rr，#201 支持谐波各自 rr）


@dataclass(slots=True)
class BacktestResult:
    coin: str
    trades: list[Trade] = field(default_factory=list)

    @property
    def resolved(self) -> list[Trade]:
        return [t for t in self.trades if t.outcome in ("win", "loss")]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.outcome == "win")

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.outcome == "loss")

    @property
    def win_rate(self) -> float:
        n = self.wins + self.losses
        return self.wins / n if n else 0.0

    @property
    def avg_r(self) -> float:
        r = self.resolved
        return sum(t.r for t in r) / len(r) if r else 0.0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.r for t in self.resolved if t.r > 0)
        gross_loss = -sum(t.r for t in self.resolved if t.r < 0)
        return gross_win / gross_loss if gross_loss else float("inf")

    # ---- freqtrade 式绩效(#201 借鉴 backtesting 报告) ----
    @property
    def total_r(self) -> float:
        """总盈亏(以 R 计)= 净期望×笔数,反映策略整体盈利能力。"""
        return sum(t.r for t in self.resolved)

    @property
    def expectancy(self) -> float:
        """每笔期望 R(=avg_r,freqtrade 命名);>0 为正期望策略。"""
        return self.avg_r

    @property
    def max_drawdown(self) -> float:
        """最大回撤(R):按平仓时间序累计 R 的峰-谷最大跌幅(freqtrade 风险核心指标)。"""
        seq = sorted((t for t in self.resolved), key=lambda t: t.exit_idx)
        peak = equity = 0.0
        mdd = 0.0
        for t in seq:
            equity += t.r
            peak = max(peak, equity)
            mdd = max(mdd, peak - equity)
        return mdd

    def summary(self) -> str:
        n = self.wins + self.losses
        pf = self.profit_factor
        return (f"{self.coin:>8}: 交易{n:>3} 胜率{self.win_rate*100:5.1f}% "
                f"期望{self.expectancy:+.2f}R 盈亏比{pf if pf != float('inf') else 99:5.2f} "
                f"总{self.total_r:+.1f}R 回撤{self.max_drawdown:.1f}R "
                f"(胜{self.wins}/负{self.losses}/未平{len(self.trades)-n})")


class Backtester:
    def __init__(self, coin: str = "X") -> None:
        self.coin = coin

    def run(
        self,
        candles: list[Candle],
        *,
        lookback: int = 2,
        target_rr: float = 2.0,
        max_stop_pct: float = 0.08,
        require_zone: bool = False,
        require_sweep: bool = False,
        sweep_window_bars: int = 6,
        entry_mode: str = "break",     # 'break' 突破即入 / 'retrace' 回撤到 OB 限价入
        max_wait_bars: int = 12,       # retrace：等待回撤触发的最大 K 线数
    ) -> BacktestResult:
        ms = MarketStructure(lookback=lookback)
        ze = ZoneEngine()
        le = LiquidityEngine(lookback=lookback)
        result = BacktestResult(self.coin)
        last_sweep: tuple[str, int] | None = None

        for i, c in enumerate(candles):
            ze.update(c)
            for sw in le.update(c):
                last_sweep = (sw.direction, i)
            for e in ms.update(c):
                direction = "long" if e.direction == "bull" else "short"
                if require_zone:
                    zones = [z for z in ze.active_zones(e.direction)
                             if z.kind in ("OB", "FVG")]
                    if not zones:
                        continue
                if require_sweep:
                    want = "bullish" if e.direction == "bull" else "bearish"
                    if not (last_sweep and last_sweep[0] == want
                            and i - last_sweep[1] <= sweep_window_bars):
                        continue
                obs = [z for z in ze.active_zones(e.direction) if z.kind == "OB"]
                ob = max(obs, key=lambda z: z.created_at) if obs else None
                if entry_mode == "retrace":
                    if ob is None:
                        continue                     # 回撤入场需 OB 作限价位
                    if direction == "long":
                        entry, stop = ob.top, ob.bottom * (1 - 0.001)
                        risk = entry - stop
                    else:
                        entry, stop = ob.bottom, ob.top * (1 + 0.001)
                        risk = stop - entry
                    if risk <= 0 or abs(entry - stop) / entry > max_stop_pct:
                        continue
                    target = (entry + target_rr * risk if direction == "long"
                              else entry - target_rr * risk)
                    result.trades.append(Trade(
                        coin=self.coin, direction=direction, entry=entry, stop=stop,
                        target=target, entry_idx=i, entry_mode="retrace"))
                else:
                    swing_low = ms.ref_low.price if ms.ref_low else 0.0
                    swing_high = ms.ref_high.price if ms.ref_high else 0.0
                    rp = compute_risk(direction, c.c, swing_low, swing_high,
                                      ob.bottom if ob else 0.0, ob.top if ob else 0.0,
                                      target_rr=target_rr, max_stop_pct=max_stop_pct)
                    if rp is None:
                        continue
                    result.trades.append(Trade(
                        coin=self.coin, direction=direction, entry=rp.entry,
                        stop=rp.stop, target=rp.target, entry_idx=i, entry_mode="break"))

        for t in result.trades:
            t.rr = target_rr                  # 结构信号统一 target_rr;run_setups 可传各自 rr
        self._simulate(candles, result.trades, target_rr, max_wait_bars)
        return result

    def run_setups(
        self,
        candles: list[Candle],
        signals: list[dict],
        *,
        target_rr: float = 2.0,
        max_wait_bars: int = 12,
    ) -> BacktestResult:
        """回测**外部信号**(谐波 TradeSetup / 任意来源),复用 _simulate fill 模拟器(去重)。

        signals=[{entry_idx,direction,entry,stop,target, rr?, entry_mode?}]——谐波各 setup rr 不同,
        win 记各自 rr(无 rr 回退 target_rr)。这是 freqtrade 式"策略产信号→引擎模拟成交"的解耦(#201)。
        """
        result = BacktestResult(self.coin)
        for s in signals:
            result.trades.append(Trade(
                coin=self.coin, direction=s["direction"], entry=float(s["entry"]),
                stop=float(s["stop"]), target=float(s["target"]),
                entry_idx=int(s["entry_idx"]), entry_mode=s.get("entry_mode", "break"),
                rr=float(s.get("rr", target_rr))))
        self._simulate(candles, result.trades, target_rr, max_wait_bars)
        return result

    @staticmethod
    def _simulate(candles: list[Candle], trades: list[Trade], target_rr: float,
                  max_wait_bars: int = 12) -> None:
        for t in trades:
            win_r = t.rr if t.rr > 0 else target_rr   # #201 每笔自带 rr(谐波各异),回退 target_rr
            triggered = t.entry_mode == "break"   # break 模式立即成交
            if triggered:
                t.triggered_idx = t.entry_idx
            for j in range(t.entry_idx + 1, len(candles)):
                cj = candles[j]
                if not triggered:                 # retrace：等价格回撤触及限价
                    hit = (cj.l <= t.entry if t.direction == "long" else cj.h >= t.entry)
                    if hit:
                        triggered = True
                        t.triggered_idx = j        # 同根继续判止损/目标
                    elif j - t.entry_idx >= max_wait_bars:
                        t.outcome = "expired"
                        break
                    else:
                        continue
                if t.direction == "long":
                    if cj.l <= t.stop:             # 保守：同根止损优先
                        t.outcome, t.r, t.exit_idx = "loss", -1.0, j
                        break
                    if cj.h >= t.target:
                        t.outcome, t.r, t.exit_idx = "win", win_r, j
                        break
                else:
                    if cj.h >= t.stop:
                        t.outcome, t.r, t.exit_idx = "loss", -1.0, j
                        break
                    if cj.l <= t.target:
                        t.outcome, t.r, t.exit_idx = "win", win_r, j
                        break
            if not triggered and t.outcome == "open":
                t.outcome = "expired"
