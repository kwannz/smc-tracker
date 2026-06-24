"""sfg/pdbb.py — PDBB 因子（PD Array & Breaker Block 反转簇因子）Python 移植。

算法来源: SFG "PD Array & Breaker Block Signals [SFG]" Pine Script v5
Rust 锚定: sfg_indicators_rs/src/indicators/pd_array_breaker_block.rs

因子语义（反转簇，sign_convention 与趋势簇相反）：
  factor_pdbb = clamp((HH+LL-2*close)/(HH-LL), -1, 1)
  +1 = close 在折扣区极底（close≈LL）= 看涨反转信号
  -1 = close 在溢价区极顶（close≈HH）= 看跌反转信号
   0 = close 在 HH/LL 中点 = 中性
  NaN = fail-closed（无 active breaker block 或 band 退化）

核心算法步骤（strict no-lookahead）：
  1. WARMUP: n < length+2 则 emit NaN。
  2. BODY EXTREMES: mx=max(close,open), mn=min(close,open)。
  3. PIVOTS: ph/pl 用 left=length, right=1（right=1 确认滞后）。
  4. ZIGZAG: 环形缓冲 size=50（index0=newest），d=+1高/-1低/0空。
     ph 确认时 push(+1, center_bar_idx, high[center])；
     pl 同理。方向相同则 extend（更极端值替换），反向则 push。
  5. iH/iL slot: iH=(zz[2].d==1)?2:1; iL=(zz[2].d==-1)?2:1。
  6. MSS 多头 gate: close>zz[iH].y AND zz[iH].d==1 AND mss_dir<1 AND per。
     读 ABCDE 5摆动点；formation: Ey<Cy AND Cx!=Dx AND isOK(onlyWhenInPDarray)。
  7. BREAKER BLOCK 多头: 从 D bar 向 C bar 扫描第一根绿 bar（close>open）；
     green1_top/bot = high/low（或 body 若 breakerCandleOnlyBody）。
     bb_avg = (green1_bot + green1_top)/2 = D2（BB 中线）。
  8. PREMIUM/DISCOUNT D3: 扫描 ZigZag 最近 HH(d==1) 和 LL(d==-1)；
     pd_premium_top = last_hh, pd_discount_bottom = last_ll。
  9. CONTINUOUS FACTOR: level_factor(close, pd_discount_bottom, pd_premium_top)
     = clamp((mid_ref-close)/half_range, -1, 1)。

诚实标注：
  - 本因子高复杂度（ZigZag+MSS+breaker block）；短序列或无摆动结构→ 全 NaN，正常。
  - full_history=False（默认）: Pine `per = last_bar_index - bar_index <= 2000` 绘图预算门；
    短历史（<2001 bar）等价于 full_history=True（per 恒为 True）。
    对 KNN 历史特征计算建议 full_history=True 以保持稳定性。
  - lookahead_risk: LOW（pivot right=1 确认滞后，ZigZag 写 prior bar y2=high[c-1]，
    level 仅扫已确认 ZigZag 点）。1-bar 确认滞后已内建；KNN 对齐时用确认 bar 索引。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from smc_tracker.util import to_float
from ._common import (
    level_factor,
    ohlcv_arrays,
    pivot_high_series,
    pivot_low_series,
    forward_fill,
)


# ─────────────────────────────────────────────────────────────────────────────
# ZigZag 环形缓冲点
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _ZZPoint:
    """ZigZag 单点：方向 d（+1高/-1低/0空）+ 确认 bar 索引 x + 价格 y。"""
    d: int = 0    # +1=HH / -1=LL / 0=空
    x: int = 0    # 确认 bar 索引
    y: float = math.nan  # 价格


class _ZigZag:
    """ZigZag 环形缓冲（size=50, index0=newest）。

    Rust: pdbb.rs:139-165 zigzag_update。
    Pine: in_out 数组，最新在前。
    """

    SIZE = 50

    def __init__(self) -> None:
        # index 0 = newest；初始全空
        self._pts: list[_ZZPoint] = [_ZZPoint() for _ in range(self.SIZE)]

    def front(self) -> _ZZPoint:
        """最新点（index 0）。"""
        return self._pts[0]

    def get(self, i: int) -> _ZZPoint:
        """获取第 i 个点（0=newest）。"""
        return self._pts[i]

    def _push(self, d: int, x: int, y: float) -> None:
        """推入新点（shift right，discard oldest）。"""
        # 右移（从末尾向前），然后写入 index 0
        for i in range(self.SIZE - 1, 0, -1):
            self._pts[i] = self._pts[i - 1]
        self._pts[0] = _ZZPoint(d=d, x=x, y=y)

    def update_high(self, x: int, y: float) -> None:
        """处理 pivot high 确认事件（Rust: pdbb.rs:139-150）。

        若前端方向 < +1（空或最近是低点）: push 新高点。
        若前端方向 == +1（已有高点）: extend（更高价格替换，保持 index 位置）。
        """
        front = self._pts[0]
        if front.d < 1:
            self._push(d=1, x=x, y=y)
        else:
            # 同向 extend：更高价格替换
            if y > front.y:
                self._pts[0] = _ZZPoint(d=1, x=x, y=y)

    def update_low(self, x: int, y: float) -> None:
        """处理 pivot low 确认事件（Rust: pdbb.rs:151-165）。

        若前端方向 > -1（空或最近是高点）: push 新低点。
        若前端方向 == -1（已有低点）: extend（更低价格替换）。
        """
        front = self._pts[0]
        if front.d > -1:
            self._push(d=-1, x=x, y=y)
        else:
            # 同向 extend：更低价格替换
            if y < front.y:
                self._pts[0] = _ZZPoint(d=-1, x=x, y=y)

    def copy(self) -> "_ZigZag":
        """深拷贝（供逐 bar 状态机使用）。"""
        zz = _ZigZag()
        zz._pts = [_ZZPoint(p.d, p.x, p.y) for p in self._pts]
        return zz


# ─────────────────────────────────────────────────────────────────────────────
# 主状态机
# ─────────────────────────────────────────────────────────────────────────────

def pdbb_series(
    candles: list[Any],
    *,
    length: int = 5,
    r2a: float = 2.0,
    r2b: float = 3.0,
    r2c: float = 4.0,
    breakerCandleOnlyBody: bool = False,
    breakerCandle_2Last: bool = False,
    onlyWhenInPDarray: bool = False,
    tillFirstBreak: bool = True,
    full_history: bool = False,
) -> np.ndarray:
    """PDBB 连续因子序列。

    Args:
        candles: K 线列表，每项有 .o/.h/.l/.c 属性。
        length: pivot 左臂长度（right 硬编码 =1）。默认 5。
        r2a/r2b/r2c: TP 倍数，不影响 continuous factor（factor 只用 pd level）。
        breakerCandleOnlyBody: False=用 high/low；True=用 body（验证默认路径 False）。
        breakerCandle_2Last: 是否用最后 2 根同向蜡烛（验证默认 False）。
        onlyWhenInPDarray: 是否加 fib-mid isOK 子门（验证默认 False）。
        tillFirstBreak: 遇到中线突破停止 block 复用（不影响 factor）。
        full_history: True=禁用 Pine 2000-bar 绘图预算门；False=复现 Pine 行为
                      （短序列 per=True，等效 full_history=True）。

    Returns:
        np.ndarray shape=(n,) dtype=float64。
        warmup 段和无 active block 位置填 NaN（fail-closed sentinel）。
        有限值已 clamp 到 [-1, 1]。
    """
    n = len(candles)
    out = np.full(n, np.nan, dtype=float)

    if n == 0:
        return out

    # ── 提取 OHLCV ───────────────────────────────────────────────────────────
    arrs = ohlcv_arrays(candles)
    o_arr = arrs["o"]
    h_arr = arrs["h"]
    l_arr = arrs["l"]
    c_arr = arrs["c"]

    # ── WARMUP: n < length+2 → 全 nan（pdbb.rs:256-260）──────────────────────
    if n < length + 2:
        return out

    # ── BODY EXTREMES（pdbb.rs:262-268, pine:50-51）──────────────────────────
    # mx[i] = max(close[i], open[i]); mn[i] = min(close[i], open[i])
    mx_arr = np.maximum(c_arr, o_arr)
    mn_arr = np.minimum(c_arr, o_arr)

    # ── PIVOTS（right=1 确认滞后，pine:230-231, pdbb.rs:42-100）────────────
    # ph[i] = high[center] 当且仅当 i = center+1（right=1）
    ph_arr = pivot_high_series(h_arr, left=length, right=1)
    pl_arr = pivot_low_series(l_arr, left=length, right=1)

    # ── 逐 bar 状态机 ─────────────────────────────────────────────────────────
    # 状态变量（模拟 Rust/Pine 的 var 变量）
    zz = _ZigZag()          # ZigZag 环形缓冲
    mss_dir: int = 0        # MSS 方向（+1 多头 / -1 空头 / 0 无）
    bb_dir: int = 0         # breaker block 方向（+1多 / -1空 / 0 无）
    # Premium/Discount D3 columns（port invention，Rust: pdbb.rs:838-860）
    pd_premium_top: float = math.nan
    pd_discount_bottom: float = math.nan

    # 最近有效的 HH/LL 价格（来自 ZigZag）
    last_hh: float = math.nan
    last_ll: float = math.nan

    # Pine `per` 绘图预算门（pine:49）：per = last_bar_index - bar_index <= 2000
    last_bar_index = n - 1

    for i in range(n):
        close_i = c_arr[i]
        open_i = o_arr[i]
        high_i = h_arr[i]
        low_i = l_arr[i]

        # Pine `per` 门：full_history=False 时只有最后 2001 bar 形成 block
        # 对短序列（n<=2001），per 恒为 True
        if full_history:
            per = True
        else:
            per = (last_bar_index - i) <= 2000

        # ── ZigZag 更新（pdbb.rs:139-165）────────────────────────────────────
        # pivot high 确认：ph_arr[i] 非 nan → center bar = i-1（因 right=1，center=i-1）
        # ZigZag y2 = high[center] = high[i-1]（pine:236: y2=nz(hi[1])）
        if math.isfinite(ph_arr[i]) and i >= 1:
            y2_h = h_arr[i - 1]  # center bar 的 high
            if math.isfinite(y2_h):
                zz.update_high(x=i - 1, y=y2_h)
                # 更新 last_hh（最近确认的 HH）
                last_hh = y2_h

        if math.isfinite(pl_arr[i]) and i >= 1:
            y2_l = l_arr[i - 1]  # center bar 的 low
            if math.isfinite(y2_l):
                zz.update_low(x=i - 1, y=y2_l)
                # 更新 last_ll（最近确认的 LL）
                last_ll = y2_l

        # ── iH/iL slot 选择（pdbb.rs:365-366, pine:260-261）─────────────────
        # iH = (zz[2].d==1) ? 2 : 1
        # iL = (zz[2].d==-1) ? 2 : 1
        zz2 = zz.get(2)
        iH = 2 if zz2.d == 1 else 1
        iL = 2 if zz2.d == -1 else 1

        # ── MSS + BREAKER BLOCK（pdbb.rs:375-663）────────────────────────────
        zz_iH = zz.get(iH)
        zz_iL = zz.get(iL)

        # ── MSS 多头 gate（pdbb.rs:375-405, pine:265-280）───────────────────
        # 条件: close[i] > zz[iH].y AND zz[iH].d==1 AND mss_dir<1 AND per
        if (
            math.isfinite(close_i)
            and math.isfinite(zz_iH.y)
            and close_i > zz_iH.y
            and zz_iH.d == 1
            and mss_dir < 1
            and per
        ):
            # 读 ABCDE 5摆动引用（pine:265-268）
            # A=zz[4], B=zz[3], C=zz[2], D=zz[1], E=zz[0]
            # 注意: 若 iH≠1，需要调整偏移
            # 按 Rust pdbb.rs:382-395:
            #   bull arm: ay_idx=iH+3, by_idx=iH+2, cy_idx=iH+1, dy_idx=iH, ey_idx=iH-1
            # 这里我们直接按 pine 逻辑（iH通常为1或2）
            ay = zz.get(iH + 3)
            by = zz.get(iH + 2)
            cy = zz.get(iH + 1)
            dy = zz.get(iH)
            ey = zz.get(iH - 1) if iH >= 1 else _ZZPoint()

            # 所有引用需有效（d!=0 且 y 有限）
            abcde_valid = (
                ay.d != 0 and math.isfinite(ay.y)
                and by.d != 0 and math.isfinite(by.y)
                and cy.d != 0 and math.isfinite(cy.y)
                and dy.d != 0 and math.isfinite(dy.y)
                and ey.d != 0 and math.isfinite(ey.y)
            )

            if abcde_valid:
                # fib mid（pine:272-273, pdbb.rs:399）
                # ay_mn = min of ay's extreme（A 是低点所以用 ay.y）
                # 正确: bull arm A=LL,B=HH,C=LL,D=HH,E=LL
                # ay_mn = ay.y（A 是低点）
                # fib_mid = ay_mn + (max(by.y, dy.y) - ay_mn)/2
                # isOK gate（onlyWhenInPDarray）:
                #   isOK = (onlyWhenInPDarray==false) OR (close[i] <= fib_mid)
                if onlyWhenInPDarray:
                    ay_mn = ay.y  # A 是低点
                    fib_mid = ay_mn + (max(by.y, dy.y) - ay_mn) / 2.0
                    is_ok = close_i <= fib_mid
                else:
                    is_ok = True

                # formation sub-gate（pdbb.rs:404-405, pine:280）:
                # Ey < Cy AND Cx != Dx（防止同 bar 连续摆动）AND isOK
                cx_neq_dx = cy.x != dy.x
                ey_lt_cy = ey.y < cy.y
                formation_ok = ey_lt_cy and cx_neq_dx and is_ok

                if formation_ok:
                    # 扫描 D bar → C bar 找第一根绿 bar（pdbb.rs:406-518）
                    # 绿 bar: close > open（多头 breaker block）
                    # 扫描范围: bar idx from dy.x down to cy.x (inclusive)
                    # 注意 x 是确认 bar 索引，范围可能跨越多根
                    scan_start = cy.x
                    scan_end = dy.x
                    # 确保范围有效（D 在 C 之后）
                    if scan_end > scan_start:
                        green1_idx = -1
                        for scan_i in range(scan_start, scan_end + 1):
                            if scan_i < n:
                                sc = c_arr[scan_i]
                                so = o_arr[scan_i]
                                if math.isfinite(sc) and math.isfinite(so) and sc > so:
                                    green1_idx = scan_i
                                    break

                        if green1_idx >= 0:
                            # breaker block 极值
                            if breakerCandleOnlyBody:
                                g_top = mx_arr[green1_idx]
                                g_bot = mn_arr[green1_idx]
                            else:
                                g_top = h_arr[green1_idx]
                                g_bot = l_arr[green1_idx]

                            if math.isfinite(g_top) and math.isfinite(g_bot) and g_top > g_bot:
                                # bb_avg = (g_bot + g_top) / 2 (D2 = BB 中线)
                                # 设置 block active
                                mss_dir = 1
                                bb_dir = 1

        # ── MSS 空头 gate（pdbb.rs:520-663, pine:402-538 mirror）────────────
        if (
            math.isfinite(close_i)
            and math.isfinite(zz_iL.y)
            and close_i < zz_iL.y
            and zz_iL.d == -1
            and mss_dir > -1
            and per
        ):
            ay = zz.get(iL + 3)
            by = zz.get(iL + 2)
            cy = zz.get(iL + 1)
            dy = zz.get(iL)
            ey = zz.get(iL - 1) if iL >= 1 else _ZZPoint()

            abcde_valid = (
                ay.d != 0 and math.isfinite(ay.y)
                and by.d != 0 and math.isfinite(by.y)
                and cy.d != 0 and math.isfinite(cy.y)
                and dy.d != 0 and math.isfinite(dy.y)
                and ey.d != 0 and math.isfinite(ey.y)
            )

            if abcde_valid:
                if onlyWhenInPDarray:
                    ay_mx = ay.y  # A 是高点（bear arm）
                    fib_mid = ay_mx - (ay_mx - min(by.y, dy.y)) / 2.0
                    is_ok = close_i >= fib_mid
                else:
                    is_ok = True

                # bear formation: Ey > Cy AND Cx != Dx AND isOK
                cx_neq_dx = cy.x != dy.x
                ey_gt_cy = ey.y > cy.y
                formation_ok = ey_gt_cy and cx_neq_dx and is_ok

                if formation_ok:
                    scan_start = cy.x
                    scan_end = dy.x
                    if scan_end > scan_start:
                        red1_idx = -1
                        for scan_i in range(scan_start, scan_end + 1):
                            if scan_i < n:
                                sc = c_arr[scan_i]
                                so = o_arr[scan_i]
                                if math.isfinite(sc) and math.isfinite(so) and sc < so:
                                    red1_idx = scan_i
                                    break

                        if red1_idx >= 0:
                            if breakerCandleOnlyBody:
                                r_top = mx_arr[red1_idx]
                                r_bot = mn_arr[red1_idx]
                            else:
                                r_top = h_arr[red1_idx]
                                r_bot = l_arr[red1_idx]

                            if math.isfinite(r_top) and math.isfinite(r_bot) and r_top > r_bot:
                                mss_dir = -1
                                bb_dir = -1

        # ── PREMIUM/DISCOUNT D3 更新（pdbb.rs:838-860）───────────────────────
        # 当 bb_dir != 0（block active），扫描 ZigZag 最近 HH/LL：
        #   pd_premium_top = last confirmed HH（ZigZag d==+1 中最新的 y）
        #   pd_discount_bottom = last confirmed LL（ZigZag d==-1 中最新的 y）
        if bb_dir != 0:
            # 扫描 ZigZag 环形缓冲找最近 HH 和 LL
            cur_hh: float = math.nan
            cur_ll: float = math.nan
            for k in range(_ZigZag.SIZE):
                pt = zz.get(k)
                if pt.d == 0:
                    continue
                if pt.d == 1 and math.isnan(cur_hh) and math.isfinite(pt.y):
                    cur_hh = pt.y
                elif pt.d == -1 and math.isnan(cur_ll) and math.isfinite(pt.y):
                    cur_ll = pt.y
                # 找到两者即可停止
                if math.isfinite(cur_hh) and math.isfinite(cur_ll):
                    break

            if math.isfinite(cur_hh):
                pd_premium_top = cur_hh
            if math.isfinite(cur_ll):
                pd_discount_bottom = cur_ll

        # ── CONTINUOUS FACTOR（continuous_factors.rs:237-241）────────────────
        if (
            bb_dir != 0
            and math.isfinite(close_i)
            and math.isfinite(pd_discount_bottom)
            and math.isfinite(pd_premium_top)
        ):
            half_range = (pd_premium_top - pd_discount_bottom) / 2.0
            if half_range > 0:
                mid_ref = (pd_discount_bottom + pd_premium_top) / 2.0
                raw = (mid_ref - close_i) / half_range
                # clamp to [-1, 1]（fail-closed: non-finite → nan）
                out[i] = max(-1.0, min(1.0, raw))
        # 否则 out[i] 保持 nan（fail-closed）

    return out


def pdbb_factor(
    candles: list[Any],
    *,
    length: int = 5,
    r2a: float = 2.0,
    r2b: float = 3.0,
    r2c: float = 4.0,
    breakerCandleOnlyBody: bool = False,
    breakerCandle_2Last: bool = False,
    onlyWhenInPDarray: bool = False,
    tillFirstBreak: bool = True,
    full_history: bool = False,
) -> float | None:
    """PDBB 因子末值标量包装器（供 parity 测试 + 末值消费）。

    Returns:
        series 最后一个有限值；若无有限值（序列过短或无 active block）→ None。
    """
    if not candles:
        return None
    series = pdbb_series(
        candles,
        length=length,
        r2a=r2a,
        r2b=r2b,
        r2c=r2c,
        breakerCandleOnlyBody=breakerCandleOnlyBody,
        breakerCandle_2Last=breakerCandle_2Last,
        onlyWhenInPDarray=onlyWhenInPDarray,
        tillFirstBreak=tillFirstBreak,
        full_history=full_history,
    )
    finite_vals = series[np.isfinite(series)]
    if len(finite_vals) == 0:
        return None
    return float(finite_vals[-1])
