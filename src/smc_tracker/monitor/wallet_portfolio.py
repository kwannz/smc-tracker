"""钱包完整持仓画像 + 持久化注册表。

功能：
- WalletSnapshot：单地址快照（账户净值/总名义/所有非空持仓）
- WalletPortfolio.refresh：并发拉取所有观察钱包的完整持仓，落库 wallet_positions_full +
  watched_wallets
- WalletPortfolio.fmt：格式化单地址画像（控制台/推送）
- WalletPortfolio.snapshot_rows：供 dashboard，从 latest_wallet_positions 读最新持仓
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..models import Position
from ..util import to_float as _f
from ..util import fmt_px as _fmt_px
from .position_lifecycle import PositionLifecycle, fmt_hold, reconstruct as _reconstruct

if TYPE_CHECKING:
    from ..config import WatchAddress
    from ..storage.db import Store

log = logging.getLogger("wallet_portfolio")


# ---------------------------------------------------------------------------
# 紧凑金额格式化（本地私有，不污染 util）
# ---------------------------------------------------------------------------

def _usd(v: float | None) -> str:
    """将 USD 数值格式化为紧凑字符串（K/M/B），None → '—'。"""
    if v is None:
        return "—"
    n = float(v)
    abs_n = abs(n)
    prefix = "$" if n >= 0 else "-$"
    if abs_n >= 1e9:
        return f"{prefix}{abs_n / 1e9:.2f}B"
    if abs_n >= 1e6:
        return f"{prefix}{abs_n / 1e6:.2f}M"
    if abs_n >= 1e3:
        return f"{prefix}{abs_n / 1e3:.1f}K"
    return f"{prefix}{abs_n:,.2f}"


def _px(v: float | None) -> str:
    """价格格式化：None → '—'，其余用统一非科学计数法格式器（见 util.fmt_px）。"""
    if v is None:
        return "—"
    return _fmt_px(v)


# ---------------------------------------------------------------------------
# 快照数据类
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class WalletSnapshot:
    """单地址某时刻的完整持仓快照。"""
    address: str
    label: str
    account_value: float          # 账户净值 USD
    total_ntl_pos: float          # 总持仓名义 USD
    positions: list[Position]     # 所有非空持仓（已过滤 szi==0）
    ts: int                       # 快照 ms 时间戳
    lifecycles: dict[str, PositionLifecycle] = field(default_factory=dict)  # coin → 生命周期

    @property
    def is_empty(self) -> bool:
        """空画像：无任何持仓 **且** 账户净值可忽略（<$100）。

        用户#：净值$0.00/持仓0个/无币种方向 的快照是纯噪声，周期推送应跳过。
        但「0 持仓 + 可观净值」（离场持币）仍是信息，不算空——只滤掉真正空壳/休眠地址。
        """
        return not self.positions and self.account_value < 100.0


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class WalletPortfolio:
    """观察钱包完整持仓画像管理器。"""

    def __init__(self, store: "Store", rest_url: str) -> None:
        self.store = store
        self.rest_url = rest_url

    async def refresh(
        self,
        wallets: "list[WatchAddress]",
        now_ms: int,
        info: Any = None,
    ) -> list[WalletSnapshot]:
        """并发拉取所有 wallets 的完整持仓，落库，返回快照列表。

        :param wallets: 要拉取的钱包列表（WatchAddress）
        :param now_ms:  当前 ms 时间戳
        :param info:    若传入则复用（便于测试注入 mock），否则自建 HyperliquidInfo
        :return:        每个成功拉取的钱包的 WalletSnapshot
        """
        from ..hyperliquid.info_client import HyperliquidInfo

        snaps: list[WalletSnapshot] = []
        sem = asyncio.Semaphore(6)

        async def _fetch_one(w: "WatchAddress", client: Any) -> WalletSnapshot | None:
            async with sem:
                try:
                    state = await client.clearinghouse_state(w.address)
                    ms = state.get("marginSummary", {})
                    account_value = _f(ms.get("accountValue"))
                    total_ntl_pos = _f(ms.get("totalNtlPos"))
                    positions: list[Position] = []
                    for ap in state.get("assetPositions", []):
                        p = ap.get("position", {})
                        szi = _f(p.get("szi"))
                        if szi == 0:
                            continue
                        lev = p.get("leverage", {})
                        liq_raw = p.get("liquidationPx")
                        positions.append(Position(
                            coin=p.get("coin", ""),
                            szi=szi,
                            entry_px=_f(p.get("entryPx")),
                            position_value=_f(p.get("positionValue")),
                            unrealized_pnl=_f(p.get("unrealizedPnl")),
                            leverage=_f(lev.get("value")) if isinstance(lev, dict) else 0.0,
                            liquidation_px=_f(liq_raw) if liq_raw else None,
                        ))

                    # 拉取近期成交（最近 2000 笔），重建持仓生命周期
                    lifecycles: dict[str, PositionLifecycle] = {}
                    try:
                        fills = await client.user_fills(w.address)
                        lifecycles = _reconstruct(fills, now_ms)
                    except Exception as fill_exc:  # noqa: BLE001
                        log.warning("拉取 fills 失败 %s: %s（持仓时间降级）",
                                    w.address[:10], fill_exc)

                    snap = WalletSnapshot(
                        address=w.address,
                        label=w.label,
                        account_value=account_value,
                        total_ntl_pos=total_ntl_pos,
                        positions=positions,
                        ts=now_ms,
                        lifecycles=lifecycles,
                    )
                    # 落库：持仓快照（含开仓时间/平仓时间/持仓时长）
                    rows = []
                    for pos in positions:
                        lc = lifecycles.get(pos.coin)
                        open_ms_val = lc.open_ms if lc else None
                        last_close_ms_val = lc.last_close_ms if lc else None
                        hold_sec_val = (
                            (now_ms - lc.open_ms) // 1000
                            if (lc and lc.open_ms > 0)
                            else None
                        )
                        rows.append((
                            w.address,
                            pos.coin,
                            "long" if pos.szi > 0 else "short",
                            pos.szi,
                            pos.entry_px,
                            pos.position_value,
                            pos.unrealized_pnl,
                            pos.leverage,
                            pos.liquidation_px,
                            now_ms,
                            open_ms_val,
                            last_close_ms_val,
                            hold_sec_val,
                        ))
                    self.store.save_wallet_positions(rows)
                    # 落库：watched_wallets 注册表
                    self.store.upsert_wallet(
                        address=w.address,
                        label=w.label,
                        source="discover",
                        ts=now_ms,
                        account_value=account_value,
                        total_ntl_pos=total_ntl_pos,
                        n_positions=len(positions),
                    )
                    return snap
                except Exception as e:  # noqa: BLE001
                    log.warning("拉取持仓失败 %s: %s", w.address[:10], e)
                    return None

        async def _run_all(client: Any) -> None:
            tasks = [asyncio.create_task(_fetch_one(w, client)) for w in wallets]
            results = await asyncio.gather(*tasks, return_exceptions=False)
            for r in results:
                if r is not None:
                    snaps.append(r)

        if info is not None:
            # 使用注入的 info（测试 mock）
            await _run_all(info)
        else:
            async with HyperliquidInfo(self.rest_url) as client:
                await _run_all(client)

        return snaps

    def fmt(self, snap: WalletSnapshot, top: int = 12) -> str:
        """格式化单地址完整持仓画像。

        标题行：地址缩写 标签 净值 总名义 持仓数
        持仓行：币种 方向 名义 入场 uPnL 杠杆 爆仓（按名义绝对值降序取 top）
        """
        addr = snap.address
        short = addr[:6] + "…" + addr[-4:] if len(addr) > 10 else addr
        label = snap.label or addr[:8]
        n = len(snap.positions)
        header = (
            f"🏦 {label}({short}) "
            f"净值{_usd(snap.account_value)} "
            f"总名义{_usd(snap.total_ntl_pos)} "
            f"持仓{n}个"
        )
        lines = [header]
        # 按名义价值绝对值降序排，取前 top 个
        sorted_pos = sorted(snap.positions, key=lambda p: abs(p.position_value), reverse=True)
        for pos in sorted_pos[:top]:
            direction_tag = "多🟢" if pos.szi > 0 else "空🔴"
            liq = _px(pos.liquidation_px) if pos.liquidation_px else "—"
            # 开仓时间 + 持仓时长
            lc = snap.lifecycles.get(pos.coin) if snap.lifecycles else None
            lifecycle_str = ""
            if lc and lc.open_ms > 0:
                open_local = time.strftime("%m-%d %H:%M", time.localtime(lc.open_ms / 1000))
                hold_str = fmt_hold(lc.open_ms, snap.ts)
                lifecycle_str = f" 开仓{open_local} 持仓{hold_str}"
            lines.append(
                f"  {pos.coin} {direction_tag} "
                f"名义{_usd(pos.position_value)} "
                f"入场{_px(pos.entry_px)} "
                f"uPnL{_usd(pos.unrealized_pnl)} "
                f"{pos.leverage:.0f}x "
                f"爆仓{liq}"
                f"{lifecycle_str}"
            )
        if n > top:
            lines.append(f"  … 另 {n - top} 个持仓省略")
        return "\n".join(lines)

    def snapshot_rows(self, addresses: list[str], now_ms: int) -> list[dict]:
        """供 dashboard，从 latest_wallet_positions 读每地址最新持仓。

        返回 [{address,label,account_value,total_ntl_pos,n_positions,
                positions:[{coin,direction,position_value,entry_px,
                            unrealized_pnl,leverage,liquidation_px}]}]
        """
        # 从 watched_wallets 取 label/account_value 等元数据
        wallet_meta: dict[str, tuple] = {}
        try:
            for row in self.store.load_wallets():
                # row: (address,label,source,first_seen_ms,last_seen_ms,
                #        account_value,total_ntl_pos,n_positions)
                wallet_meta[row[0]] = row
        except Exception:  # noqa: BLE001
            pass

        result = []
        for addr in addresses:
            meta = wallet_meta.get(addr)
            label = meta[1] if meta else ""
            account_value = meta[5] if meta else None
            total_ntl_pos = meta[6] if meta else None
            n_positions = meta[7] if meta else 0

            positions: list[dict] = []
            try:
                pos_rows = self.store.latest_wallet_positions(addr)
                for r in pos_rows:
                    # r: (address,coin,direction,szi,entry_px,position_value,
                    #      unrealized_pnl,leverage,liquidation_px,ts,
                    #      open_ms,last_close_ms,hold_sec)
                    positions.append({
                        "coin": r[1],
                        "direction": r[2],
                        "position_value": r[5],
                        "entry_px": r[4],
                        "unrealized_pnl": r[6],
                        "leverage": r[7],
                        "liquidation_px": r[8],
                        "open_ms": r[10] if len(r) > 10 else None,
                        "last_close_ms": r[11] if len(r) > 11 else None,
                        "hold_sec": r[12] if len(r) > 12 else None,
                    })
            except Exception:  # noqa: BLE001
                pass

            result.append({
                "address": addr,
                "label": label,
                "account_value": account_value,
                "total_ntl_pos": total_ntl_pos,
                "n_positions": n_positions,
                "positions": positions,
            })
        return result
