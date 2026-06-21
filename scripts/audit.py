"""数据真实性审计 / Debugger。

目标：证明系统消费的是**真实的 Hyperliquid 链上数据**，且我们的解析正确。
方法：多源交叉验证（任一来源造假都会暴露）。

审计项：
  A. 价格三源一致性：REST allMids vs WS allMids vs REST l2Book 中价（三条独立链路）
  B. 跨主机账户净值一致：排行榜主机(stats-data) vs 主 API(api) 的 accountValue
  C. 持仓解析校验：positionValue ≈ |szi| × markPx（用 metaAndAssetCtxs 的真实 markPx）
  D. 符号约定校验：找到真实空头仓位(szi<0)验证 is_long/is_flat
  E. 成交解析+分类校验：真实 userFills 的 side(B/A)/dir 与我们的分类一致
  F. WS 实时一致性：webData2 解析出的持仓与 REST 持仓一致

运行：./.venv/bin/python scripts/audit.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.hyperliquid import HyperliquidInfo, HyperliquidWSClient, Subscription  # noqa: E402
from smc_tracker.monitor import AddressMonitor, fetch_leaderboard_rows  # noqa: E402
from smc_tracker.config import WatchAddress  # noqa: E402

PASS, FAIL = "✅ PASS", "❌ FAIL"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL}  {name}  {detail}")


async def top_whales(n=8) -> list[dict]:
    # 复用项目 fetch_leaderboard_rows（180s 读超时 + gzip）：排行榜已涨到 ~16.8MB，
    # 旧的 urllib 20s 超时会被慢下载压垮（#51 审计发现，与 #41 同根）。
    rows = await fetch_leaderboard_rows()
    def pnl(r):
        for w in r.get("windowPerformances", []):
            if w and len(w) >= 2 and w[0] == "allTime" and isinstance(w[1], dict):
                return float(w[1].get("pnl", 0))
        return 0.0
    rows.sort(key=pnl, reverse=True)
    return rows[:n]


async def main() -> int:
    print("=" * 70)
    print("Hyperliquid 数据真实性审计")
    print("=" * 70)

    async with HyperliquidInfo() as info:
        # ---- A. 价格三源一致性 ----
        print("\n[A] 价格三源一致性 (BTC)")
        rest_mids = await info.all_mids()
        btc_rest = float(rest_mids["BTC"])
        l2 = await info._post({"type": "l2Book", "coin": "BTC"})
        levels = l2["levels"]
        best_bid = float(levels[0][0]["px"]); best_ask = float(levels[1][0]["px"])
        btc_l2 = (best_bid + best_ask) / 2
        # WS allMids
        ws = HyperliquidWSClient()
        got = asyncio.Event(); ws_mid = {"v": None}
        def on_mids(d, _):
            if ws_mid["v"] is None and "BTC" in d.get("mids", {}):
                ws_mid["v"] = float(d["mids"]["BTC"]); got.set()
        ws.subscribe(Subscription(type="allMids"), on_mids)
        run = asyncio.create_task(ws.run())
        try:
            await asyncio.wait_for(got.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass
        btc_ws = ws_mid["v"]
        print(f"      REST allMids={btc_rest}  WS allMids={btc_ws}  L2中价={btc_l2:.1f}  "
              f"(盘口 {best_bid}/{best_ask})")
        spread = best_ask - best_bid
        ok_a = (btc_ws is not None
                and abs(btc_rest - btc_l2) <= max(spread * 2, btc_rest * 0.001)
                and abs(btc_rest - btc_ws) <= max(spread * 5, btc_rest * 0.002))
        check("A 三源价格一致(在合理价差内)", ok_a,
              f"|REST-L2|={abs(btc_rest-btc_l2):.1f} |REST-WS|={abs(btc_rest-(btc_ws or 0)):.1f}")

        # 真实 markPx（用于持仓校验）
        meta_ctx = await info._post({"type": "metaAndAssetCtxs"})
        universe = meta_ctx[0]["universe"]; ctxs = meta_ctx[1]
        mark = {universe[i]["name"]: float(ctxs[i]["markPx"])
                for i in range(len(universe)) if ctxs[i].get("markPx")}

        # ---- 选一个有持仓的真实巨鲸 ----
        print("\n[选取真实地址] 从排行榜取前 8 名，挑第一个有未平仓持仓的")
        whales = await top_whales(8)
        chosen = None; chosen_state = None; chosen_lb = None
        for w in whales:
            addr = w["ethAddress"]
            state = await info.clearinghouse_state(addr)
            if state.get("assetPositions"):
                chosen, chosen_state, chosen_lb = addr, state, w
                break
        if not chosen:
            check("选到有持仓的地址", False, "前8名均空仓")
            await ws.stop(); run.cancel()
            return 1
        print(f"      选中 {chosen}  (排行榜 acctValue={chosen_lb.get('accountValue')})")
        positions = await info.positions(chosen)

        # ---- B. 解析器聚合 vs API 自报总持仓值 ----
        # 注：排行榜 accountValue 是聚合口径(perp+spot+滞后快照)，与 perp clearinghouseState
        #     的 accountValue 不可直接比。正确的真实性交叉校验：我们解析出的所有持仓名义价值之和
        #     应等于 API 自己汇总的 marginSummary.totalNtlPos。
        print("\n[B] 解析器持仓聚合 vs API totalNtlPos")
        our_total = sum(p.position_value for p in positions)
        api_total = float(chosen_state["marginSummary"]["totalNtlPos"])
        dev_b = abs(our_total - api_total) / api_total if api_total else 1.0
        ok_b = dev_b < 0.005
        check("B Σ(我们的 positionValue) == API totalNtlPos", ok_b,
              f"我们={our_total:,.0f} vs API={api_total:,.0f} (偏差 {dev_b*100:.3f}%)")

        # ---- C. 持仓解析 positionValue ≈ |szi|×markPx ----
        print("\n[C] 持仓解析校验 (我们的 positions() 解析器)")
        ok_c = True; n_short = 0
        for p in positions:
            mk = mark.get(p.coin)
            recomputed = abs(p.szi) * mk if mk else None
            dev = abs(recomputed - p.position_value) / p.position_value if recomputed and p.position_value else None
            tag = "多" if p.is_long else "空"
            if p.szi < 0:
                n_short += 1
            flag = "" if (dev is None or dev < 0.02) else "  ⚠超2%"
            if dev is not None and dev >= 0.02:
                ok_c = False
            print(f"      {p.coin:>6} {tag} szi={p.szi:>12.4f} 仓位值=${p.position_value:>14,.0f} "
                  f"|szi|×mark=${(recomputed or 0):>14,.0f} 偏差={'%.3f%%'%(dev*100) if dev is not None else 'NA'}{flag} "
                  f"uPnL={p.unrealized_pnl:+,.0f} lev={p.leverage:g}x")
        check("C positionValue≈|szi|×markPx (偏差<2%)", ok_c, f"{len(positions)}个持仓")

        # ---- D. 符号约定 ----
        print("\n[D] 多空符号约定校验")
        # 在所有巨鲸里凑齐至少一个空头来验证
        if n_short == 0:
            for w in whales:
                ps = await info.positions(w["ethAddress"])
                shorts = [p for p in ps if p.szi < 0]
                if shorts:
                    sp = shorts[0]
                    print(f"      在 {w['ethAddress'][:10]} 找到空头 {sp.coin} szi={sp.szi}")
                    check("D 空头 szi<0 且 is_long=False/is_flat=False",
                          (not sp.is_long) and (not sp.is_flat), "")
                    n_short = 1
                    break
            if n_short == 0:
                check("D 找到真实空头验证符号", False, "样本里无空头(非bug)")
        else:
            sp = next(p for p in positions if p.szi < 0)
            check("D 空头 szi<0 且 is_long=False/is_flat=False",
                  (not sp.is_long) and (not sp.is_flat), f"{sp.coin} szi={sp.szi}")

        # ---- E. 成交解析 + 分类 ----
        print("\n[E] 真实 userFills 解析 + 分类校验")
        fills = await info.user_fills(chosen)
        print(f"      拉到 {len(fills)} 笔近期成交，展示最近 5 笔：")
        from smc_tracker.monitor.address_monitor import _classify
        ok_e = True
        for f in fills[:5]:
            before = f.start_position
            signed = f.sz if f.side.name == "BUY" else -f.sz
            after = before + signed
            etype = _classify(before, after) if before != after else None
            # 用 Hyperliquid 自带 dir 字段交叉验证方向语义
            print(f"      {f.coin:>6} side={f.side.name:<4} sz={f.sz:<10g} px={f.px:<10g} "
                  f"dir='{f.dir}' startPos={before:g}→{after:g} 我们的分类={etype.value if etype else 'NA'} "
                  f"pnl={f.closed_pnl:+g}")
            # 校验：dir 含 Open/Long/Short 等与 side 不矛盾
            if f.side.name == "BUY" and "Short" in f.dir and "Close" not in f.dir and "Long" not in f.dir:
                ok_e = False  # 买单不应是纯开空
        check("E userFills 解析且 side/dir/分类自洽", ok_e and len(fills) > 0, f"{len(fills)}笔")

        # ---- F. WS webData2 与 REST 持仓一致 ----
        print("\n[F] WS webData2 实时持仓 vs REST 持仓一致性")
        mon = AddressMonitor([WatchAddress(chosen, "audit")], ws=ws, on_event=lambda e: None)
        wd_got = asyncio.Event()
        orig = mon._on_web_data2
        def wrap(d, n):
            orig(d, n)
            if (d.get("user") or "").lower() == chosen.lower():
                wd_got.set()
        ws.subscribe(Subscription(type="webData2", user=chosen), wrap)
        try:
            await asyncio.wait_for(wd_got.wait(), timeout=20)
            mismatches = []
            for p in positions:
                ws_szi = mon.position(chosen, p.coin)
                if abs(ws_szi - p.szi) > abs(p.szi) * 0.001 + 1e-9:
                    mismatches.append(f"{p.coin}: ws={ws_szi} rest={p.szi}")
            check("F webData2 持仓 == REST 持仓", not mismatches,
                  "全部一致" if not mismatches else f"不一致: {mismatches}")
        except asyncio.TimeoutError:
            check("F 收到 webData2 帧", False, "20s 内未收到(可能该地址 ws 推送较慢)")

        await ws.stop(); run.cancel()

    # ---- 汇总 ----
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"审计结果: {passed}/{len(results)} 项通过")
    for name, ok, _ in results:
        print(f"  {PASS if ok else FAIL}  {name}")
    print("=" * 70)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
