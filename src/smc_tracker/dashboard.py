"""SMC 抓庄监控仪表盘 —— 从 SQLite 实时渲染深色主题 HTML，用 aiohttp 起服务。

接口：
  build_dashboard_state(store, now_ms, window_ms) → dict  （组装各 section 数据）
  render_html(state)                               → str  （返回自包含单页 HTML）
  serve(db_path, host, port)                       → None （aiohttp Web 服务）
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiohttp.web
from .dashboard_harmonic import render_harmonic_html, render_harmonic_detail_html  # noqa: F401

# 模板目录：HTML 模板已外置，模块导入时一次性读入并缓存
_TPL_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# 数据层 —— 每个查询独立 try/except，表不存在或为空时返回空列表（参考 report.py _count 写法）
# ---------------------------------------------------------------------------

def _safe_rows(conn: Any, sql: str, params: tuple = ()) -> list[tuple]:
    """防御性 SQL 查询：表不存在/列缺失时返回 []，不抛。"""
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001
        return []


def _row_to_dict(row: tuple, keys: list[str]) -> dict[str, Any]:
    """tuple 行 → dict，按 keys 映射，缺失字段填 None。"""
    return {k: (row[i] if i < len(row) else None) for i, k in enumerate(keys)}


def build_dashboard_state(store: Any, now_ms: int, window_ms: int = 3_600_000) -> dict:
    """从 store.conn 查询近 window_ms 毫秒的数据，组装成可序列化 dict。

    所有 SQL 查询均用 try/except 包裹，表不存在/空库时各 section 返回 []，不抛异常。
    """
    since_ms = now_ms - window_ms
    conn = store.conn
    gen_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ms / 1000))

    # ---- meta ----
    meta: dict[str, Any] = {
        "generated": gen_str,
        "window_min": window_ms // 60_000,
    }

    # ---- 共振信号（signals 表）----
    sig_rows = _safe_rows(
        conn,
        "SELECT ts,coin,direction,score,entry,stop,target,rr FROM signals "
        "WHERE ts>=? ORDER BY ts DESC LIMIT 15",
        (since_ms,),
    )
    signals = [_row_to_dict(r, ["ts", "coin", "direction", "score",
                                 "entry", "stop", "target", "rr"])
               for r in sig_rows]

    # ---- 背离信号（divergence 表）----
    div_rows = _safe_rows(
        conn,
        "SELECT ts,coin,direction,score,funding,dex_flow_usd FROM divergence "
        "WHERE ts>=? ORDER BY ts DESC LIMIT 15",
        (since_ms,),
    )
    divergence = [_row_to_dict(r, ["ts", "coin", "direction", "score",
                                    "funding", "dex_flow_usd"])
                  for r in div_rows]

    # ---- 聪明钱主动净流向 Top（hl_meme_trades 按 coin 聚合）----
    flow_rows = _safe_rows(
        conn,
        "SELECT coin, SUM(CASE WHEN taker_side='B' THEN notional ELSE -notional END) net "
        "FROM hl_meme_trades WHERE time_ms>=? GROUP BY coin ORDER BY ABS(net) DESC LIMIT 12",
        (since_ms,),
    )
    whale_flows = [{"coin": r[0], "net": r[1]} for r in flow_rows]

    # ---- Top 聪明钱地址画像（top_profiles 返回 tuple，列序见 db.py）----
    # 列序: address,score,account_value,alltime_pnl,month_pnl,win_rate,realized_pnl,n_trades,net_bias,fav_coins,ts
    profile_keys = ["address", "score", "account_value", "alltime_pnl",
                    "month_pnl", "win_rate", "realized_pnl", "n_trades",
                    "net_bias", "fav_coins", "ts"]
    try:
        raw_profiles = store.top_profiles(limit=10)
    except Exception:  # noqa: BLE001
        raw_profiles = []
    top_addresses = [_row_to_dict(r, profile_keys) for r in raw_profiles]

    # ---- 庄家集团（AddressCorrelation.clusters_detailed，取前 8）----
    try:
        from .monitor.address_correlation import AddressCorrelation
        clusters_raw = AddressCorrelation(store).clusters_detailed(
            now_ms - 1_800_000, window_sec=120, min_shared=3, min_coins=2
        )
        clusters = clusters_raw[:8]
    except Exception:  # noqa: BLE001
        clusters = []

    # ---- Bitget OI 近窗（防御：列不确定，直接 try/except 返回 []）----
    oi_rows = _safe_rows(
        conn,
        "SELECT symbol, oi_size, funding, ts FROM bitget_oi "
        "WHERE ts>=? GROUP BY symbol HAVING ts=MAX(ts) ORDER BY oi_size DESC LIMIT 15",
        (since_ms,),
    )
    oi_surges = [_row_to_dict(r, ["symbol", "oi_size", "funding", "ts"])
                 for r in oi_rows]

    # ---- 链上大额转账（onchain_transfers 由 OnchainMemeMonitor 自建，可能不存在）----
    onchain_rows = _safe_rows(
        conn,
        "SELECT coin,chain,amount,amount_usd,tx_hash,ts FROM onchain_transfers "
        "WHERE ts>=? ORDER BY ts DESC LIMIT 12",
        (since_ms,),
    )
    onchain = [_row_to_dict(r, ["coin", "chain", "amount", "amount_usd", "tx_hash", "ts"])
               for r in onchain_rows]

    # ---- 鲸鱼信号（whale_signals 表）——同时用作 pump_alerts ----
    # 列：ts,address,label,coin,action,direction,notional,px,pos_after,taker
    ws_rows = _safe_rows(
        conn,
        "SELECT ts,label,coin,direction,notional,px FROM whale_signals "
        "WHERE ts>=? ORDER BY ts DESC LIMIT 12",
        (since_ms,),
    )
    whale_signals = [_row_to_dict(r, ["ts", "label", "coin", "direction", "notional", "px"])
                     for r in ws_rows]

    # ---- 行情监控板（ticker_board）——从 bitget_oi 表聚合最新行情 ----
    # bitget_oi 表只有 symbol/coin/oi_size/oi_usd/mark_px/funding/ts（无 chg24/last_px，
    # 这两个字段仅在内存 _latest 快照中），所以 dashboard 直接用 mark_px 作为价格。
    # chg24 best-effort：用同 symbol「最新 mark_px 对比近 24h 前最早一行 mark_px」估算，算不出置 None。
    ticker_board: list[dict] = []
    try:
        # 步骤 1：取每 symbol 最新一行（ts=MAX(ts)）
        latest_rows = _safe_rows(
            conn,
            "SELECT symbol, coin, mark_px, funding, oi_usd, ts FROM bitget_oi "
            "WHERE (symbol, ts) IN (SELECT symbol, MAX(ts) FROM bitget_oi GROUP BY symbol) "
            "ORDER BY oi_usd DESC",
        )
        for r in latest_rows:
            symbol, coin, mark_px, funding, oi_usd, ts = r
            if not mark_px or float(mark_px) <= 0:
                continue
            # best-effort chg24：查该 symbol 24h 前最近一条 mark_px
            chg24: float | None = None
            since_24h = (now_ms - 86_400_000)
            old_rows = _safe_rows(
                conn,
                "SELECT mark_px FROM bitget_oi "
                "WHERE symbol=? AND ts>=? AND ts<=? "
                "ORDER BY ts ASC LIMIT 1",
                (symbol, since_24h, now_ms - 82_800_000),  # 23~24h 前
            )
            if old_rows and old_rows[0][0]:
                old_px = float(old_rows[0][0])
                new_px = float(mark_px)
                if old_px > 0:
                    chg24 = (new_px - old_px) / old_px
            ticker_board.append({
                "symbol": symbol,
                "coin": coin or symbol,
                "price": float(mark_px),
                "funding": float(funding) if funding is not None else 0.0,
                "oi_usd": float(oi_usd) if oi_usd is not None else 0.0,
                "chg24": chg24,  # 可能为 None（表里无足够历史数据时）
            })
    except Exception:  # noqa: BLE001 — 表不存在/结构不对时返回 []
        ticker_board = []

    # ---- 交易所资金流（exchange_flows 表，每个交易所最新一行）----
    # 按 (exchange, MAX(ts)) 取最新行，按 abs(net) 降序（净流量绝对值大的优先展示）
    ef_rows: list[dict] = []
    try:
        raw_ef = _safe_rows(
            conn,
            "SELECT exchange, chain, inflow, outflow, net, n_tx, n_addr, dt "
            "FROM exchange_flows "
            "WHERE (exchange, ts) IN "
            "(SELECT exchange, MAX(ts) FROM exchange_flows GROUP BY exchange) "
            "ORDER BY ABS(net) DESC",
        )
        ef_rows = [
            _row_to_dict(r, ["exchange", "chain", "inflow", "outflow",
                              "net", "n_tx", "n_addr", "dt"])
            for r in raw_ef
        ]
    except Exception:  # noqa: BLE001 — 表不存在/为空时返回 []
        ef_rows = []

    # ---- 系统健康（数据新鲜度 + 验证闭环积压）----
    try:
        from .health import system_health
        health = system_health(store, now_ms)
    except Exception:  # noqa: BLE001 — 健康检查失败不影响其余面板
        health = {}

    # ---- 预测准确率（诚实回顾：近 24h 已评估，含相对随机边际/样本充分性）----
    try:
        from .review import PredictionReview
        accuracy = PredictionReview(store).accuracy_report(now_ms - 86_400_000, now_ms)
    except Exception:  # noqa: BLE001 — 回顾失败不影响其余面板
        accuracy = {}

    # ---- 钱包持仓画像（去重：复用 WalletPortfolio.snapshot_rows，零重复 SQL）----
    # 地址集与排序沿用 store.load_wallets()（watched_wallets 按 account_value DESC NULLS
    # LAST），snapshot_rows 内部用 load_wallets() 取元数据 + latest_wallet_positions() 取
    # 最新持仓——与原内联 SQL 等价（仅 positions LIMIT 100 vs 旧 50，形状不变）。
    # snapshot_rows 不产出 source 字段，但前端 renderWalletPortfolio 未使用 source，安全。
    wallet_portfolio: list[dict] = []
    try:
        from .monitor.wallet_portfolio import WalletPortfolio
        # snapshot_rows 仅依赖 store（不发网络），rest_url 传空串占位
        addresses = [row[0] for row in store.load_wallets()]
        wallet_portfolio = WalletPortfolio(store, "").snapshot_rows(addresses, now_ms)
    except Exception:  # noqa: BLE001 — 表不存在/结构不对时返回 []
        wallet_portfolio = []

    # ---- OKX 跨所信号（okx_signals 表：资金费×净流向背离）----
    # recent_okx_signals 返回列：(ts, coin, direction, kind, funding, net_flow)，按 ts ASC。
    # dashboard 只展示 coin/direction/kind/net_flow（funding 不入卡，保持表精简）。
    okx_signals: list[dict] = []
    try:
        for r in store.recent_okx_signals(since_ms):
            okx_signals.append({
                "coin": r[1],
                "direction": r[2],
                "kind": r[3],
                "net_flow": r[5],
            })
    except Exception:  # noqa: BLE001 — 表不存在/为空时返回 []
        okx_signals = []

    # ---- OKX 强平级联（okx_liquidations 表）----
    # recent_okx_liquidations 返回列：(ts, coin, pos_side, side, notional_usd, bk_px)，按 ts ASC。
    # notional 取 notional_usd（第 4 列）；前端按 coin 聚合规模成条形图 + 明细表。
    okx_liquidations: list[dict] = []
    try:
        for r in store.recent_okx_liquidations(since_ms):
            okx_liquidations.append({
                "ts": r[0],
                "coin": r[1],
                "pos_side": r[2],
                "side": r[3],
                "notional": r[4],   # notional_usd
            })
    except Exception:  # noqa: BLE001 — 表不存在/为空时返回 []
        okx_liquidations = []

    # ---- HL 挂单墙（hl_orderbook_walls 表：领先意图，可能 spoof）----
    # recent_orderbook_walls 返回列：(ts, coin, side, kind, px, notional)，按 ts ASC。
    okx_walls: list[dict] = []
    try:
        for r in store.recent_orderbook_walls(since_ms):
            okx_walls.append({
                "ts": r[0],
                "coin": r[1],
                "side": r[2],
                "kind": r[3],
                "px": r[4],
                "notional": r[5],
            })
    except Exception:  # noqa: BLE001 — 表不存在/为空时返回 []
        okx_walls = []

    return {
        "meta": meta,
        "health": health,
        "accuracy": accuracy,
        "signals": signals,
        "divergence": divergence,
        "whale_flows": whale_flows,
        "top_addresses": top_addresses,
        "clusters": clusters,
        "oi_surges": oi_surges,
        "onchain": onchain,
        "pump_alerts": whale_signals,   # whale_signals 双用
        "whale_signals": whale_signals,
        "ticker_board": ticker_board,
        "exchange_flows": ef_rows,
        "wallet_portfolio": wallet_portfolio,
        "okx_signals": okx_signals,
        "okx_liquidations": okx_liquidations,
        "okx_walls": okx_walls,
    }


# ---------------------------------------------------------------------------
# 渲染层 —— 自包含单页 HTML，深色主题，JS 每 5 秒 fetch('/api/state') 重渲染
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = (_TPL_DIR / "index.html").read_text(encoding="utf-8")


def render_html(state: dict) -> str:
    """将 build_dashboard_state 的结果渲染成自包含单页 HTML 字符串。

    首屏直接注入 initial state（避免白屏），同时挂 setInterval 5s 拉 /api/state 更新。

    ⚠️ 模板 CSS/JS 用 `.format()` 风格的双括号 `{{`/`}}` 转义；此前直接 .replace 注入
    导致输出残留字面双括号（CSS 失效、JS 模板插值 `${{…}}` 语法错误→页面卡「加载中」）。
    修复：先把 `{{`→`{`、`}}`→`}` 解转义（模板无三连括号/无裸单括号，安全），再注入 JSON
    （JSON 自身的括号在注入后才出现，不受解转义影响）。
    """
    # 序列化注入的 initial state（ensure_ascii=False 支持中文，indent=None 省体积）
    state_json = json.dumps(state, ensure_ascii=False, default=str)
    # 先对模板解转义（{{→{、}}→}），再注入 JSON：注入发生在解转义之后，JSON 自身的
    # 括号（含嵌套对象闭合产生的 `}}`）不经过 .replace，故数据值原样保留。
    # ⚠️ 不可对 state_json 改写括号——含字面 {{/}} 的数据值（如信号 reason）会被腐蚀。
    # 注：紧凑 JSON 永不含 `{{`（每个 { 后必跟 " 或 }），但会含 `}}`（嵌套闭合，合法无害）。
    html = _HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    return html.replace("__INITIAL_STATE__", state_json)


# ---------------------------------------------------------------------------
# Web 服务层 —— aiohttp
# ---------------------------------------------------------------------------

async def serve(db_path: str, host: str = "127.0.0.1", port: int = 8787) -> None:
    """用 aiohttp 起仪表盘服务：GET / 返回 HTML，GET /api/state 返回 JSON。

    使用 from .storage import Store 打开 db_path，每次请求查询当前数据。
    """
    from .storage import Store

    store = Store(db_path)

    async def handle_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
        now_ms = int(time.time() * 1000)
        state = build_dashboard_state(store, now_ms)
        html = render_html(state)
        return aiohttp.web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
        )

    async def handle_api_state(request: aiohttp.web.Request) -> aiohttp.web.Response:
        now_ms = int(time.time() * 1000)
        state = build_dashboard_state(store, now_ms)
        return aiohttp.web.json_response(state, dumps=lambda o: json.dumps(o, default=str))

    async def handle_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /health — 返回数据新鲜度 + 总体状态 JSON。

        HTTP 状态码：ok→200, degraded→200(body 标注), down→503。
        使用 system_health()（唯一 DB 真相源）替代已删除的 _data_freshness/_overall。
        """
        from .health import system_health

        try:
            now_ms = int(time.time() * 1000)
            rep = system_health(store, now_ms)
            overall = rep.get("overall", "unknown")
        except Exception as exc:  # noqa: BLE001
            return aiohttp.web.json_response(
                {"error": str(exc), "overall": "unknown"},
                status=503,
            )
        payload = {"data": rep.get("freshness", []), "overall": overall, "predictions": rep.get("predictions", {})}
        status = 503 if overall == "down" else 200
        return aiohttp.web.json_response(payload, status=status)

    async def handle_harmonic(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /harmonic — 谐波形态独立 HTML 页。"""
        now_ms = int(time.time() * 1000)
        h_state = build_harmonic_state(store, now_ms)
        html = render_harmonic_html(h_state)
        return aiohttp.web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
        )

    async def handle_api_harmonic(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /api/harmonic — 谐波形态 JSON，供前端 setInterval 5s 拉取刷新。"""
        now_ms = int(time.time() * 1000)
        h_state = build_harmonic_state(store, now_ms)
        return aiohttp.web.json_response(
            h_state, dumps=lambda o: json.dumps(o, default=str)
        )

    async def handle_harmonic_list(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /api/harmonic/list — 谐波币列表 JSON（左面板，5s 轮询）。

        查询参数（均可选，prepared 参数防注入）：
          q           : 币名关键词（大小写不敏感，前缀/子串匹配）
          asset_class : 过滤类别（'crypto' 或 'tradfi'；缺省=全部）
          offset      : 分页起点（默认 0）
          limit       : 每页条数（默认 50，最大 500）

        响应体：{ items: [...], total: N, offset: O, limit: L }
        total 为过滤后总数（供前端计算总页数），items 为当前页切片。
        """
        q = (request.rel_url.query.get("q") or "").strip().lower()
        asset_class = (request.rel_url.query.get("asset_class") or "").strip().lower()
        try:
            offset = max(0, int(request.rel_url.query.get("offset") or 0))
        except (ValueError, TypeError):
            offset = 0
        try:
            limit = min(500, max(1, int(request.rel_url.query.get("limit") or 50)))
        except (ValueError, TypeError):
            limit = 50

        # 全量数据从 DB 构建（内存级，表不存在时返回 []）
        lst = build_harmonic_list(store)

        # 服务端过滤（keyword + asset_class）
        if q:
            lst = [r for r in lst if q in (r.get("coin") or "").lower()]
        if asset_class in ("crypto", "tradfi"):
            lst = [r for r in lst if r.get("asset_class") == asset_class]

        total = len(lst)
        items = lst[offset: offset + limit]

        payload = {"items": items, "total": total, "offset": offset, "limit": limit}
        return aiohttp.web.json_response(payload, dumps=lambda o: json.dumps(o, default=str))

    async def handle_harmonic_coin(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /api/harmonic/coin/{coin}?tf=<tf> — 指定币详情 JSON（右面板按需拉取）。"""
        coin = request.match_info.get("coin", "")
        tf = request.rel_url.query.get("tf") or None
        detail = build_coin_detail(store, coin, tf)
        return aiohttp.web.json_response(detail, dumps=lambda o: json.dumps(o, default=str))

    async def handle_harmonic2(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /harmonic2 — 谐波主-详情 SPA HTML。"""
        lst = build_harmonic_list(store)
        html = render_harmonic_detail_html(lst)
        return aiohttp.web.Response(text=html, content_type="text/html", charset="utf-8")

    async def handle_hl2(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET /hl2 — HL 聪明钱追踪终端 HTML 页。"""
        now_ms = int(time.time() * 1000)
        state = build_dashboard_state(store, now_ms)
        html = render_hl_html(state)
        return aiohttp.web.Response(text=html, content_type="text/html", charset="utf-8")

    async def handle_harmonic_discover(request: aiohttp.web.Request) -> aiohttp.web.Response:
        """GET/POST /api/harmonic/discover — 「发现搜集」按钮：扫描更广 Bitget 宇宙
        （按成交额排序、排除已监控/已收集），快扫有谐波形态的币 → 立即落库展示 +
        加入 monitored_coins（监控进程两种模式都并入谐波宇宙持续监控）。返回发现的币。"""
        import time as _t  # noqa: PLC0415
        from .bitget.rest import BitgetREST  # noqa: PLC0415
        from .monitor.harmonic_monitor import HarmonicMonitor  # noqa: PLC0415
        from .util import to_float as _f  # noqa: PLC0415
        now = int(_t.time() * 1000)
        try:
            # 已监控（per-coin latest）+ 已收集的币 → 排除，避免重复扫描
            existing = store.recent_harmonic_setups()
            # B2：recent_harmonic_setups 已改 per-coin latest，各币 ts 可不同。
            # 新发现的币用 now 作 ts（独立于已有币的各自最新 ts，per-coin latest 读取保证各自显示）。
            batch_ts = now
            current = {r[1] for r in existing}
            current |= set(store.get_harmonic_collected())
            current |= set(store.get_monitored_coins())  # 监控清单也排除（统一真相源）
            async with BitgetREST() as bg:
                base_map = await bg.perp_base_coins()   # {symbol: base}
                tickers = await bg.tickers()            # {symbol: ticker}
            # 按 24h 成交额降序的候选 {coin: symbol}，排除已监控
            ranked: list[tuple[float, str, str]] = []
            for sym, base in base_map.items():
                coin = str(base).upper()
                if coin in current:
                    continue
                tk = tickers.get(sym) or {}
                vol = _f(tk.get("quoteVolume") or tk.get("usdtVolume") or 0)
                ranked.append((vol, coin, sym))
            ranked.sort(key=lambda x: x[0], reverse=True)
            candidates: dict[str, str] = {coin: sym for _, coin, sym in ranked[:15]}
            if not candidates:
                return aiohttp.web.json_response({"discovered": [], "scanned": 0, "note": "无新候选币"})
            # 复用 HarmonicMonitor 快扫（全 7 周期；store 共享 K 线缓存/回填）
            # 7 周期 × N 候选币任务量增加，但 HarmonicMonitor 内 Semaphore(≤8) 限流 + DB 缓存可接受
            from .config import CANONICAL_TIMEFRAMES as _DISCOVER_TFS  # noqa: PLC0415
            mon = HarmonicMonitor(candidates, _DISCOVER_TFS, 200, 3, 0.05, len(candidates), store=store)
            rows = await mon.refresh(now)
            found = sorted({str(r["coin"]) for r in rows})
            if rows:
                store.insert_harmonic_setups(mon.to_records(rows, batch_ts))   # 并入当前批次立即展示
            if found:
                # 写监控清单 monitored_coins（统一真相源；监控进程周期对账热载入持续监控）
                store.add_monitored_coins([(c, candidates[c], now, "discover") for c in found])
            return aiohttp.web.json_response(
                {"discovered": found, "scanned": len(candidates)},
                dumps=lambda o: json.dumps(o, default=str),
            )
        except Exception as exc:  # noqa: BLE001
            return aiohttp.web.json_response({"discovered": [], "error": str(exc)}, status=500)

    app = aiohttp.web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_get("/health", handle_health)
    # 谐波主-详情 SPA（**新版替代旧 /harmonic**，用户#：/harmonic2 替代/合并旧版）
    # /harmonic 与 /harmonic2 均指向新主-详情页；旧 handle_harmonic/api 仅保留 /api/harmonic 兼容
    app.router.add_get("/harmonic", handle_harmonic2)
    app.router.add_get("/harmonic2", handle_harmonic2)
    app.router.add_get("/api/harmonic", handle_api_harmonic)
    app.router.add_get("/api/harmonic/list", handle_harmonic_list)
    app.router.add_get("/api/harmonic/coin/{coin}", handle_harmonic_coin)
    app.router.add_get("/api/harmonic/discover", handle_harmonic_discover)
    app.router.add_post("/api/harmonic/discover", handle_harmonic_discover)
    # 面板路由外置扁平模块注册（dashboard.py 巨文件零增长，逐个稀释）：
    from .dashboard_monitored import register as _register_monitored  # noqa: PLC0415
    from .dashboard_vol import register as _register_vol  # noqa: PLC0415
    _register_monitored(app, store)   # /monitored + /api/monitored（增删查）
    _register_vol(app, store)         # /volatility + /api/volatility（逐周期波动）
    # HL 聪明钱追踪终端（新增，不动现有 / 路由）
    app.router.add_get("/hl2", handle_hl2)
    # 信号总览页 + 导航页（迁出扁平模块，零增长）
    from .dashboard_signals import register as _register_signals  # noqa: PLC0415
    from .dashboard_nav import register as _register_nav          # noqa: PLC0415
    _register_signals(app, store)   # /signals + /api/signals
    _register_nav(app)              # /nav 导航

    print(f"仪表盘: http://{host}:{port}")
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, host, port)
    await site.start()
    # 保持运行直到外部取消
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        store.close()


# ---------------------------------------------------------------------------
# 谐波形态独立页 —— build_harmonic_state / render_harmonic_html / 路由
# ---------------------------------------------------------------------------

# 谐波 setups 列序（29 列，与表契约对齐）
_HARMONIC_KEYS = [
    "ts", "coin", "tf", "kind", "pattern", "direction", "price",
    "entry_lo", "entry_hi", "stop", "target1", "target2", "rr",
    "confidence", "knn", "orderflow", "fib_note", "prz_lo", "prz_hi",
    # XABCD 点坐标（v2 新增，forming 行为 None）
    "x_idx", "x_px", "a_idx", "a_px", "b_idx", "b_px",
    "c_idx", "c_px", "d_idx", "d_px",
]


def build_harmonic_state(store: Any, now_ms: int) -> dict:
    """从 store.conn 查询 harmonic_setups，分 completed/forming 两组返回 dict。

    每行含 asset_class 字段（'tradfi'/'crypto'），用于前端渲染徽章。
    表不存在/为空时各组返回 []，不抛异常（防御性查询）。
    """
    from .asset_class import asset_class as _asset_class  # 延迟导入，避免循环

    gen_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ms / 1000))

    rows = _safe_rows(
        store.conn,
        "SELECT ts,coin,tf,kind,pattern,direction,price,"
        "entry_lo,entry_hi,stop,target1,target2,rr,"
        "confidence,knn,orderflow,fib_note,prz_lo,prz_hi,"
        "x_idx,x_px,a_idx,a_px,b_idx,b_px,c_idx,c_px,d_idx,d_px "
        "FROM harmonic_setups ORDER BY confidence DESC",
    )

    completed: list[dict] = []
    forming: list[dict] = []
    for r in rows:
        d = _row_to_dict(r, _HARMONIC_KEYS)
        # 注入资产类别（TradFi/加密），供前端显示徽章
        d["asset_class"] = _asset_class(d.get("coin") or "")
        if d.get("kind") == "completed":
            completed.append(d)
        else:
            forming.append(d)

    return {
        "completed": completed,
        "forming": forming,
        "generated_at": gen_str,
    }


