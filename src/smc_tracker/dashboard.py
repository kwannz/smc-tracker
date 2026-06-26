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
from .dashboard_common import _safe_rows, _row_to_dict
from .dashboard_harmonic import render_harmonic_html, render_harmonic_detail_html  # noqa: F401
from .dashboard_harmonic import (  # noqa: F401
    build_harmonic_state, build_harmonic_list, build_coin_detail, _HARMONIC_KEYS,
    _knn_note_from_flag, _prz_proximity, _compute_confluence, _enrich_setup,
)

# 模板目录：HTML 模板已外置，模块导入时一次性读入并缓存
_TPL_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# 数据层 —— 每个查询独立 try/except，表不存在或为空时返回空列表（参考 report.py _count 写法）
# ---------------------------------------------------------------------------

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

    # 注：原 ticker_board(行情监控板)计算已删除——前端无消费者 + 每 symbol 一次 chg24 子查询(N+1)，
    # 属死计算(修审计 P2)。行情维度由 /volatility 等专用板覆盖。

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
