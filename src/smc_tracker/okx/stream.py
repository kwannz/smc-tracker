"""OKX 永续实时 streaming 核心逻辑（可被 CLI 和脚本共用）。

把已验证的 OKX 组件串成 streaming：按 OI 排名选 top_n 永续 → WS 订阅
trades/OI/mark → 实时净主动流向 + OI 异动。返回摘要文本供 CLI 打印或推送。

用法：
    from smc_tracker.okx.stream import run_stream
    summary = await run_stream(top_n=10, secs=15.0)
"""
from __future__ import annotations

import asyncio

from .client import OKXClient
from .ws_client import OKXWSClient


async def select_top_insts(
    client: OKXClient, top_n: int
) -> tuple[list[str], dict[str, float]]:
    """按全市场 OI(美元) 排名选 top_n 个 USDT 永续，返回 (inst_ids, ctVal 映射)。"""
    oi = await client.all_open_interest()   # {inst_id: {oi_ccy, oi_usd, ...}}
    meta = await client.swap_meta()          # {inst_id: {ct_val, ct_val_ccy}}
    ranked = sorted(oi.items(), key=lambda kv: kv[1].get("oi_usd", 0.0), reverse=True)
    insts = [k for k, _ in ranked if k in meta][:top_n]
    ct_val = {i: meta[i]["ct_val"] for i in insts}
    return insts, ct_val


def fmt_flow_signals(signals: list[dict]) -> str:
    """格式化 OKX 净流向抓庄信号为文本。long→🟢 short→🔴；空列表 → "无"。"""
    if not signals:
        return "无"
    parts: list[str] = []
    for s in signals:
        coin = s.get("coin", "")
        direction = s.get("direction", "")
        net = s.get("net_flow", 0.0)
        mark = "🟢" if direction == "long" else "🔴"
        parts.append(f"{coin} {mark}{direction} ${abs(net):,.0f}")
    return " / ".join(parts)


def funding_flow_divergence(
    funding: float,
    net_flow: float,
    funding_th: float = 0.0001,
    flow_th: float = 300_000.0,
) -> dict | None:
    """资金费(杠杆拥挤) × taker 净流向(实际方向) 背离判定。

    多头拥挤(funding≥th) 但 taker 净卖(net≤-th) → bearish/distribution(分销)；
    空头拥挤(funding≤-th) 但 taker 净买(net≥th) → bullish/accumulation(吸筹)；
    其余(同向/量级不足) → None。
    """
    if funding >= funding_th and net_flow <= -flow_th:
        return {"direction": "bearish", "kind": "distribution",
                "funding": funding, "net_flow": net_flow}
    if funding <= -funding_th and net_flow >= flow_th:
        return {"direction": "bullish", "kind": "accumulation",
                "funding": funding, "net_flow": net_flow}
    return None


def detect_divergences(latest: dict, net_by_coin: dict) -> list[tuple[str, dict]]:
    """遍历各 inst 的 funding × net_flow 检测背离，返回 [(coin, sig), ...]。

    latest      : monitor.all_latest() → {inst_id: {coin, funding, ...}}
    net_by_coin : monitor.all_net_flows() → {coin: net_flow_usd}
    """
    out: list[tuple[str, dict]] = []
    for snap in latest.values():
        coin = snap.get("coin", "")
        fr = snap.get("funding")
        if fr is None or not coin:
            continue
        sig = funding_flow_divergence(float(fr), net_by_coin.get(coin, 0.0))
        if sig:
            out.append((coin, sig))
    return out