def build_harmonic_list(store: Any) -> list[dict]:
    """聚合 recent_harmonic_setups → 每币一条汇总行，按 best_conf 降序。

    返回字段：coin, asset_class, best_conf, direction, n_setups, has_completed, ts。
    ts=该币最新 setup 计算时刻（供前端显示真实"数据时间/数据年龄"，而非浏览器时钟）。
    表缺/空时返回 []，不抛。
    """
    from .asset_class import asset_class as _asset_class

    try:
        rows = store.recent_harmonic_setups()
    except Exception:  # noqa: BLE001
        return []

    # 按 coin 聚合
    agg: dict[str, dict] = {}
    for r in rows:
        d = _row_to_dict(r, _HARMONIC_KEYS)
        coin = d.get("coin") or ""
        if coin not in agg:
            agg[coin] = {
                "coin": coin,
                "asset_class": _asset_class(coin),
                "best_conf": None,
                "direction": None,
                "n_setups": 0,
                "has_completed": False,
                "ts": None,
            }
        entry = agg[coin]
        entry["n_setups"] += 1
        conf = d.get("confidence")
        if conf is not None:
            if entry["best_conf"] is None or conf > entry["best_conf"]:
                entry["best_conf"] = conf
                entry["direction"] = d.get("direction")
        if d.get("kind") == "completed":
            entry["has_completed"] = True
        # 跟踪该币最新 setup ts（数据新鲜度）
        ts = d.get("ts")
        if ts is not None and (entry["ts"] is None or ts > entry["ts"]):
            entry["ts"] = ts

    # 按 best_conf 降序（None 排最后）
    result = list(agg.values())
    result.sort(key=lambda x: (x["best_conf"] is None, -(x["best_conf"] or 0)))
    return result


