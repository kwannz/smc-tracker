"""谐波增量计算状态机（HarmonicState）。

实现目标：
  每次喂入一根 K 线（update），通过 MarketStructure 增量引擎更新 swing 流，
  然后复用 harmonic.py 的几何函数（_alternate_immutable / detect_xabcd / project_prz）
  组装出与 analyze_candles 完全等价的结构。

不变量（保真等价）：
  对同一 K 线序列，HarmonicState 逐根 update 后的 snapshot() ==
  analyze_candles(candles, order=order, tol=tol)，逐字段完全相等。

设计决策：
  - update + snapshot 共用同一内部 _compute（单一计算路径，去重）。
  - _ms 是 append-only MarketStructure（只增量 update，不重建），保证 no-repaint。
  - _CandleAdapter 直接从 harmonic.py 复用（不重写），适配 close_time_ms 缺失。
  - 几何函数（_alternate_immutable/detect_xabcd/project_prz）全部 import 复用，不重写。

Author: Claude Code
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .harmonic import (
    _alternate_immutable,
    _CandleAdapter,
    _COMPLETED_MAX_DIST,
    _merge_completed_by_d,
    detect_xabcd,
    project_prz,
)
from ..smc.structure import MarketStructure

log = logging.getLogger("harmonic_state")


@dataclass(slots=True)
class HarmonicState:
    """谐波形态增量状态机。

    每次喂一根 K 线调用 update(candle)，内部维护 MarketStructure append-only swing 流，
    随时可调 snapshot() 获取与 analyze_candles 保真等价的结果。

    参数：
        order: 分形邻域大小（默认 2 = 高灵敏，与 analyze_candles 新默认对齐）。
        tol:   比率容差（默认 0.07 = 7%，与 analyze_candles 新默认对齐）。
    ⚡ 高灵敏模式（order=2 / tol=7%）：含更多早期形态，误检率上升，止损必执行。
    """

    order: int = 2    # spec §3 C：order 3→2（高灵敏，与 analyze_candles/HarmonicCfg 对齐）
    tol: float = 0.07  # spec §3 C：tol 0.05→0.07（宽容比率，与 analyze_candles/HarmonicCfg 对齐）

    # 内部：增量 MarketStructure（append-only，绝不重建）
    _ms: MarketStructure = field(init=False, repr=False)
    # 当前已喂入的 K 线计数（用于 _CandleAdapter 时间戳补零）
    _n: int = field(default=0, init=False, repr=False)
    # 最后一根 K 线的 close（用于 snapshot price 字段）
    _last_price: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        # MarketStructure 用 lookback=order，与 pivots_from_structure 保持一致
        self._ms = MarketStructure(lookback=self.order)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def update(self, candle: Any) -> dict:
        """增量喂入一根 K 线，返回当前谐波快照（与 snapshot() 等价）。

        Args:
            candle: 需有 .h/.l/.c 属性；.close_time_ms 可选（缺省按序号补零）。

        Returns:
            {"completed": [...], "forming": [...], "price": float}
        """
        # 适配 candle（补 close_time_ms），然后增量更新 MarketStructure
        adapted = _CandleAdapter(candle, self._n)
        self._ms.update(adapted)
        self._n += 1
        self._last_price = float(candle.c)
        return self._compute()

    def snapshot(self) -> dict:
        """返回当前谐波快照，不消耗新 K 线（idempotent）。

        Returns:
            {"completed": [...], "forming": [...], "price": float}
        """
        return self._compute()

    # ------------------------------------------------------------------
    # 内部单一计算路径（update 和 snapshot 共用，去重）
    # ------------------------------------------------------------------

    def _compute(self) -> dict:
        """从当前 MarketStructure swing 流计算谐波结果。

        与 analyze_candles 的计算路径逐行对齐（含扩展形态 merge）：
          1. ms.swings → 映射为 (index, price, 'H'|'L') 原始序列
          2. _alternate_immutable → 交替枢轴（first-wins，不回改）
          3. len(pivots) < 5 → 返回 empty
          4. detect_xabcd + detect_all_ext → _merge_completed_by_d
          5. 可操作性过滤（D 在最近枢轴 + 距现价 ±_COMPLETED_MAX_DIST）
          6. project_prz + project_all_ext_prz → forming 合并排序（末 4 枢轴）
        """
        # 局部导入避免循环（harmonic_ext 依赖 harmonic._ratio/_within）
        from .harmonic_ext import (  # noqa: PLC0415
            detect_all_ext,
            project_cypher_prz,
            project_shark_prz,
        )

        price = self._last_price
        empty: dict = {"completed": [], "forming": [], "price": price}

        # --- 步骤 1: swing 流映射 ---
        raw: list[tuple[int, float, str]] = [
            (sw.index, float(sw.price), "H" if sw.kind == "high" else "L")
            for sw in self._ms.swings
        ]

        # --- 步骤 2: 交替化（first-wins，不回改）---
        pivots = _alternate_immutable(raw)

        # --- 步骤 3: 枢轴不足 ---
        if len(pivots) < 5:
            return empty

        # --- 步骤 4: 完整形态检测（经典 + 扩展，同 D_idx 去重保留最高 confidence）---
        completed_classic = detect_xabcd(pivots, tol=self.tol)
        completed_ext = detect_all_ext(pivots, tol=self.tol)
        best_by_d = _merge_completed_by_d([completed_classic, completed_ext])

        # --- 步骤 5: 可操作性过滤（与 analyze_candles 完全对齐）---
        n_pivots = len(pivots)
        recent_cutoff = max(n_pivots - 8, int(n_pivots * 0.40))
        recent_d_idxs = {p[0] for p in pivots[recent_cutoff:]}
        completed = [
            r for r in best_by_d.values()
            if r["points"]["D"][0] in recent_d_idxs
            and price > 0
            and abs(r["points"]["D"][1] - price) / price <= _COMPLETED_MAX_DIST
        ]
        completed.sort(key=lambda r: r["confidence"], reverse=True)

        # --- 步骤 6: 前瞻 forming（末 4 枢轴，经典 + 扩展 Cypher/Shark 合并排序）---
        # 与 analyze_candles 逐行对齐：
        #   - ABCD 前瞻不在此路径（4 点方向逻辑与 XABC 上下文不兼容，避免误报）。
        #   - Cypher/Shark 含自身几何约束（C>A / B<X），结构不满足自动返回 []。
        if len(pivots) >= 4:
            last4 = pivots[-4:]
            X_px = last4[0][1]
            A_px = last4[1][1]
            B_px = last4[2][1]
            C_px = last4[3][1]
            direction = "bull" if A_px > X_px else "bear"
            # 结构次序校验（与 analyze_candles 逐行对齐）
            if direction == "bull":
                order_ok = (X_px < B_px < C_px < A_px)
            else:
                order_ok = (X_px > B_px > C_px > A_px)
            forming_classic = (
                project_prz(X_px, A_px, B_px, C_px, direction=direction, tol=self.tol)
                if order_ok
                else []
            )
            # 扩展前瞻：Cypher/Shark（各含自身几何约束，结构不满足自动返回 []）
            forming_ext = (
                project_cypher_prz(X_px, A_px, B_px, C_px, direction, tol=self.tol)
                + project_shark_prz(X_px, A_px, B_px, C_px, direction, tol=self.tol)
            )
            # 合并：按 confidence 降序（forming 无 D_idx，不做 D_idx 去重）
            forming = sorted(
                forming_classic + forming_ext,
                key=lambda r: r["confidence"],
                reverse=True,
            )
        else:
            forming = []

        return {"completed": completed, "forming": forming, "price": price}