async def run_stream(
    top_n: int = 10,
    secs: float = 15.0,
    rest_url: str = "https://www.okx.com",
) -> str:
    """跑 OKX 永续 streaming，打印并返回净流向 + OI 异动摘要文本。

    参数
    ----
    top_n    : 按 OI 排名监控的永续数量
    secs     : 订阅持续秒数
    rest_url : OKX REST 基础 URL（用于 OKXClient）

    返回
    ----
    摘要文本字符串（净流向 Top + OI 异动）
    """
    # 1. REST 查询 top_n 永续的合约元数据
    async with OKXClient(base=rest_url) as client:
        insts, ct_val = await select_top_insts(client, top_n)

    inst_to_coin = {i: i.split("-")[0] for i in insts}

    # 2. 构建 WS 客户端 + monitor（延迟导入 OKXPerpMonitor 避免循环引用）
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor  # noqa: PLC0415
    ws = OKXWSClient()
    surges: list[dict] = []
    monitor = OKXPerpMonitor(
        inst_ids=insts,
        inst_to_coin=inst_to_coin,
        ct_val=ct_val,
        ws=ws,
        store=None,
        surge_pct=0.02,
        on_surge=surges.append,
    )
    monitor.attach()

    # 3. 订阅并运行指定秒数
    task = asyncio.create_task(ws.run())
    try:
        await asyncio.sleep(secs)
    finally:
        await ws.stop()
        task.cancel()

    # 4. 汇总净流向
    flows = sorted(
        monitor.all_net_flows().items(),
        key=lambda kv: abs(kv[1]),
        reverse=True,
    )

    lines: list[str] = []
    header = f"OKX top{top_n} perp · {secs:g}s streaming · {monitor.trades_seen} trades"
    lines.append(header)
    print(header)

    lines.append("net taker flow (USD):")
    print("net taker flow (USD):")
    for coin, nf in flows[:12]:
        side = "buy " if nf > 0 else "sell"
        row = f"  {coin:<8} {side} ${abs(nf):,.0f}"
        lines.append(row)
        print(row)

    if surges:
        surge_hdr = f"OI surge {len(surges)}:"
        lines.append(surge_hdr)
        print(surge_hdr)
        for e in surges[:8]:
            row = f"  {e['coin']:<8} {e['change']*100:+.1f}%  OI=${e['oi_usd']:,.0f}"
            lines.append(row)
            print(row)

    return "\n".join(lines)


async def run_okx_streaming(store: object, okx_cfg: object) -> None:
    """常驻 OKX streaming 任务：按 OI 排名选 top_n 永续 → 实时落库 okx_perp。

    供 TradingSystem.run() 作为独立 asyncio.Task 运行，可通过 cancel() 优雅退出。
    store    : smc_tracker.storage.Store 实例（落库 okx_perp）。
    okx_cfg  : smc_tracker.config.OKXCfg 实例（ws_url/rest_url/top_n/surge_pct）。
    """
    from smc_tracker.monitor.okx_perp_monitor import OKXPerpMonitor  # noqa: PLC0415

    # 1. REST 查询 top_n 永续元数据
    top_n: int = getattr(okx_cfg, "top_n", 20)
    rest_url: str = getattr(okx_cfg, "rest_url", "https://www.okx.com")
    ws_url: str = getattr(okx_cfg, "ws_url", "wss://ws.okx.com:8443/ws/v5/public")
    surge_pct: float = getattr(okx_cfg, "surge_pct", 0.05)

    async with OKXClient(base=rest_url) as client:
        insts, ct_val = await select_top_insts(client, top_n)

    inst_to_coin: dict[str, str] = {i: i.split("-")[0] for i in insts}

    # 2. 构建 WS + Monitor
    ws = OKXWSClient(ws_url=ws_url)
    monitor = OKXPerpMonitor(
        inst_ids=insts,
        inst_to_coin=inst_to_coin,
        ct_val=ct_val,
        ws=ws,
        store=store,
        surge_pct=surge_pct,
        on_surge=None,  # app 层不需要回调；OI 异动已由 monitor 内部落库
    )
    monitor.attach()

    # 3. 周期 flush 落库 + WS 常驻（可被 cancel 打断）
    flush_interval: float = 5.0
    ws_task = asyncio.create_task(ws.run())
    try:
        while True:
            await asyncio.sleep(flush_interval)
            monitor.flush()
    except asyncio.CancelledError:
        pass
    finally:
        await ws.stop()
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