def _knn_note_from_flag(knn_flag: str | None) -> str:
    """把 DB knn 列（'✓'/'✗'/'?'/None）映射为友好说明文字。

    注：KNN 命中率实测 ≈50%（随机基线），诚实标注，不伪造概率。
    """
    if knn_flag == "✓":
        return "找到历史相似态（注：KNN≈随机基线，仅辅助参考）"
    if knn_flag == "✗":
        return "历史无相似态（注：KNN≈随机基线，仅辅助参考）"
    return "样本不足或未计算（KNN≈随机基线，不可单独依赖）"


def _prz_proximity(price: float | None, prz_lo: float | None, prz_hi: float | None,
                   is_completed: bool = False) -> str:
    """描述当前价格相对 PRZ 区间的位置（前瞻信号强度指示）。

    返回中文描述字符串，用于"前瞻接近度"展示。价格/PRZ 缺失时返回 '—'。
    util.to_float(None) 返回 0.0 而非 None，故先检查原始值是否为 None。

    is_completed=True（D点已发生的 completed 形态）用回顾语义，不说"前瞻等待"——
    completed 的 D 点已反应过 PRZ，当前价格只是反应后的位置，说"前瞻等待"语义矛盾。
    forming（默认）保持前瞻语义（D 未到，等价格逼近 PRZ 是真前瞻提前量）。
    """
    from smc_tracker.util import to_float as _to_float
    if price is None or prz_lo is None or prz_hi is None:
        return "—"
    p = _to_float(price)
    lo = _to_float(prz_lo)
    hi = _to_float(prz_hi)
    if p is None or lo is None or hi is None:
        return "—"
    if lo > hi:
        lo, hi = hi, lo
    span = hi - lo
    if span <= 0:
        return "—"
    mid = (lo + hi) / 2
    if mid <= 0 or p <= 0:
        return "—"
    dist_pct = abs(p - mid) / mid * 100
    if lo <= p <= hi:
        zone = "D点反应区" if is_completed else "⚡ 距中轴"
        return f"价格在 PRZ 内（{zone} {dist_pct:.1f}%）"
    elif p < lo:
        gap_pct = (lo - p) / p * 100
        tail = "D点已反应，现价回落" if is_completed else "尚未触及，前瞻等待"
        return f"价格低于 PRZ {gap_pct:.1f}%（{tail}）"
    else:
        gap_pct = (p - hi) / p * 100
        tail = "D点已反应，现价上行" if is_completed else "已突破 PRZ 上沿"
        return f"价格高于 PRZ {gap_pct:.1f}%（{tail}）"


