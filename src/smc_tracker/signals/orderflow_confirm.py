"""订单流确认层：PRZ（谐波 setup 进场区）的领先信号确认。

设计依据（order-flow 理论）：
- 订单流是**确认层非主信号**：先有结构位（谐波 PRZ=强 S/R），再用订单流确认该位是否真会守。
- 确认信号 = PRZ 处有**同向大挂单墙**（看多找 bid 支撑墙/看空找 ask 压制墙）+ **挂单失衡同向**。

诚实铁律（CLAUDE.md §2 抓庄核心）：
- 墙可能 spoof（虚挂诱导）/吸收 ≠ 必反转（机构可能只是平仓）。
- 仅在 PRZ 处的墙才有意义（非随机位）。
- 本模块仅提供确认信号，不产生独立交易指令。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class OrderflowConfirm:
    """订单流确认结果（值对象，可序列化）。"""

    confirmed: bool       # 订单流是否确认该方向（墙+失衡同向）
    wall_usd: float       # 确认墙名义额（0.0 = PRZ 处无同向墙）
    wall_dist_pct: float  # 墙距进场中点比例（无墙 = 1.0）
    imbalance: float      # 当前挂单失衡（-1..1，正=bid 占优=偏多）
    note: str             # 诚实标注（含 spoof 警告或谨慎说明）


@runtime_checkable
class OBProvider(Protocol):
    """订单簿提供者协议（鸭子类型，兼容 HLOrderbookMonitor）。"""

    def confirming_wall(
        self, coin: str, price: float, side: str, tol_pct: float = 0.015
    ) -> dict | None: ...

    def book_imbalance(self, coin: str) -> dict[str, float]: ...


def confirm_setup(
    coin: str,
    direction: str,           # "long" / "short"
    entry_lo: float,
    entry_hi: float,
    ob_provider: Any,         # 鸭子类型：需有 confirming_wall + book_imbalance；None → 返回 None
    *,
    tol_pct: float = 0.015,
    min_wall_usd: float = 0.0,
) -> OrderflowConfirm | None:
    """判断订单流是否在 PRZ（进场区 entry_lo..entry_hi）处确认谐波 setup 方向。

    参数：
      coin        — 合约代码（如 "BTC"）。
      direction   — "long"（看多）或 "short"（看空）。
      entry_lo    — 进场区下沿价格。
      entry_hi    — 进场区上沿价格。
      ob_provider — 订单簿提供者（None → 无数据，返回 None）。
      tol_pct     — 墙距进场中点最大容忍比例（默认 1.5%）。
      min_wall_usd — 有效墙最小名义额过滤（0.0=不过滤）。

    返回：
      OrderflowConfirm — 含确认结果与诚实标注。
      None             — 参数非法或无订单流数据，调用方应跳过此确认。
    """
    # 无数据提供者 → 无法确认
    if ob_provider is None:
        return None

    # 参数校验
    if direction not in ("long", "short"):
        return None

    entry_mid = (entry_lo + entry_hi) / 2.0
    if entry_mid <= 0:
        return None

    # 看多找 bid 支撑墙；看空找 ask 压制墙
    side = "bid" if direction == "long" else "ask"

    # 查询 PRZ 处同向墙
    wall = ob_provider.confirming_wall(coin, entry_mid, side, tol_pct)

    # 查询挂单失衡
    imb_dict = ob_provider.book_imbalance(coin)
    imbalance: float = imb_dict.get("imbalance", 0.0)

    # 失衡同向判断：long 需 imb>0（bid 占优）；short 需 imb<0（ask 占优）
    imbalance_aligned = (direction == "long" and imbalance > 0) or (
        direction == "short" and imbalance < 0
    )

    # 墙有效性：存在且 notional >= min_wall_usd
    wall_valid = wall is not None and wall["notional"] >= min_wall_usd

    confirmed = wall_valid and imbalance_aligned

    wall_usd = wall["notional"] if wall is not None else 0.0
    wall_dist_pct = wall["dist_pct"] if wall is not None else 1.0

    # 诚实标注（CLAUDE.md §2 抓庄核心：硬编码算法诚实可解释）
    if confirmed:
        note = (
            "PRZ处同向挂单墙+失衡确认(领先意图)；"
            "⚠墙可能spoof/吸收≠必反转，仅确认非保证"
        )
    elif wall is None:
        note = "PRZ处无同向墙确认(谨慎)；订单流未支持该方向"
    elif not wall_valid:
        note = f"PRZ处同向墙名义额不足(实际={wall_usd:.0f}，阈值={min_wall_usd:.0f}，谨慎)"
    else:
        note = "PRZ有同向墙但挂单失衡反向，方向未获整体订单流确认(谨慎)"

    return OrderflowConfirm(
        confirmed=confirmed,
        wall_usd=wall_usd,
        wall_dist_pct=wall_dist_pct,
        imbalance=imbalance,
        note=note,
    )
