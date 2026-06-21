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