def _compute_confluence(all_setups: list[dict]) -> list[dict]:
    """检测跨 TF PRZ 区间重叠（多周期共振）——前瞻强化信号。

    算法：枚举所有 TF pair，两个 setup 的 [prz_lo, prz_hi] 有非空交集
    且方向一致 → 共振。返回共振列表，每项含：
      tf_a, tf_b, direction, overlap_lo, overlap_hi, kind_a, kind_b

    业界 multi-TF confluence 标准：多周期在同价区均有反转意愿 = 更高确定性。
    共振 forming 优于共振 completed（forming 是前瞻信号）。
    """
    from smc_tracker.util import to_float as _to_float
    results: list[dict] = []
    seen: set[tuple] = set()
    for i, a in enumerate(all_setups):
        tf_a = a.get("tf") or ""
        dir_a = a.get("direction") or ""
        raw_lo_a = a.get("prz_lo")
        raw_hi_a = a.get("prz_hi")
        # util.to_float(None)=0.0 不是 None，须先检查原始值
        if raw_lo_a is None or raw_hi_a is None or not dir_a:
            continue
        lo_a = _to_float(raw_lo_a)
        hi_a = _to_float(raw_hi_a)
        if lo_a is None or hi_a is None:
            continue
        if lo_a > hi_a:
            lo_a, hi_a = hi_a, lo_a
        for b in all_setups[i + 1:]:
            tf_b = b.get("tf") or ""
            if tf_b == tf_a:
                continue
            dir_b = b.get("direction") or ""
            if dir_b != dir_a:
                continue
            raw_lo_b = b.get("prz_lo")
            raw_hi_b = b.get("prz_hi")
            if raw_lo_b is None or raw_hi_b is None:
                continue
            lo_b = _to_float(raw_lo_b)
            hi_b = _to_float(raw_hi_b)
            if lo_b is None or hi_b is None:
                continue
            if lo_b > hi_b:
                lo_b, hi_b = hi_b, lo_b
            overlap_lo = max(lo_a, lo_b)
            overlap_hi = min(hi_a, hi_b)
            if overlap_lo > overlap_hi:
                continue
            key = tuple(sorted([tf_a, tf_b]) + [dir_a])
            if key in seen:
                continue
            seen.add(key)
            kind_a = a.get("kind") or "—"
            kind_b = b.get("kind") or "—"
            fwd_count = sum(1 for k in (kind_a, kind_b) if k == "forming")
            results.append({
                "tf_a": tf_a,
                "tf_b": tf_b,
                "direction": dir_a,
                "overlap_lo": round(overlap_lo, 6),
                "overlap_hi": round(overlap_hi, 6),
                "kind_a": kind_a,
                "kind_b": kind_b,
                "fwd_count": fwd_count,
            })
    results.sort(key=lambda x: x["fwd_count"], reverse=True)
    return results


