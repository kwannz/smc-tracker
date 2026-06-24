"""SMC 抓庄监控仪表盘 —— 从 SQLite 实时渲染深色主题 HTML，用 aiohttp 起服务。

接口：
  build_dashboard_state(store, now_ms, window_ms) → dict  （组装各 section 数据）
  render_html(state)                               → str  （返回自包含单页 HTML）
  serve(db_path, host, port)                       → None （aiohttp Web 服务）
"""
from __future__ import annotations

import json
import time
from typing import Any

import aiohttp.web


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

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SMC 抓庄监控</title>
<style>
:root{{
  --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;
  --muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;
  --yellow:#e3b341;--purple:#bc8cff;--orange:#ffa657;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:"SF Mono",ui-monospace,monospace;font-size:13px;line-height:1.5}}
header{{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;align-items:baseline;gap:16px}}
h1{{font-size:20px;color:var(--blue)}}
#meta{{color:var(--muted);font-size:12px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:16px;padding:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
.card-title{{padding:10px 14px;border-bottom:1px solid var(--border);font-weight:700;color:var(--blue);font-size:13px}}
.card-body{{padding:12px 14px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{color:var(--muted);font-weight:600;text-align:left;padding:4px 6px;border-bottom:1px solid var(--border)}}
td{{padding:3px 6px;vertical-align:top;white-space:nowrap}}
tr:hover td{{background:rgba(255,255,255,.03)}}
.long{{color:var(--green)}} .short{{color:var(--red)}}
.bullish{{color:var(--green)}} .bearish{{color:var(--red)}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}}
.none{{color:var(--muted);font-style:italic}}
.tag{{display:inline-block;padding:1px 5px;border-radius:4px;font-size:11px;font-weight:600}}
.tag-long{{background:#1a3a2a;color:var(--green)}}
.tag-short{{background:#3a1a1a;color:var(--red)}}
.addr{{font-family:monospace;font-size:11px;color:var(--purple)}}
.coin{{color:var(--orange);font-weight:600}}
.score{{color:var(--yellow)}}
#refresh-bar{{font-size:11px;color:var(--muted);padding:4px 24px;border-top:1px solid var(--border)}}
</style>
</head>
<body>
<header>
  <h1>🐋 SMC 抓庄监控</h1>
  <span id="meta">加载中…</span>
</header>
<main id="main"><!-- 由 JS renderAll() 填充 --></main>
<div id="refresh-bar">自动刷新 · 5 秒</div>
<script>
const S = __INITIAL_STATE__;

// ---------- 工具函数 ----------
function fmtTime(ms){{
  if(!ms)return'--';
  const d=new Date(ms);
  return d.toLocaleTimeString('zh-CN',{{hour12:false}});
}}
function fmtUsd(v){{
  if(v==null)return'--';
  const n=parseFloat(v);
  if(isNaN(n))return'--';
  const abs=Math.abs(n);
  let s;
  if(abs>=1e9)s=(n/1e9).toFixed(2)+'B';
  else if(abs>=1e6)s=(n/1e6).toFixed(2)+'M';
  else if(abs>=1e3)s=(n/1e3).toFixed(1)+'K';
  else s=n.toFixed(2);
  return(n>=0?'$':'−$')+s.replace('-','');
}}
function fmtNum(v,dec=2){{
  if(v==null)return'--';
  const n=parseFloat(v);
  return isNaN(n)?'--':n.toFixed(dec);
}}
function fmtPct(v){{
  if(v==null)return'--';
  return(parseFloat(v)*100).toFixed(3)+'%';
}}
function shortAddr(a){{
  if(!a)return'--';
  if(a.startsWith('0x')&&a.length>10)return a.slice(0,6)+'…'+a.slice(-4);
  if(a.length>12)return a.slice(0,6)+'…'+a.slice(-4);
  return a;
}}
function dirTag(d){{
  if(!d)return'';
  const cls=d==='long'?'tag-long':'tag-short';
  const lbl=d==='long'?'做多':'做空';
  return`<span class="tag ${{cls}}">${{lbl}}</span>`;
}}
function none(){{ return'<span class="none">（无）</span>'; }}

// ---------- 纯 inline SVG 图表（无 CDN/无依赖）----------
// XML 转义：防止标签文本里的 < > & 破坏 SVG 结构
function svgEsc(s){{
  return String(s==null?'':s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
// 发散横向条形图：每项一行，标签在左，横条从中线向右(正/绿)或向左(负/红)，数值在右
//   items: 数据数组；getLabel(item)->标签字符串；getVal(item)->数值
//   opts: {{fmt: 数值->显示文本; width: viewBox 宽; labelW: 标签列宽; valW: 数值列宽}}
//   无数据返回空串。
function svgBars(items, getLabel, getVal, opts){{
  if(!items||!items.length)return'';
  opts=opts||{{}};
  const width=opts.width||460;
  const labelW=opts.labelW||96;
  const valW=opts.valW||78;
  const fmt=opts.fmt||(v=>fmtUsd(v));
  const rowH=22, padT=6, padB=6;
  // 归一化：取所有 |值| 的最大值（>0 防除零）
  let maxAbs=0;
  items.forEach(it=>{{ const v=Math.abs(parseFloat(getVal(it))||0); if(v>maxAbs)maxAbs=v; }});
  if(maxAbs<=0)maxAbs=1;
  // 绘图区：中线两侧各占一半（barArea 为单侧最大像素长度）
  const x0=labelW;                       // 条形区左起点
  const x1=width-valW;                   // 条形区右终点
  const mid=(x0+x1)/2;                   // 中线（0 值）
  const half=(x1-x0)/2-2;                // 单侧最大长度
  const h=items.length*rowH+padT+padB;
  let s=`<svg viewBox="0 0 ${{width}} ${{h}}" width="100%" height="${{h}}" `
       +`xmlns="http://www.w3.org/2000/svg" style="display:block">`;
  // 中线（0 轴）
  s+=`<line x1="${{mid}}" y1="${{padT}}" x2="${{mid}}" y2="${{h-padB}}" `
    +`stroke="#30363d" stroke-width="1"/>`;
  items.forEach((it,i)=>{{
    const v=parseFloat(getVal(it))||0;
    const y=padT+i*rowH;
    const cy=y+rowH/2;
    const len=Math.abs(v)/maxAbs*half;
    const color=v>=0?'#3fb950':'#f85149';   // 正绿/负红（与 --pos/--red 一致）
    // 条形：正值从中线向右，负值从中线向左
    const bx=v>=0?mid:(mid-len);
    s+=`<rect x="${{bx}}" y="${{y+4}}" width="${{Math.max(len,0.5)}}" height="${{rowH-8}}" `
      +`fill="${{color}}" rx="2"/>`;
    // 左侧标签
    s+=`<text x="4" y="${{cy+4}}" fill="#8b949e" font-size="11">`
      +`${{svgEsc(getLabel(it))}}</text>`;
    // 右侧数值（按符号着色）
    s+=`<text x="${{width-4}}" y="${{cy+4}}" fill="${{color}}" font-size="11" `
      +`text-anchor="end">${{svgEsc(fmt(v))}}</text>`;
  }});
  s+=`</svg>`;
  return s;
}}
// 折线 sparkline：points=数值数组，返回 inline SVG <polyline>；无/单点安全返回空串
function svgSpark(points, opts){{
  if(!points||points.length<2)return'';
  opts=opts||{{}};
  const width=opts.width||160;
  const height=opts.height||32;
  const pad=2;
  let lo=Infinity, hi=-Infinity;
  points.forEach(p=>{{ const v=parseFloat(p); if(!isNaN(v)){{ if(v<lo)lo=v; if(v>hi)hi=v; }} }});
  if(!isFinite(lo)||!isFinite(hi))return'';
  const span=(hi-lo)||1;                 // 防除零（全平时 span=1）
  const n=points.length;
  const dx=(width-2*pad)/(n-1);
  const color=opts.color||'#58a6ff';
  let pts='';
  points.forEach((p,i)=>{{
    const v=parseFloat(p)||0;
    const x=pad+i*dx;
    // y 翻转（SVG 原点左上：高值在上）
    const y=height-pad-((v-lo)/span)*(height-2*pad);
    pts+=`${{x.toFixed(1)}},${{y.toFixed(1)}} `;
  }});
  return`<svg viewBox="0 0 ${{width}} ${{height}}" width="${{width}}" height="${{height}}" `
       +`xmlns="http://www.w3.org/2000/svg" style="display:block">`
       +`<polyline points="${{pts.trim()}}" fill="none" stroke="${{color}}" `
       +`stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}}

// ---------- 各 section 渲染 ----------
function renderSignals(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr><th>时间</th><th>标的</th><th>方向</th><th>评分</th><th>入场</th><th>止损</th><th>目标</th><th>RR</th></tr>';
  rows.forEach(r=>{{
    h+=`<tr>
      <td>${{fmtTime(r.ts)}}</td>
      <td class="coin">${{r.coin||''}}</td>
      <td>${{dirTag(r.direction)}}</td>
      <td class="score">${{fmtNum(r.score,2)}}</td>
      <td>${{r.entry?fmtNum(r.entry,4):'--'}}</td>
      <td>${{r.stop?fmtNum(r.stop,4):'--'}}</td>
      <td>${{r.target?fmtNum(r.target,4):'--'}}</td>
      <td>${{r.rr?fmtNum(r.rr,2):'--'}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

function renderDivergence(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr><th>时间</th><th>标的</th><th>偏向</th><th>评分</th><th>资金费</th><th>DEX净流</th></tr>';
  rows.forEach(r=>{{
    const cls=r.direction==='bullish'?'bullish':'bearish';
    const lbl=r.direction==='bullish'?'吸筹↑':'分销↓';
    h+=`<tr>
      <td>${{fmtTime(r.ts)}}</td>
      <td class="coin">${{r.coin||''}}</td>
      <td class="${{cls}}">${{lbl}}</td>
      <td class="score">${{fmtNum(r.score,2)}}</td>
      <td>${{fmtPct(r.funding)}}</td>
      <td>${{fmtUsd(r.dex_flow_usd)}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

function renderWhaleFlows(rows){{
  if(!rows||!rows.length)return none();
  // 图在上：净流向发散条形图（净买绿向右 / 净卖红向左），数值用美元格式
  const chart=svgBars(
    rows,
    r=>r.coin||'',
    r=>parseFloat(r.net)||0,
    {{fmt:v=>(v>=0?'净买 ':'净卖 ')+fmtUsd(Math.abs(v))}}
  );
  // 表在下：原始明细表
  let h='<table><tr><th>标的</th><th>净主动流向</th><th>方向</th></tr>';
  rows.forEach(r=>{{
    const n=parseFloat(r.net)||0;
    const cls=n>=0?'pos':'neg';
    const arrow=n>=0?'净买↑':'净卖↓';
    h+=`<tr>
      <td class="coin">${{r.coin||''}}</td>
      <td class="${{cls}}">${{fmtUsd(Math.abs(n))}}</td>
      <td class="${{cls}}">${{arrow}}</td>
    </tr>`;
  }});
  return chart+h+'</table>';
}}

function renderTopAddresses(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr><th>地址</th><th>评分</th><th>净值</th><th>月PnL</th><th>偏向</th><th>偏好</th></tr>';
  rows.forEach(r=>{{
    h+=`<tr>
      <td class="addr" title="${{r.address||''}}">${{shortAddr(r.address)}}</td>
      <td class="score">${{fmtNum(r.score,1)}}</td>
      <td>${{fmtUsd(r.account_value)}}</td>
      <td class="${{parseFloat(r.month_pnl)>=0?'pos':'neg'}}">${{fmtUsd(r.month_pnl)}}</td>
      <td>${{r.net_bias||'--'}}</td>
      <td style="color:var(--muted);max-width:120px;overflow:hidden;text-overflow:ellipsis">${{r.fav_coins||'--'}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

function renderClusters(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr><th>#</th><th>成员数</th><th>协同次数</th><th>跨币数</th><th>涉及币种</th><th>成员</th></tr>';
  rows.forEach((r,i)=>{{
    const coins=(r.coin_list||[]).slice(0,5).join(' ');
    const members=(r.members||[]).map(shortAddr).join(' ');
    h+=`<tr>
      <td>${{i+1}}</td>
      <td>${{r.size||0}}</td>
      <td class="score">${{r.events||0}}</td>
      <td class="${{(r.coins||0)>=2?'pos':'muted'}}">${{r.coins||0}}</td>
      <td class="coin">${{coins||'--'}}</td>
      <td class="addr" style="white-space:normal">${{members}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

function renderOiSurges(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr><th>合约</th><th>OI(张)</th><th>资金费</th><th>更新时间</th></tr>';
  rows.forEach(r=>{{
    h+=`<tr>
      <td class="coin">${{r.symbol||''}}</td>
      <td>${{fmtNum(r.oi_size,0)}}</td>
      <td>${{fmtPct(r.funding)}}</td>
      <td>${{fmtTime(r.ts)}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

function renderOnchain(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr><th>时间</th><th>标的</th><th>链</th><th>数量(USD)</th><th>TxHash</th></tr>';
  rows.forEach(r=>{{
    const th=r.tx_hash||'';
    h+=`<tr>
      <td>${{fmtTime(r.ts)}}</td>
      <td class="coin">${{r.coin||''}}</td>
      <td>${{r.chain||''}}</td>
      <td>${{fmtUsd(r.amount_usd)}}</td>
      <td class="addr" title="${{th}}">${{shortAddr(th)}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

function renderWhaleSignals(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr><th>时间</th><th>标签</th><th>标的</th><th>方向</th><th>名义(USD)</th><th>价格</th></tr>';
  rows.forEach(r=>{{
    h+=`<tr>
      <td>${{fmtTime(r.ts)}}</td>
      <td class="addr">${{r.label||'--'}}</td>
      <td class="coin">${{r.coin||''}}</td>
      <td>${{dirTag(r.direction)}}</td>
      <td>${{fmtUsd(r.notional)}}</td>
      <td>${{fmtNum(r.px,4)}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

function fmtOpenTime(open_ms){{
  // 将 open_ms(ms 时间戳) 格式化为本地时间 MM-DD HH:MM
  if(!open_ms)return'—';
  const d=new Date(open_ms);
  const mo=String(d.getMonth()+1).padStart(2,'0');
  const dd=String(d.getDate()).padStart(2,'0');
  const hh=String(d.getHours()).padStart(2,'0');
  const mm=String(d.getMinutes()).padStart(2,'0');
  return`${{mo}}-${{dd}} ${{hh}}:${{mm}}`;
}}
function fmtHoldSec(hold_sec){{
  // 将 hold_sec(秒) 格式化为紧凑时长
  if(hold_sec==null||hold_sec<=0)return'—';
  const s=Math.floor(hold_sec);
  if(s<60)return s+'s';
  const m=Math.floor(s/60);
  if(m<60)return m+'m';
  const h=Math.floor(m/60);
  const mr=m%60;
  if(h<24)return h+'h'+(mr?mr+'m':'');
  const d=Math.floor(h/24);
  const hr=h%24;
  return d+'d'+(hr?hr+'h':'');
}}
function renderWalletPortfolio(rows){{
  if(!rows||!rows.length)return none();
  let html='';
  rows.forEach(r=>{{
    const shortA=shortAddr(r.address);
    const lbl=r.label||shortA;
    html+=`<div style="margin-bottom:14px;border-bottom:1px solid var(--border);padding-bottom:10px">`;
    html+=`<div style="font-weight:700;color:var(--blue);margin-bottom:4px">`;
    html+=`🏦 ${{lbl}} <span class="addr" title="${{r.address||''}}">${{shortA}}</span>`;
    html+=` 净值${{fmtUsd(r.account_value)}} 总名义${{fmtUsd(r.total_ntl_pos)}} 持仓${{r.n_positions||0}}个`;
    html+=`</div>`;
    if(r.positions&&r.positions.length){{
      html+='<table><tr><th>币种</th><th>方向</th><th>名义</th><th>入场</th><th>uPnL</th><th>杠杆</th><th>爆仓</th><th>开仓时间</th><th>持仓时长</th></tr>';
      r.positions.forEach(p=>{{
        const dc=p.direction==='long'?'long':'short';
        const dlbl=p.direction==='long'?'多🟢':'空🔴';
        const upnl=parseFloat(p.unrealized_pnl||0);
        html+=`<tr>
          <td class="coin">${{p.coin||''}}</td>
          <td class="${{dc}}">${{dlbl}}</td>
          <td>${{fmtUsd(p.position_value)}}</td>
          <td>${{p.entry_px!=null?fmtNum(p.entry_px,4):'--'}}</td>
          <td class="${{upnl>=0?'pos':'neg'}}">${{fmtUsd(p.unrealized_pnl)}}</td>
          <td>${{p.leverage!=null?fmtNum(p.leverage,0)+'x':'--'}}</td>
          <td style="color:var(--muted)">${{p.liquidation_px!=null?fmtNum(p.liquidation_px,4):'—'}}</td>
          <td style="color:var(--muted);font-size:11px">${{fmtOpenTime(p.open_ms)}}</td>
          <td style="color:var(--yellow)">${{fmtHoldSec(p.hold_sec)}}</td>
        </tr>`;
      }});
      html+='</table>';
    }}else{{html+='<span class="none">暂无持仓</span>';}}
    html+='</div>';
  }});
  return html;
}}

function renderExchangeFlows(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr><th>交易所</th><th>链</th><th>净流(BTC)</th><th>流入</th><th>流出</th><th>笔数</th><th>更新时间</th></tr>';
  rows.forEach(r=>{{
    const net=parseFloat(r.net)||0;
    // 净流入(正)=资金流向交易所/潜在抛压🔴；净流出(负)=资金离开交易所/潜在吸筹🟢
    const netSymbol=net>=0?'🔴':'🟢';
    const netCls=net>=0?'neg':'pos';
    const netStr=netSymbol+' '+(net>=0?'+':'')+fmtNum(net,2);
    h+=`<tr>
      <td class="coin">${{r.exchange||''}}</td>
      <td>${{r.chain||'BTC'}}</td>
      <td class="${{netCls}}">${{netStr}}</td>
      <td>${{fmtNum(r.inflow,2)}}</td>
      <td>${{fmtNum(r.outflow,2)}}</td>
      <td>${{r.n_tx||0}}</td>
      <td style="color:var(--muted);font-size:11px">${{r.dt||'--'}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

function renderOkxLiquidations(rows){{
  if(!rows||!rows.length)return none();
  // 先按 coin 聚合强平名义总额 → 条形图（强平规模 by coin，全部为正→统一红色告警语义）
  const agg={{}};
  rows.forEach(r=>{{
    const c=r.coin||'?';
    agg[c]=(agg[c]||0)+(parseFloat(r.notional)||0);
  }});
  const items=Object.keys(agg)
    .map(c=>({{coin:c, total:agg[c]}}))
    .sort((a,b)=>b.total-a.total)
    .slice(0,12);
  const chart=svgBars(
    items,
    it=>it.coin,
    it=>parseFloat(it.total)||0,
    {{fmt:v=>'💥 '+fmtUsd(Math.abs(v))}}
  );
  // 表在下：时间/coin/被平方向/名义（pos_side='long'=多头被平→抛压级联；'short'=空头被平→逼空）
  let h='<div style="color:var(--muted);font-size:11px;margin-bottom:6px">'
       +'诚实标注：强平=已发生告警（多头被平🔴抛压级联 / 空头被平🟢逼空）</div>';
  h+='<table><tr><th>时间</th><th>标的</th><th>被平方向</th><th>名义(USD)</th></tr>';
  rows.slice().reverse().forEach(r=>{{   // reverse：最新在前（底层按 ts ASC）
    const ps=r.pos_side;
    const psLbl=ps==='long'?'多头被平🔴':(ps==='short'?'空头被平🟢':(ps||'--'));
    const psCls=ps==='long'?'neg':(ps==='short'?'pos':'');
    h+=`<tr>
      <td>${{fmtTime(r.ts)}}</td>
      <td class="coin">${{r.coin||''}}</td>
      <td class="${{psCls}}">${{psLbl}}</td>
      <td>${{fmtUsd(r.notional)}}</td>
    </tr>`;
  }});
  return chart+h+'</table>';
}}

function renderOkxSignals(rows){{
  if(!rows||!rows.length)return none();
  // 表：coin/方向/类型/净流向（kind: accumulation=吸筹 / distribution=分销）
  let h='<table><tr><th>标的</th><th>方向</th><th>类型</th><th>净流向(USD)</th></tr>';
  rows.slice().reverse().forEach(r=>{{   // reverse：最新在前（底层按 ts ASC）
    const k=r.kind;
    const kLbl=k==='accumulation'?'吸筹↑':(k==='distribution'?'分销↓':(k||'--'));
    const n=parseFloat(r.net_flow)||0;
    const nCls=n>=0?'pos':'neg';
    h+=`<tr>
      <td class="coin">${{r.coin||''}}</td>
      <td>${{dirTag(r.direction)}}</td>
      <td>${{kLbl}}</td>
      <td class="${{nCls}}">${{(n>=0?'净买 ':'净卖 ')+fmtUsd(Math.abs(n))}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

function renderHlWalls(rows){{
  if(!rows||!rows.length)return none();
  // 可选图：按 coin 聚合墙名义总额（spoof 风险高，仅作意图体量参考）
  const agg={{}};
  rows.forEach(r=>{{
    const c=r.coin||'?';
    agg[c]=(agg[c]||0)+(parseFloat(r.notional)||0);
  }});
  const items=Object.keys(agg)
    .map(c=>({{coin:c, total:agg[c]}}))
    .sort((a,b)=>b.total-a.total)
    .slice(0,12);
  const chart=svgBars(
    items,
    it=>it.coin,
    it=>parseFloat(it.total)||0,
    {{fmt:v=>'🧱 '+fmtUsd(Math.abs(v))}}
  );
  // 诚实标注：挂单墙=未成交意图（可能 spoof 诱多/诱空），非已实现
  let h='<div style="color:var(--muted);font-size:11px;margin-bottom:6px">'
       +'诚实标注：挂单墙=未成交意图（领先信号，但可能 spoof 诱单）</div>';
  h+='<table><tr><th>时间</th><th>标的</th><th>墙向</th><th>动作</th><th>价</th><th>名义(USD)</th></tr>';
  rows.slice().reverse().forEach(r=>{{   // reverse：最新在前（底层按 ts ASC）
    // side: 'bid'=买墙(支撑/吸筹意图) / 'ask'=卖墙(压制/分销意图)
    const sd=r.side;
    const sdLbl=sd==='bid'?'买墙🟢':(sd==='ask'?'卖墙🔴':(sd||'--'));
    const sdCls=sd==='bid'?'pos':(sd==='ask'?'neg':'');
    // kind: 'build'=墙出现 / 'pull'=抽单（撤墙，意图反转/诱单兑现）
    const k=r.kind;
    const kLbl=k==='build'?'出现':(k==='pull'?'抽单':(k||'--'));
    h+=`<tr>
      <td>${{fmtTime(r.ts)}}</td>
      <td class="coin">${{r.coin||''}}</td>
      <td class="${{sdCls}}">${{sdLbl}}</td>
      <td>${{kLbl}}</td>
      <td>${{r.px!=null?fmtNum(r.px,4):'--'}}</td>
      <td>${{fmtUsd(r.notional)}}</td>
    </tr>`;
  }});
  return chart+h+'</table>';
}}

function renderHealth(h){{
  if(!h||!h.freshness||!h.freshness.length)return none();
  const ok=h.ok?'<span class="pos">✅ 健康</span>':'<span class="neg">⚠️ 告警</span>';
  let html='<div style="margin-bottom:8px">总体：'+ok+'</div>';
  html+='<table><tr><th>表</th><th>行数</th><th>最新</th><th>状态</th></tr>';
  h.freshness.forEach(f=>{{
    let st,cls;
    if(!f.exists){{st='缺失';cls='neg';}}
    else if(f.age_s==null){{st='空表';cls='none';}}
    else if(f.stale){{st=(f.age_s/3600).toFixed(1)+'h前·陈旧';cls='neg';}}
    else{{st=(f.age_s/3600).toFixed(1)+'h前';cls='pos';}}
    html+=`<tr><td>${{f.table}}</td><td>${{f.n||0}}</td><td style="color:var(--muted);font-size:11px">${{f.latest_dt||'--'}}</td><td class="${{cls}}">${{st}}</td></tr>`;
  }});
  html+='</table>';
  const p=h.predictions||{{}};
  const ovCls=(p.overdue||0)>0?'neg':'pos';
  html+=`<div style="margin-top:8px">验证闭环：预测 ${{p.total||0}} · 已评 ${{p.evaluated||0}} · 待评 ${{p.pending||0}} · <span class="${{ovCls}}">到期未评 ${{p.overdue||0}}</span></div>`;
  return html;
}}

function renderAccuracy(a){{
  if(!a||!a.total_n)return '<span class="none">样本不足，继续积累（尚无已到期评估）</span>';
  const hr=(a.hit_rate*100).toFixed(1);
  const edge=a.edge*100;
  const edgeCls=edge>=0?'pos':'neg';
  const edgeStr=(edge>=0?'+':'')+edge.toFixed(1)+'pp';
  let html=`<div style="margin-bottom:6px">总体：样本 ${{a.total_n}} · 命中率 ${{hr}}% · 相对随机 <span class="${{edgeCls}}">${{edgeStr}}</span></div>`;
  if(!a.sufficient){{html+=`<div class="neg" style="margin-bottom:6px">⚠️ 样本不足(${{a.total_n}}<${{a.min_sample||20}})，结论仅供参考</div>`;}}
  const bk=a.by_kind||{{}};
  const keys=Object.keys(bk);
  if(keys.length){{
    html+='<table><tr><th>类型</th><th>命中</th><th>命中率</th><th>边际</th><th>均按向收益</th></tr>';
    keys.forEach(k=>{{
      const d=bk[k];
      const e=d.edge*100;
      const ecls=e>=0?'pos':'neg';
      const ar=d.avg_ret*100;
      html+=`<tr><td>${{k}}</td><td>${{d.hits}}/${{d.n}}</td><td>${{(d.hit_rate*100).toFixed(0)}}%</td><td class="${{ecls}}">${{(e>=0?'+':'')+e.toFixed(0)}}pp</td><td class="${{ar>=0?'pos':'neg'}}">${{(ar>=0?'+':'')+ar.toFixed(2)}}%</td></tr>`;
    }});
    html+='</table>';
  }}
  // MTF 多时间段命中率（alpha 诊断：哪个 TF 有真 alpha）
  const bh=a.by_horizon||{{}};
  const bhMn=a.by_horizon_market_neutral||{{}};
  const hzKeys=Object.keys(bh).map(Number).sort((x,y)=>x-y);
  function tfLabel(hzMs){{
    const min=Math.round(hzMs/60000);
    return min<60?min+'m':(min/60)+'h';
  }}
  if(hzKeys.length){{
    html+='<div style="margin-top:8px;font-weight:600">MTF alpha 诊断（各时间段命中率）：</div>';
    // 图在上：各 TF 命中率发散条形图（以 50% 随机基线为中心，>50% 绿/<50% 红）
    const hrItems=hzKeys.map(hz=>({{hz:hz, hr:bh[hz].hit_rate}}));
    const hrChart=svgBars(
      hrItems,
      it=>tfLabel(it.hz),
      it=>(parseFloat(it.hr)||0)-0.5,   // 相对 50% 随机基线的偏移（正=有边际）
      {{fmt:it=>((parseFloat(it)+0.5)*100).toFixed(0)+'%'}}
    );
    html+=hrChart;
    html+='<table><tr><th>TF</th><th>命中</th><th>命中率</th><th>边际</th><th>均按向收益</th><th>中性alpha边际</th><th>样本</th></tr>';
    hzKeys.forEach(hz=>{{
      const d=bh[hz];
      const mn=bhMn[hz]||{{}};
      const e=d.edge*100;
      const ecls=e>=0?'pos':'neg';
      const ar=d.avg_ret*100;
      const mnEdge=mn.edge!=null?(mn.edge*100):null;
      const mnStr=mnEdge!=null?`<span class="${{mnEdge>=0?'pos':'neg'}}">${{(mnEdge>=0?'+':'')+mnEdge.toFixed(0)}}pp</span>`:'--';
      const insuf=d.n<20?'<span class="neg">⚠️不足</span>':'';
      html+=`<tr><td>${{tfLabel(hz)}}</td><td>${{d.hits}}/${{d.n}}</td><td>${{(d.hit_rate*100).toFixed(0)}}%</td><td class="${{ecls}}">${{(e>=0?'+':'')+e.toFixed(0)}}pp</td><td class="${{ar>=0?'pos':'neg'}}">${{(ar>=0?'+':'')+ar.toFixed(2)}}%</td><td>${{mnStr}}</td><td>${{insuf||d.n}}</td></tr>`;
    }});
    html+='</table>';
  }}
  return html;
}}

// ---------- 主渲染入口 ----------
function renderAll(state){{
  const m=state.meta||{{}};
  document.getElementById('meta').textContent=
    `生成于 ${{m.generated||'--'}}  ·  近 ${{m.window_min||60}} 分钟`;

  const sections=[
    ['🩺 系统健康','health',renderHealth],
    ['📊 预测准确率(诚实回顾)','accuracy',renderAccuracy],
    ['🏦 交易所资金流(24h)','exchange_flows',renderExchangeFlows],
    ['🏦 钱包持仓画像','wallet_portfolio',renderWalletPortfolio],
    ['共振信号 ⚡','signals',renderSignals],
    ['背离信号 🔀','divergence',renderDivergence],
    ['聪明钱净流向 🐋','whale_flows',renderWhaleFlows],
    ['鲸鱼信号 🚨','whale_signals',renderWhaleSignals],
    ['聪明钱地址排行 🏆','top_addresses',renderTopAddresses],
    ['庄家集团 🕸️','clusters',renderClusters],
    ['Bitget OI 动向 📊','oi_surges',renderOiSurges],
    ['链上大额转账 ⛓️','onchain',renderOnchain],
    ['OKX 强平级联 💥','okx_liquidations',renderOkxLiquidations],
    ['OKX 跨所信号 🌐','okx_signals',renderOkxSignals],
    ['HL 挂单墙 🧱','okx_walls',renderHlWalls],
  ];

  document.getElementById('main').innerHTML=sections.map(([title,key,fn])=>
    `<div class="card">
      <div class="card-title">${{title}}</div>
      <div class="card-body">${{fn(state[key]||[])}}</div>
    </div>`
  ).join('');
}}

// ---------- 首屏 + 定时刷新 ----------
renderAll(S);
async function refresh(){{
  try{{
    const r=await fetch('/api/state');
    if(r.ok)renderAll(await r.json());
  }}catch(e){{console.warn('refresh err',e)}}
}}
setInterval(refresh,5000);
</script>
</body>
</html>"""


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
        """GET /api/harmonic/list — 谐波币列表 JSON（左面板，5s 轮询）。"""
        lst = build_harmonic_list(store)
        return aiohttp.web.json_response(lst, dumps=lambda o: json.dumps(o, default=str))

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

    app = aiohttp.web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_get("/health", handle_health)
    # 谐波形态独立页（与 HL 主页分开，/harmonic + /api/harmonic）
    app.router.add_get("/harmonic", handle_harmonic)
    app.router.add_get("/api/harmonic", handle_api_harmonic)
    # 谐波主-详情 SPA（新版：/harmonic2 + /api/harmonic/list + /api/harmonic/coin/{coin}）
    app.router.add_get("/harmonic2", handle_harmonic2)
    app.router.add_get("/api/harmonic/list", handle_harmonic_list)
    app.router.add_get("/api/harmonic/coin/{coin}", handle_harmonic_coin)

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


# 谐波页 HTML 模板（深色主题，与现有 _HTML_TEMPLATE 风格一致）
_HARMONIC_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>谐波形态 Setup</title>
<style>
:root{{
  --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;
  --muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;
  --yellow:#e3b341;--purple:#bc8cff;--orange:#ffa657;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:"SF Mono",ui-monospace,monospace;font-size:13px;line-height:1.5}}
header{{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}}
h1{{font-size:20px;color:var(--blue)}}
#meta{{color:var(--muted);font-size:12px}}
.nav-link{{font-size:12px;color:var(--muted);text-decoration:none;border:1px solid var(--border);
  border-radius:4px;padding:2px 8px}}
.nav-link:hover{{color:var(--blue);border-color:var(--blue)}}
.disclaimer{{margin:12px 16px;padding:10px 14px;background:#1c1a10;border:1px solid #5a4a00;
  border-radius:6px;color:var(--yellow);font-size:12px;line-height:1.6}}
main{{display:grid;gap:16px;padding:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
.card-title{{padding:10px 14px;border-bottom:1px solid var(--border);font-weight:700;
  color:var(--blue);font-size:13px}}
.card-body{{padding:12px 14px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{color:var(--muted);font-weight:600;text-align:left;padding:4px 6px;
  border-bottom:1px solid var(--border)}}
td{{padding:3px 6px;vertical-align:top;white-space:nowrap}}
tr:hover td{{background:rgba(255,255,255,.03)}}
.long{{color:var(--green)}} .short{{color:var(--red)}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}}
.none{{color:var(--muted);font-style:italic}}
.tag{{display:inline-block;padding:1px 5px;border-radius:4px;font-size:11px;font-weight:600}}
.tag-long{{background:#1a3a2a;color:var(--green)}}
.tag-short{{background:#3a1a1a;color:var(--red)}}
.coin{{color:var(--orange);font-weight:600}}
.conf-bar{{display:inline-block;height:8px;background:var(--blue);border-radius:2px;
  vertical-align:middle;margin-right:4px}}
.knn-ok{{color:var(--green)}} .knn-no{{color:var(--muted)}} .knn-unk{{color:var(--yellow)}}
.of-ok{{color:var(--green)}} .of-no{{color:var(--muted)}}
#refresh-bar{{font-size:11px;color:var(--muted);padding:4px 24px;border-top:1px solid var(--border)}}
/* 资产类别徽章 */
.badge-tradfi{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;
  font-weight:700;background:#3a2400;color:var(--orange);margin-right:3px}}
.badge-crypto{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;
  font-weight:700;background:#0e1f3a;color:var(--blue);margin-right:3px}}
/* 傻瓜版解释面板 */
details.explainer{{margin:12px 16px;background:#111820;border:1px solid #2a3f55;
  border-radius:6px;overflow:hidden}}
details.explainer summary{{padding:10px 14px;cursor:pointer;font-weight:700;
  color:var(--blue);font-size:13px;list-style:none;user-select:none}}
details.explainer summary::-webkit-details-marker{{display:none}}
details.explainer summary::before{{content:"▶ ";font-size:10px;color:var(--muted)}}
details[open].explainer summary::before{{content:"▼ ";font-size:10px;color:var(--muted)}}
.explainer-body{{padding:12px 16px;color:var(--text);font-size:12px;line-height:1.8;
  border-top:1px solid #2a3f55}}
.explainer-body dt{{font-weight:700;color:var(--yellow);margin-top:8px}}
.explainer-body dd{{margin-left:12px;color:var(--text)}}
.explainer-honest{{margin-top:10px;padding:8px 10px;background:#1c1a10;
  border-left:3px solid var(--yellow);color:var(--muted);font-size:11px}}
</style>
</head>
<body>
<header>
  <h1>🔷 谐波形态 + 可执行交易 Setup</h1>
  <span id="meta">加载中…</span>
  <a class="nav-link" href="/">← HL 主页</a>
</header>

<!-- 傻瓜版解释面板（默认展开，可折叠） -->
<details class="explainer" open>
  <summary>📖 名词傻瓜解释（点击收起）</summary>
  <div class="explainer-body">
    <dl>
      <dt>看多 / 看空</dt>
      <dd>看多 = 预期价格上涨，参考买入方向；看空 = 预期价格下跌，参考卖出方向。</dd>

      <dt>完整形态（入场触发）</dt>
      <dd>形态已走完 D 点，现在是参考入场区（<strong>反应式信号</strong>，价格已到达 PRZ，可观察是否反转确认）。</dd>

      <dt>成形中（前瞻 PRZ）</dt>
      <dd>形态尚未走完，系统<strong>前瞻预测</strong>未来反转区（PRZ）的价格范围。价格还没到，是预判性信号，不是当前入场点。</dd>

      <dt>斐波那契 / fib_note</dt>
      <dd>谐波形态本身基于斐波那契比率（0.618、0.786、0.886 等）定义 D 点/PRZ 位置。fib_note 列显示当前形态用到的具体 Fib 比率。</dd>

      <dt>🏦TradFi 徽章</dt>
      <dd>标的为 Bitget 代币化传统金融资产（美股/ETF/贵金属等，如 XAU、SOXL、AAPL），行情逻辑与原生加密不同。</dd>

      <dt>₿加密 徽章</dt>
      <dd>标的为原生加密货币（BTC/ETH/SOL 等），Bitget 永续合约。</dd>

      <dt>进场区（entry_lo ~ entry_hi）</dt>
      <dd>建议参考入场的价格区间（PRZ 范围内）。不是固定点，需结合当前成交量/订单流确认后再考虑介入。</dd>

      <dt>止损（stop）</dt>
      <dd>一旦价格跌破/突破此位，形态失效，应立即止损离场，不拖延。</dd>

      <dt>止盈（target1 / target2）</dt>
      <dd>形态结构给出的两个参考目标位（target1=保守，target2=扩展）。可分批止盈。</dd>

      <dt>盈亏比（RR）</dt>
      <dd>= (目标1 - 进场) / (进场 - 止损)。RR≥2 意味着赔 1 赚 2，是风险管理基础要求。</dd>

      <dt>订单流✓（orderflow）</dt>
      <dd>该价位附近有大挂单墙或成交量失衡确认（<strong>领先意图信号</strong>，优先于价格）。注意挂单墙可能 spoof（虚假挂单），需结合实际成交判断。</dd>

      <dt>KNN（≈随机基线，仅辅助）</dt>
      <dd>历史相似形态的方向参考。诚实标注：历史 KNN 命中率接近随机（无真实 alpha），仅供辅助参考，不可单独依赖。</dd>
    </dl>
    <div class="explainer-honest">
      ⚠️ <strong>诚实声明：以上为确认层参考工具，非投资建议。</strong>
      谐波形态 + 订单流确认提高概率，但不保证盈利；KNN ≈ 随机基线仅辅助；
      止损必须执行，价格进入进场区不等于必然反转。
    </div>
  </div>
</details>

<div class="disclaimer">
  ⚠️ <strong>确认层非投资建议</strong>：谐波PRZ前瞻 × 订单流确认；
  挂单墙可能 spoof/吸收 ≠ 必反转；KNN ≈ 随机基线（历史 KNN 命中无真实 alpha，仅供参考）。
  入场需等待价格进入进场区 + 订单流/成交量确认，止损触碰即离场。
</div>

<main id="main"><!-- 由 JS renderAll() 填充 --></main>
<div id="refresh-bar">自动刷新 · 5 秒</div>
<script>
const S = __INITIAL_STATE__;

// ---------- 工具函数 ----------
function fmtTime(ms){{
  if(ms==null)return'—';
  const d=new Date(ms);
  return d.toLocaleTimeString('zh-CN',{{hour12:false}});
}}
function fmtNum(v,dec){{
  if(v==null||v===undefined)return'—';
  const n=parseFloat(v);
  return isNaN(n)?'—':n.toFixed(dec!=null?dec:4);
}}
function fmtPct(v){{
  if(v==null)return'—';
  return(parseFloat(v)*100).toFixed(1)+'%';
}}
function none(){{return'<span class="none">（无数据）</span>';}}

// 方向标签：看多绿 / 看空红
function dirTag(d){{
  if(!d)return'—';
  if(d==='long')return'<span class="tag tag-long">看多</span>';
  if(d==='short')return'<span class="tag tag-short">看空</span>';
  return'<span>'+d+'</span>';
}}

// 置信进度条 + 百分比（confidence 为 0~1 小数）
function confBar(v){{
  if(v==null)return'—';
  const pct=Math.round(parseFloat(v)*100);
  const w=Math.max(2,pct);
  return'<span class="conf-bar" style="width:'+w+'px"></span>'+pct+'%';
}}

// KNN 标记（✓/✗/?）
function knnTag(v){{
  if(v==null||v==='')return'<span class="knn-unk">?</span>';
  if(v==='✓')return'<span class="knn-ok">✓</span>';
  if(v==='✗')return'<span class="knn-no">✗</span>';
  return'<span class="knn-unk">'+v+'</span>';
}}

// 订单流标记（'✓...'=确认/绿; '✗'=否定/灰; ''=无数据/灰）
function ofTag(v){{
  if(v==null||v==='')return'<span class="of-no">—</span>';
  if(v.startsWith('✓'))return'<span class="of-ok">'+v+'</span>';
  return'<span class="of-no">'+v+'</span>';
}}

// 资产类别徽章：tradfi → 🏦TradFi（橙色）；crypto → ₿加密（蓝色）
function badgeHtml(ac){{
  if(ac==='tradfi')return'<span class="badge-tradfi">🏦TradFi</span>';
  return'<span class="badge-crypto">₿加密</span>';
}}

// 进场区间 "entry_lo ~ entry_hi"，NULL 安全
function fmtRange(lo,hi){{
  if(lo==null&&hi==null)return'—';
  const l=lo!=null?fmtNum(lo,4):'—';
  const h=hi!=null?fmtNum(hi,4):'—';
  return l+' ~ '+h;
}}

// ---------- 完整形态（completed）表格渲染 ----------
function renderCompleted(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr>'
      +'<th>类别</th><th>币/周期</th><th>形态</th><th>方向</th><th>进场区</th>'
      +'<th>止损</th><th>目标1</th><th>目标2</th><th>盈亏比</th>'
      +'<th>置信</th><th>KNN</th><th>订单流</th>'
      +'</tr>';
  rows.forEach(r=>{{
    h+='<tr>'
      +'<td>'+badgeHtml(r.asset_class||'crypto')+'</td>'
      +'<td><span class="coin">'+r.coin+'</span>'
      +' <span style="color:var(--muted);font-size:11px">'+r.tf+'</span></td>'
      +'<td>'+r.pattern+'</td>'
      +'<td>'+dirTag(r.direction)+'</td>'
      +'<td>'+fmtRange(r.entry_lo,r.entry_hi)+'</td>'
      +'<td class="'+(r.direction==='long'?'neg':'pos')+'">'+fmtNum(r.stop,4)+'</td>'
      +'<td>'+fmtNum(r.target1,4)+'</td>'
      +'<td>'+fmtNum(r.target2,4)+'</td>'
      +'<td class="pos">'+fmtNum(r.rr,2)+'</td>'
      +'<td>'+confBar(r.confidence)+'</td>'
      +'<td>'+knnTag(r.knn)+'</td>'
      +'<td>'+ofTag(r.orderflow)+'</td>'
      +'</tr>';
  }});
  return h+'</table>';
}}

// ---------- 成形中（forming）PRZ 前瞻表格 ----------
function renderForming(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr>'
      +'<th>类别</th><th>币/周期</th><th>形态</th><th>方向</th><th>PRZ 区间</th><th>置信</th>'
      +'</tr>';
  rows.forEach(r=>{{
    h+='<tr>'
      +'<td>'+badgeHtml(r.asset_class||'crypto')+'</td>'
      +'<td><span class="coin">'+r.coin+'</span>'
      +' <span style="color:var(--muted);font-size:11px">'+r.tf+'</span></td>'
      +'<td>'+r.pattern+'</td>'
      +'<td>'+dirTag(r.direction)+'</td>'
      +'<td>'+fmtRange(r.prz_lo,r.prz_hi)+'</td>'
      +'<td>'+confBar(r.confidence)+'</td>'
      +'</tr>';
  }});
  return h+'</table>';
}}

// ---------- 主渲染 ----------
function renderAll(state){{
  const gen=state.generated_at||'--';
  document.getElementById('meta').textContent='生成于 '+gen;

  const sections=[
    ['✅ 完整形态（入场触发）','completed',renderCompleted],
    ['🔭 成形中（前瞻 PRZ）','forming',renderForming],
  ];

  document.getElementById('main').innerHTML=sections.map(([title,key,fn])=>
    '<div class="card">'
    +'<div class="card-title">'+title+'</div>'
    +'<div class="card-body">'+fn(state[key]||[])+'</div>'
    +'</div>'
  ).join('');
}}

// ---------- 首屏 + 5s 自动刷新 ----------
renderAll(S);
async function refresh(){{
  try{{
    const r=await fetch('/api/harmonic');
    if(r.ok)renderAll(await r.json());
  }}catch(e){{console.warn('harmonic refresh err',e)}}
}}
setInterval(refresh,5000);
</script>
</body>
</html>"""


def build_harmonic_list(store: Any) -> list[dict]:
    """聚合 recent_harmonic_setups → 每币一条汇总行，按 best_conf 降序。

    返回字段：coin, asset_class, best_conf, direction, n_setups, has_completed。
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

    # 按 best_conf 降序（None 排最后）
    result = list(agg.values())
    result.sort(key=lambda x: (x["best_conf"] is None, -(x["best_conf"] or 0)))
    return result


def build_coin_detail(store: Any, coin: str, tf: str | None = None) -> dict:
    """组装指定 coin（和 tf）的详情数据：蜡烛/setup/S/R/历史。

    tf 缺省时取该币在 recent_harmonic_setups 中首个 setup 的 tf。
    表缺/空时各字段返回 []，不抛。
    """
    from .asset_class import asset_class as _asset_class

    # 1. 读该币全部最新 setup 行（按 tf 过滤）
    all_setups: list[dict] = []
    tfs_available: list[str] = []
    resolved_tf = tf
    try:
        for r in store.recent_harmonic_setups():
            d = _row_to_dict(r, _HARMONIC_KEYS)
            if d.get("coin") != coin:
                continue
            d["asset_class"] = _asset_class(coin)
            all_setups.append(d)
            t = d.get("tf") or ""
            if t and t not in tfs_available:
                tfs_available.append(t)
    except Exception:  # noqa: BLE001
        pass

    # tf 缺省 → 用该币第一个 setup 的 tf
    if not resolved_tf:
        resolved_tf = tfs_available[0] if tfs_available else ""

    # 只保留目标 tf 的 setup
    setups = [d for d in all_setups if d.get("tf") == resolved_tf]

    # 2. 蜡烛（200 根）
    candles: list[list] = []
    if resolved_tf:
        try:
            raw_candles = store.get_candles(coin, resolved_tf, 200)
            candles = [
                [c.open_time_ms, c.o, c.h, c.l, c.c, c.v]
                for c in raw_candles
            ]
        except Exception:  # noqa: BLE001
            pass

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

    return {
        "coin": coin,
        "asset_class": _asset_class(coin),
        "tf": resolved_tf,
        "tfs_available": tfs_available,
        "candles": candles,
        "setups": setups,
        "sr": sr,
        "history": history,
    }


# ---------------------------------------------------------------------------
# 谐波主-详情 SPA 模板（左列表 + 右 SVG 蜡烛详情，无 CDN）
# ---------------------------------------------------------------------------
_HARMONIC_DETAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>谐波主-详情</title>
<style>
:root{{
  --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;
  --muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;
  --yellow:#e3b341;--purple:#bc8cff;--orange:#ffa657;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);
  font-family:"SF Mono",ui-monospace,monospace;font-size:13px;line-height:1.5;
  display:flex;flex-direction:column;height:100vh;overflow:hidden}}
header{{padding:10px 16px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:12px;flex-shrink:0}}
h1{{font-size:17px;color:var(--blue)}}
#meta{{color:var(--muted);font-size:11px;margin-left:auto}}
.layout{{display:flex;flex:1;overflow:hidden}}
/* 左面板 */
#left{{width:260px;flex-shrink:0;border-right:1px solid var(--border);
  display:flex;flex-direction:column;overflow:hidden}}
#filters{{padding:6px 8px;border-bottom:1px solid var(--border);
  display:flex;flex-wrap:wrap;gap:4px}}
.filter-btn{{font-size:11px;padding:2px 7px;border-radius:3px;border:1px solid var(--border);
  background:transparent;color:var(--muted);cursor:pointer}}
.filter-btn.active{{border-color:var(--blue);color:var(--blue);background:#0e1f3a}}
#coin-list{{overflow-y:auto;flex:1}}
.coin-row{{padding:7px 10px;cursor:pointer;border-bottom:1px solid rgba(48,54,61,.6);
  display:flex;align-items:center;gap:6px}}
.coin-row:hover{{background:rgba(255,255,255,.04)}}
.coin-row.selected{{background:#0e1f3a;border-left:3px solid var(--blue)}}
.coin-name{{font-weight:700;color:var(--orange)}}
.conf-txt{{font-size:11px;color:var(--muted)}}
/* 右面板 */
#right{{flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:12px}}
#right-empty{{color:var(--muted);padding:40px;text-align:center;font-size:13px}}
/* 周期 tabs */
.tf-tabs{{display:flex;gap:4px;flex-wrap:wrap}}
.tf-tab{{font-size:11px;padding:3px 9px;border-radius:3px;border:1px solid var(--border);
  background:transparent;color:var(--muted);cursor:pointer}}
.tf-tab.active{{border-color:var(--blue);color:var(--blue);background:#0e1f3a}}
/* SVG 蜡烛图容器 */
#chart-wrap{{background:var(--card);border:1px solid var(--border);border-radius:6px;
  overflow:hidden;padding:6px}}
/* 表格卡片 */
.card{{background:var(--card);border:1px solid var(--border);border-radius:6px;overflow:hidden}}
.card-title{{padding:8px 12px;border-bottom:1px solid var(--border);
  font-weight:700;color:var(--blue);font-size:12px}}
.card-body{{padding:10px 12px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{color:var(--muted);font-weight:600;text-align:left;padding:3px 5px;
  border-bottom:1px solid var(--border)}}
td{{padding:3px 5px;vertical-align:top;white-space:nowrap}}
tr:hover td{{background:rgba(255,255,255,.03)}}
.long{{color:var(--green)}} .short{{color:var(--red)}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}}
.none{{color:var(--muted);font-style:italic}}
.tag{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;font-weight:600}}
.tag-long{{background:#1a3a2a;color:var(--green)}}
.tag-short{{background:#3a1a1a;color:var(--red)}}
.badge-tradfi{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;
  font-weight:700;background:#3a2400;color:var(--orange);margin-right:3px}}
.badge-crypto{{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;
  font-weight:700;background:#0e1f3a;color:var(--blue);margin-right:3px}}
details.explainer{{background:var(--card);border:1px solid #2a3f55;border-radius:6px;overflow:hidden}}
details.explainer summary{{padding:8px 12px;cursor:pointer;font-weight:700;
  color:var(--blue);font-size:12px;list-style:none;user-select:none}}
details.explainer summary::-webkit-details-marker{{display:none}}
details.explainer summary::before{{content:"▶ ";font-size:10px;color:var(--muted)}}
details[open].explainer summary::before{{content:"▼ ";font-size:10px;color:var(--muted)}}
.explainer-body{{padding:10px 14px;font-size:11px;line-height:1.7;border-top:1px solid #2a3f55}}
.explainer-body dt{{font-weight:700;color:var(--yellow);margin-top:6px}}
.explainer-body dd{{margin-left:10px}}
.honest-note{{margin-top:8px;padding:6px 10px;background:#1c1a10;
  border-left:3px solid var(--yellow);color:var(--muted);font-size:11px}}
.disclaimer{{padding:7px 12px;background:#1c1a10;border:1px solid #5a4a00;
  border-radius:5px;color:var(--yellow);font-size:11px}}
#refresh-bar{{font-size:11px;color:var(--muted);padding:3px 16px;
  border-top:1px solid var(--border);flex-shrink:0}}
</style>
</head>
<body>
<header>
  <h1>🔷 谐波主-详情</h1>
  <a href="/harmonic" style="font-size:11px;color:var(--muted);text-decoration:none;
     border:1px solid var(--border);border-radius:3px;padding:2px 7px">旧版</a>
  <a href="/" style="font-size:11px;color:var(--muted);text-decoration:none;
     border:1px solid var(--border);border-radius:3px;padding:2px 7px">← HL 主页</a>
  <span id="meta">加载中…</span>
</header>
<div class="layout">
  <!-- 左面板 -->
  <div id="left">
    <div id="filters">
      <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">全部</button>
      <button class="filter-btn" data-filter="crypto" onclick="setFilter('crypto')">加密</button>
      <button class="filter-btn" data-filter="tradfi" onclick="setFilter('tradfi')">TradFi</button>
      <button class="filter-btn" data-filter="completed" onclick="setFilter('completed')">有完整形态</button>
    </div>
    <div id="coin-list"><!-- JS 渲染 --></div>
  </div>
  <!-- 右面板 -->
  <div id="right">
    <div id="right-empty">← 点击左侧币种查看详情</div>
  </div>
</div>
<div id="refresh-bar">左列表 5s 自动刷新 · 点击蜡烛图周期 tab 切换</div>
<script>
// 首屏注入左列表（array，非 object）
const S = __INITIAL_STATE__;

let _listData = S;       // 当前左列表数据
let _selectedCoin = '';  // 当前选中币
let _selectedTf  = '';   // 当前选中周期
let _curFilter   = 'all';// 当前过滤

// ---- 工具函数 ----
function fmtN(v, dec){{
  if(v==null||v===undefined)return'—';
  const n=parseFloat(v);
  return isNaN(n)?'—':n.toFixed(dec!=null?dec:4);
}}
function fmtPct(v){{
  if(v==null)return'—';
  return(parseFloat(v)*100).toFixed(1)+'%';
}}
function fmtTime(ms){{
  if(ms==null)return'—';
  const d=new Date(ms);
  return d.toLocaleTimeString('zh-CN',{{hour12:false}});
}}
function dirTag(d){{
  if(d==='long')return'<span class="tag tag-long">看多</span>';
  if(d==='short')return'<span class="tag tag-short">看空</span>';
  return d||'—';
}}
function badgeHtml(ac){{
  if(ac==='tradfi')return'<span class="badge-tradfi">🏦TradFi</span>';
  return'<span class="badge-crypto">₿加密</span>';
}}
function esc(s){{
  return String(s==null?'':s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

// ---- 左面板渲染 ----
function setFilter(f){{
  _curFilter=f;
  document.querySelectorAll('.filter-btn').forEach(b=>{{
    b.classList.toggle('active', b.dataset.filter===f);
  }});
  renderList(_listData);
}}
function renderList(rows){{
  let filtered=rows;
  if(_curFilter==='crypto')filtered=rows.filter(r=>r.asset_class==='crypto');
  else if(_curFilter==='tradfi')filtered=rows.filter(r=>r.asset_class==='tradfi');
  else if(_curFilter==='completed')filtered=rows.filter(r=>r.has_completed);
  // 按 best_conf 降序（null 排最后）
  filtered=[...filtered].sort((a,b)=>{{
    if(a.best_conf==null&&b.best_conf==null)return 0;
    if(a.best_conf==null)return 1;
    if(b.best_conf==null)return -1;
    return b.best_conf-a.best_conf;
  }});
  document.getElementById('coin-list').innerHTML=filtered.map(r=>{{
    const conf=r.best_conf!=null?Math.round(r.best_conf*100)+'%':'—';
    const dirCls=r.direction==='long'?'long':(r.direction==='short'?'short':'');
    const sel=r.coin===_selectedCoin?' selected':'';
    return`<div class="coin-row${{sel}}" onclick="selectCoin('${{esc(r.coin)}}')">
      ${{badgeHtml(r.asset_class)}}
      <span class="coin-name">${{esc(r.coin)}}</span>
      <span class="conf-txt ${{dirCls}}">${{conf}}</span>
      ${{r.direction==='long'?'<span class="long">▲</span>':r.direction==='short'?'<span class="short">▼</span>':''}}
    </div>`;
  }}).join('');
}}

// ---- 右面板：fetch 详情 ----
function selectCoin(coin, tf){{
  _selectedCoin=coin;
  if(tf)_selectedTf=tf;
  renderList(_listData);
  const url='/api/harmonic/coin/'+encodeURIComponent(coin)+(tf?'?tf='+encodeURIComponent(tf):'');
  fetch(url).then(r=>r.json()).then(d=>{{
    if(!tf)_selectedTf=d.tf||'';
    renderDetail(d);
  }}).catch(e=>{{
    document.getElementById('right').innerHTML='<div style="color:var(--red);padding:20px">加载失败: '+e+'</div>';
  }});
}}

// ---- SVG 蜡烛图 ----
function renderSvgCandles(candles, setups, sr, tf){{
  if(!candles||!candles.length)return'<div style="color:var(--muted);padding:20px">暂无K线数据（该周期未采集）</div>';
  const W=800, H=280, padL=60, padR=12, padT=12, padB=20;
  const n=candles.length;
  const cw=Math.max(2,Math.floor((W-padL-padR)/n)-1);
  const gap=Math.max(1,(W-padL-padR-n*cw)/(n-1||1));

  // 价格范围（含 setup 点，PRZ，S/R线）
  let lo=Infinity, hi=-Infinity;
  candles.forEach(c=>{{const h=parseFloat(c[2]),l=parseFloat(c[3]);if(h>hi)hi=h;if(l<lo)lo=l;}});
  setups.forEach(s=>{{
    [s.x_px,s.a_px,s.b_px,s.c_px,s.d_px,s.prz_lo,s.prz_hi,s.entry_lo,s.entry_hi,s.stop,s.target1,s.target2]
      .forEach(v=>{{if(v!=null){{const f=parseFloat(v);if(f>hi)hi=f;if(f<lo)lo=f;}}}});
  }});
  sr.filter(s=>s.tf===tf).forEach(s=>{{
    [s.upper,s.lower].forEach(v=>{{if(v!=null){{const f=parseFloat(v);if(f>hi)hi=f;if(f<lo)lo=f;}}}});
  }});
  const margin=(hi-lo)*0.05||1;
  hi+=margin; lo-=margin;
  const span=hi-lo||1;

  function py(price){{return padT+(hi-price)/span*(H-padT-padB);}}
  function px(i){{return padL+i*(cw+gap)+cw/2;}}

  let s=`<svg viewBox="0 0 ${{W}} ${{H}}" width="100%" height="${{H}}" xmlns="http://www.w3.org/2000/svg" style="display:block">`;

  // S/R 线（该 tf）
  sr.filter(r=>r.tf===tf).forEach(r=>{{
    if(r.upper!=null){{const y=py(r.upper);s+=`<line x1="${{padL}}" y1="${{y.toFixed(1)}}" x2="${{W-padR}}" y2="${{y.toFixed(1)}}" stroke="#f85149" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>`;}}
    if(r.lower!=null){{const y=py(r.lower);s+=`<line x1="${{padL}}" y1="${{y.toFixed(1)}}" x2="${{W-padR}}" y2="${{y.toFixed(1)}}" stroke="#3fb950" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>`;}}
  }});

  // Setup 水平虚线（进场/止损/目标）
  setups.forEach(su=>{{
    const lines=[
      [su.entry_lo,'#58a6ff',1.5,'6,3'],[su.stop,su.direction==='long'?'#f85149':'#3fb950',1,'4,3'],
      [su.target1,'#e3b341',1,'4,3'],[su.target2,'#bc8cff',1,'4,3'],
    ];
    lines.forEach(([v,c,w,da])=>{{
      if(v==null)return;
      const y=py(parseFloat(v));
      s+=`<line x1="${{padL}}" y1="${{y.toFixed(1)}}" x2="${{W-padR}}" y2="${{y.toFixed(1)}}" stroke="${{c}}" stroke-width="${{w}}" stroke-dasharray="${{da}}" opacity="0.85"/>`;
    }});
    // PRZ 区带
    if(su.prz_lo!=null&&su.prz_hi!=null){{
      const y1=py(Math.max(su.prz_lo,su.prz_hi)), y2=py(Math.min(su.prz_lo,su.prz_hi));
      s+=`<rect x="${{padL}}" y="${{y1.toFixed(1)}}" width="${{W-padL-padR}}" height="${{Math.max(1,(y2-y1)).toFixed(1)}}" fill="#58a6ff" opacity="0.08"/>`;
    }}
  }});

  // 蜡烛（OHLC）
  candles.forEach((c,i)=>{{
    const [ts,o,h,l,cl,v]=c.map(Number);
    const x=padL+i*(cw+gap);
    const cy=py(cl), oy=py(o);
    const top=Math.min(cy,oy), bh=Math.max(1,Math.abs(cy-oy));
    const color=cl>=o?'#3fb950':'#f85149';
    const mx=x+cw/2;
    // 影线
    s+=`<line x1="${{mx.toFixed(1)}}" y1="${{py(h).toFixed(1)}}" x2="${{mx.toFixed(1)}}" y2="${{py(l).toFixed(1)}}" stroke="${{color}}" stroke-width="1"/>`;
    // 实体
    s+=`<rect x="${{x.toFixed(1)}}" y="${{top.toFixed(1)}}" width="${{cw}}" height="${{bh.toFixed(1)}}" fill="${{color}}" rx="1"/>`;
  }});

  // XABCD polyline + 点 + 标注
  setups.forEach(su=>{{
    const pts=[
      [su.x_idx,su.x_px,'X'],[su.a_idx,su.a_px,'A'],
      [su.b_idx,su.b_px,'B'],[su.c_idx,su.c_px,'C'],[su.d_idx,su.d_px,'D'],
    ].filter(([idx,v])=>idx!=null&&v!=null);
    if(pts.length>1){{
      const poly=pts.map(([idx,v])=>`${{px(idx).toFixed(1)}},${{py(v).toFixed(1)}}`).join(' ');
      s+=`<polyline points="${{poly}}" fill="none" stroke="#ffa657" stroke-width="1.5" stroke-dasharray="3,2" opacity="0.9"/>`;
    }}
    pts.forEach(([idx,v,lbl])=>{{
      const cx=px(idx), cy2=py(v);
      s+=`<circle cx="${{cx.toFixed(1)}}" cy="${{cy2.toFixed(1)}}" r="4" fill="#ffa657" opacity="0.9"/>`;
      s+=`<text x="${{(cx+5).toFixed(1)}}" y="${{(cy2-5).toFixed(1)}}" fill="#ffa657" font-size="10" font-weight="bold">${{lbl}}</text>`;
    }});
  }});

  // Y 轴价格刻度（4~5 个）
  const nTicks=5;
  for(let i=0;i<=nTicks;i++){{
    const price=lo+(span*i/nTicks);
    const y=py(price);
    s+=`<text x="${{(padL-4).toFixed(1)}}" y="${{(y+4).toFixed(1)}}" fill="#8b949e" font-size="9" text-anchor="end">${{price>100?price.toFixed(0):price.toFixed(4)}}</text>`;
    s+=`<line x1="${{padL}}" y1="${{y.toFixed(1)}}" x2="${{W-padR}}" y2="${{y.toFixed(1)}}" stroke="#30363d" stroke-width="0.5"/>`;
  }}

  s+='</svg>';
  return s;
}}

// ---- 多周期 S/R 表 ----
function renderSrTable(sr){{
  if(!sr||!sr.length)return'<span class="none">（无 S/R 数据）</span>';
  let h='<table><tr><th>周期</th><th>压力(上轨)</th><th>支撑(下轨)</th><th>%B</th><th>挤压</th></tr>';
  sr.forEach(r=>{{
    h+=`<tr>
      <td style="color:var(--blue)">${{r.tf||'—'}}</td>
      <td class="neg">${{fmtN(r.upper,4)}}</td>
      <td class="pos">${{fmtN(r.lower,4)}}</td>
      <td>${{fmtN(r.pct_b,3)}}</td>
      <td>${{r.squeeze?'<span style="color:var(--yellow)">挤压</span>':'—'}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

// ---- Setup 明细 ----
function renderSetupDetail(setups){{
  if(!setups||!setups.length)return'<span class="none">（该周期无 setup）</span>';
  const su=setups[0];
  const entry=su.entry_lo!=null||su.entry_hi!=null
    ?(fmtN(su.entry_lo,4)+' ~ '+fmtN(su.entry_hi,4)):'—';
  const prz=su.prz_lo!=null||su.prz_hi!=null
    ?(fmtN(su.prz_lo,4)+' ~ '+fmtN(su.prz_hi,4)):'—';
  return`<table>
    <tr><th>形态</th><td>${{su.pattern||'—'}}</td><th>方向</th><td>${{dirTag(su.direction)}}</td></tr>
    <tr><th>进场区</th><td>${{entry}}</td><th>PRZ区</th><td>${{prz}}</td></tr>
    <tr><th>止损</th><td class="${{su.direction==='long'?'neg':'pos'}}">${{fmtN(su.stop,4)}}</td>
        <th>目标1</th><td class="pos">${{fmtN(su.target1,4)}}</td></tr>
    <tr><th>目标2</th><td class="pos">${{fmtN(su.target2,4)}}</td>
        <th>盈亏比</th><td class="pos">${{fmtN(su.rr,2)}}</td></tr>
    <tr><th>置信</th><td>${{su.confidence!=null?Math.round(su.confidence*100)+'%':'—'}}</td>
        <th>KNN</th><td>${{su.knn||'—'}}</td></tr>
    <tr><th>订单流</th><td colspan="3">${{su.orderflow||'—'}}</td></tr>
    <tr><th>Fib注记</th><td colspan="3">${{su.fib_note||'—'}}</td></tr>
  </table>`;
}}

// ---- 历史形态 ----
function renderHistory(history){{
  if(!history||!history.length)return'<span class="none">（暂无历史记录）</span>';
  let h='<table><tr><th>时间</th><th>周期</th><th>形态</th><th>方向</th><th>置信</th><th>状态</th></tr>';
  history.slice(0,15).forEach(r=>{{
    h+=`<tr>
      <td style="color:var(--muted)">${{fmtTime(r.ts)}}</td>
      <td style="color:var(--muted)">${{r.tf||'—'}}</td>
      <td>${{r.pattern||'—'}}</td>
      <td>${{dirTag(r.direction)}}</td>
      <td>${{r.confidence!=null?Math.round(r.confidence*100)+'%':'—'}}</td>
      <td style="color:var(--muted)">${{r.kind||'—'}}</td>
    </tr>`;
  }});
  return h+'</table>';
}}

// ---- 右面板主渲染 ----
function renderDetail(d){{
  if(!d){{document.getElementById('right').innerHTML='<div id="right-empty">← 点击左侧币种查看详情</div>';return;}}
  const coin=d.coin||'';
  const ac=d.asset_class||'crypto';
  const tf=d.tf||'';
  const tfs=d.tfs_available||[];

  // 周期 tabs
  const tabsHtml=tfs.length
    ?'<div class="tf-tabs">'+tfs.map(t=>
        `<button class="tf-tab${{t===tf?' active':''}}" onclick="selectCoin('${{esc(coin)}}','${{esc(t)}}')">${{t}}</button>`
      ).join('')+'</div>'
    :'';

  // SVG 蜡烛图
  const svgHtml=renderSvgCandles(d.candles,d.setups||[],d.sr||[],tf);

  const html=
    // 头部
    `<div style="display:flex;align-items:center;gap:8px">
       ${{badgeHtml(ac)}}<span style="font-size:16px;font-weight:700;color:var(--orange)">${{esc(coin)}}</span>
       <span style="color:var(--muted);font-size:12px">周期: ${{tf||'—'}}</span>
     </div>`+
    tabsHtml+
    // 蜡烛图
    `<div class="card"><div class="card-title">📈 蜡烛图（含 XABCD / PRZ / S&R 叠加）</div>
       <div class="card-body" id="chart-wrap">${{svgHtml}}</div></div>`+
    // 多周期 S/R
    `<div class="card"><div class="card-title">📐 多周期 S/R（布林带压力/支撑）</div>
       <div class="card-body">${{renderSrTable(d.sr||[])}}</div></div>`+
    // Setup 明细
    `<div class="card"><div class="card-title">⚡ Setup 明细</div>
       <div class="card-body">${{renderSetupDetail(d.setups||[])}}</div></div>`+
    // 历史
    `<div class="card"><div class="card-title">🕐 历史形态</div>
       <div class="card-body">${{renderHistory(d.history||[])}}</div></div>`+
    // 傻瓜解释
    `<details class="explainer">
       <summary>📖 名词傻瓜解释（点击展开）</summary>
       <div class="explainer-body">
         <dl>
           <dt>看多 / 看空</dt><dd>看多=预期涨，参考做多方向；看空=预期跌，参考做空方向。</dd>
           <dt>蜡烛图（OHLC）</dt><dd>每根蜡烛显示一个周期内的开盘/收盘/最高/最低价。绿柱=上涨；红柱=下跌。</dd>
           <dt>XABCD 形态连线</dt><dd>谐波形态的五个关键价格点，用橙色线连接，D 点是潜在反转位。</dd>
           <dt>PRZ（潜在反转区）</dt><dd>蓝色半透明区带，是形态预测的价格反转区间。</dd>
           <dt>进场区（蓝色虚线）</dt><dd>建议参考入场的价格区间（PRZ 范围内）。需等订单流/成交量确认。</dd>
           <dt>止损（红/绿虚线）</dt><dd>价格突破此位形态失效，应立即止损。</dd>
           <dt>目标（黄/紫虚线）</dt><dd>参考止盈位：target1=保守，target2=扩展目标。</dd>
           <dt>S/R 线（布林带上下轨）</dt><dd>红色虚线=压力位（上轨）；绿色虚线=支撑位（下轨）。</dd>
           <dt>斐波那契 / fib_note</dt><dd>谐波形态基于斐波那契比率（0.618/0.786/0.886等）定义 PRZ。</dd>
           <dt>订单流（前瞻领先信号）</dt><dd>PRZ 附近挂单/成交量异常确认。注意挂单墙可能 spoof（虚假）。</dd>
           <dt>KNN（仅辅助，≈随机基线）</dt><dd>历史相似形态参考，诚实标注：命中率接近随机，不可单独依赖。</dd>
         </dl>
         <div class="honest-note">⚠️ <strong>诚实声明：确认层非投资建议。</strong>
           谐波 PRZ + 订单流提高概率，不保证盈利；KNN ≈ 随机；挂单墙可能 spoof；
           止损必须执行。前瞻预测为参考，不是入场信号。</div>
       </div>
     </details>`+
    `<div class="disclaimer">⚠️ 确认层非投资建议：PRZ 前瞻 × 订单流确认；KNN ≈ 随机基线；墙可能 spoof。</div>`;

  document.getElementById('right').innerHTML=html;
}}

// ---- 初始渲染左列表 ----
renderList(_listData);
document.getElementById('meta').textContent='左列表 5s 刷新中';

// ---- 5s 轮询左列表 ----
async function refreshList(){{
  try{{
    const r=await fetch('/api/harmonic/list');
    if(r.ok){{
      _listData=await r.json();
      renderList(_listData);
      document.getElementById('meta').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{{hour12:false}});
    }}
  }}catch(e){{console.warn('harmonic list refresh err',e)}}
}}
setInterval(refreshList,5000);
</script>
</body>
</html>"""


def render_harmonic_detail_html(list_state: list[dict]) -> str:
    """将 build_harmonic_list 的结果渲染成谐波主-详情自包含 HTML 页。

    list_state 注入为 JS 数组（首屏左面板），右面板详情按需 fetch。
    双括号转义模式与 render_html / render_harmonic_html 完全一致。
    """
    state_json = json.dumps(list_state, ensure_ascii=False, default=str)
    html = _HARMONIC_DETAIL_TEMPLATE.replace("{{", "{").replace("}}", "}")
    return html.replace("__INITIAL_STATE__", state_json)


def render_harmonic_html(state: dict) -> str:
    """将 build_harmonic_state 结果渲染成谐波形态独立自包含 HTML 页。

    复用与 render_html 相同的 {{/}} 转义 + __INITIAL_STATE__ 注入模式。
    """
    state_json = json.dumps(state, ensure_ascii=False, default=str)
    html = _HARMONIC_HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    return html.replace("__INITIAL_STATE__", state_json)


# 兼容直接 import asyncio
import asyncio  # noqa: E402