def _enrich_setup(d: dict, current_price: float | None) -> dict:
    """补充 setup dict 的派生展示字段（纯函数，不改原始字段）。

    新增字段：
      knn_note   — 由 knn 旗标派生的友好说明
      honest_label — completed=回顾型/forming=前瞻预警
      prz_proximity — 当前价格 vs PRZ 位置描述（前瞻接近度）
    """
    d = dict(d)
    d["knn_note"] = _knn_note_from_flag(d.get("knn"))
    kind = d.get("kind") or ""
    if kind == "completed":
        d["honest_label"] = "completed（回顾型：D点已发生，反应式信号）"
    elif kind == "forming":
        d["honest_label"] = "forming（前瞻预警：XABCD 成形中，D点尚未到达）"
    else:
        d["honest_label"] = "—"
    price = current_price if current_price is not None else d.get("price")
    d["prz_proximity"] = _prz_proximity(
        price, d.get("prz_lo"), d.get("prz_hi"), is_completed=(kind == "completed"))
    # 交易计划诚实标注：有 PRZ 但无 entry = build_setups 因止损距离(X点失效位)超合理阈值
    # 诚实跳过(不产劣质 setup，trade_setup.py §238)。网页据此显示原因而非困惑的空白 —。
    if d.get("entry_lo") is None and d.get("prz_lo") is not None:
        d["plan_note"] = "⚠️ 止损距离超合理阈值，未生成交易计划（诚实跳过劣质 setup）"
    else:
        d["plan_note"] = ""
    return d


def build_coin_detail(store: Any, coin: str, tf: str | None = None) -> dict:
    """组装指定 coin（和 tf）的详情数据：蜡烛/setup/S/R/历史/多周期共振。

    tf 缺省时取该币在 recent_harmonic_setups 中首个 setup 的 tf。
    tfs_available 固定返回 7 周期（15m/30m/1H/4H/12H/1D/1W），无论该周期是否有形态。
    无形态周期的 setups=[]，candles 仍尝试拉取（让前端显示 K 线）。
    表缺/空时各字段返回 []，不抛。

    新增字段：
      setups[].knn_note      — KNN 旗标友好说明（从 knn 列派生）
      setups[].honest_label  — 形态类型诚实标注（completed=回顾/forming=前瞻）
      setups[].prz_proximity — 当前价格相对 PRZ 位置（前瞻接近度描述）
      confluence             — 多周期 PRZ 共振列表（前瞻强化信号）
    """
    from .asset_class import asset_class as _asset_class

    # 固定 7 周期 tab（统一 CANONICAL_TIMEFRAMES，前端始终显示完整周期导航）
    from .config import CANONICAL_TIMEFRAMES as _FIXED_TFS  # noqa: PLC0415

    # 1. 读该币全部最新 setup 行（所有 tf）
    all_setups: list[dict] = []
    first_setup_tf: str = ""
    try:
        for r in store.recent_harmonic_setups():
            d = _row_to_dict(r, _HARMONIC_KEYS)
            if d.get("coin") != coin:
                continue
            d["asset_class"] = _asset_class(coin)
            all_setups.append(d)
            if not first_setup_tf:
                first_setup_tf = d.get("tf") or ""
    except Exception:  # noqa: BLE001
        pass

    # tf 缺省 → 用该币第一个 setup 的 tf；若无 setup，取固定列表第一个
    resolved_tf: str = tf or first_setup_tf or _FIXED_TFS[0]

    # 只保留目标 tf 的 setup
    setups_raw = [d for d in all_setups if d.get("tf") == resolved_tf]

    # 2. 蜡烛（200 根）——无形态的周期也拉（K 线仍有意义）
    candles: list[list] = []
    try:
        raw_candles = store.get_candles(coin, resolved_tf, 200)
        candles = [
            [c.open_time_ms, c.o, c.h, c.l, c.c, c.v]
            for c in raw_candles
        ]
    except Exception:  # noqa: BLE001
        pass

    # 当前价格（最新蜡烛收盘，供 prz_proximity 计算）
    current_price: float | None = None
    if candles:
        try:
            current_price = float(candles[-1][4])
        except (IndexError, TypeError, ValueError):
            pass

    # setup 字段丰富化（补 knn_note / honest_label / prz_proximity）
    setups = [_enrich_setup(d, current_price) for d in setups_raw]

    # 3. S/R（该币所有 tf 的最新 bb_levels）
    sr: list[dict] = []
    try:
        for r in store.recent_bb_levels(coin):
            sr.append({
                "tf":      r[1],
                "upper":   r[3],
                "lower":   r[5],
                "pct_b":   r[6],
                "squeeze": r[7],
            })
    except Exception:  # noqa: BLE001
        pass

    # 4. 历史形态
    history: list[dict] = []
    try:
        for r in store.harmonic_history(coin, 30):
            d = _row_to_dict(r, _HARMONIC_KEYS)
            d["asset_class"] = _asset_class(coin)
            history.append(d)
    except Exception:  # noqa: BLE001
        pass

    # 5. 多周期 PRZ 共振（跨所有 tf setups，前瞻强化）
    confluence: list[dict] = _compute_confluence(all_setups)

    return {
        "coin": coin,
        "asset_class": _asset_class(coin),
        "tf": resolved_tf,
        # 固定 7 周期 tab，不受「是否有形态」影响（前端按此列表渲染完整导航）
        "tfs_available": _FIXED_TFS,
        "candles": candles,
        "setups": setups,
        "sr": sr,
        "history": history,
        "confluence": confluence,
    }


# ---------------------------------------------------------------------------
# HL 聪明钱地址追踪页（/hl2）—— 浅色金融终端，三栏布局，零伪造数据
# ---------------------------------------------------------------------------

_HL_TEMPLATE = (_TPL_DIR / "hl2.html").read_text(encoding="utf-8")


def render_hl_html(state: dict) -> str:
    """将 build_dashboard_state 结果渲染成 HL 聪明钱追踪终端 HTML（/hl2 路由）。

    双括号转义模式与 render_harmonic_detail_html 完全一致：
    先解转义 {{→{ / }}→}，再注入 __INITIAL_STATE__ JSON。
    """
    state_json = json.dumps(state, ensure_ascii=False, default=str)
    html = _HL_TEMPLATE.replace("{{", "{").replace("}}", "}")
    return html.replace("__INITIAL_STATE__", state_json)


# 兼容直接 import asyncio
import asyncio  # noqa: E402
