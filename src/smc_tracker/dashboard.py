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
from .dashboard_harmonic import render_harmonic_html, render_harmonic_detail_html  # noqa: F401


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
  --card-shadow:0 1px 4px rgba(0,0,0,.35);
  --accent-border:rgba(88,166,255,.28);--accent-bg:rgba(88,166,255,.06);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);
  font-family:'Inter',system-ui,-apple-system,sans-serif;
  font-size:13px;line-height:1.5}}
.mono{{font-family:"SF Mono",ui-monospace,monospace;
  font-variant-numeric:tabular-nums}}
header{{padding:14px 24px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
h1{{font-size:19px;color:var(--blue);font-weight:700}}
#meta{{color:var(--muted);font-size:12px}}
.hdr-nav{{display:flex;gap:6px;margin-left:auto}}
.hdr-nav a{{font-size:11.5px;color:var(--muted);text-decoration:none;
  border:1px solid var(--border);border-radius:5px;padding:3px 10px;
  transition:color .15s,border-color .15s}}
.hdr-nav a:hover{{color:var(--blue);border-color:var(--blue)}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:16px;padding:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden;
  box-shadow:var(--card-shadow);
  transition:box-shadow .15s,border-color .15s}}
.card:hover{{box-shadow:0 2px 8px rgba(0,0,0,.45)}}
.card.accent{{border-color:var(--accent-border)}}
.card.accent .card-title{{background:var(--accent-bg)}}
.card-title{{padding:10px 14px;border-bottom:1px solid var(--border);
  font-weight:700;color:var(--blue);font-size:13px}}
.card-body{{padding:12px 14px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{color:var(--muted);font-weight:600;text-align:left;padding:4px 6px;border-bottom:1px solid var(--border)}}
td{{padding:3px 6px;vertical-align:top;white-space:nowrap}}
tr:hover td{{background:rgba(255,255,255,.06)}}
.long{{color:var(--green)}} .short{{color:var(--red)}}
.bullish{{color:var(--green)}} .bearish{{color:var(--red)}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}}
.none{{color:var(--muted);font-style:italic}}
.tag{{display:inline-block;padding:1px 5px;border-radius:4px;font-size:11px;font-weight:600}}
.tag-long{{background:#1a3a2a;color:var(--green)}}
.tag-short{{background:#3a1a1a;color:var(--red)}}
.addr{{font-family:"SF Mono",ui-monospace,monospace;font-size:11px;color:var(--purple)}}
.coin{{color:var(--orange);font-weight:600}}
.score{{color:var(--yellow)}}
#refresh-bar{{font-size:11px;color:var(--muted);padding:4px 24px;border-top:1px solid var(--border)}}
@keyframes flashin{{0%{{background:rgba(88,166,255,.12)}}100%{{background:transparent}}}}
</style>
</head>
<body>
<header>
  <h1>🐋 SMC 抓庄监控</h1>
  <span id="meta">加载中…</span>
  <nav class="hdr-nav">
    <a href="/hl2">HL 系统</a>
    <a href="/harmonic2">谐波系统</a>
    <a href="/signals">信号总览</a>
  </nav>
</header>
<main id="main"><!-- 由 JS renderAll() 填充 --></main>
<div id="refresh-bar">自动刷新 · 5 秒</div>
<script>
const S = __INITIAL_STATE__;

// ---- CSS 色彩 token（单一来源，SVG 拼接复用）----
const CV = {{
  green:'#3fb950', red:'#f85149', blue:'#58a6ff',
  muted:'#8b949e', border:'#30363d',
}};

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
    +`stroke="${{CV.border}}" stroke-width="1"/>`;
  items.forEach((it,i)=>{{
    const v=parseFloat(getVal(it))||0;
    const y=padT+i*rowH;
    const cy=y+rowH/2;
    const len=Math.abs(v)/maxAbs*half;
    const color=v>=0?CV.green:CV.red;   // 正绿/负红
    // 条形：正值从中线向右，负值从中线向左
    const bx=v>=0?mid:(mid-len);
    s+=`<rect x="${{bx}}" y="${{y+4}}" width="${{Math.max(len,0.5)}}" height="${{rowH-8}}" `
      +`fill="${{color}}" rx="2"/>`;
    // 左侧标签
    s+=`<text x="4" y="${{cy+4}}" fill="${{CV.muted}}" font-size="11">`
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
  const color=opts.color||CV.blue;
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

  // accent=true → 蓝边高亮（核心卡片：鲸鱼信号/净流向/系统健康）
  const sections=[
    ['🩺 系统健康','health',renderHealth,true],
    ['📊 预测准确率(诚实回顾)','accuracy',renderAccuracy,false],
    ['🏦 交易所资金流(24h)','exchange_flows',renderExchangeFlows,false],
    ['🏦 钱包持仓画像','wallet_portfolio',renderWalletPortfolio,false],
    ['共振信号 ⚡','signals',renderSignals,false],
    ['背离信号 🔀','divergence',renderDivergence,false],
    ['聪明钱净流向 🐋','whale_flows',renderWhaleFlows,true],
    ['鲸鱼信号 🚨','whale_signals',renderWhaleSignals,true],
    ['聪明钱地址排行 🏆','top_addresses',renderTopAddresses,false],
    ['庄家集团 🕸️','clusters',renderClusters,false],
    ['Bitget OI 动向 📊','oi_surges',renderOiSurges,false],
    ['链上大额转账 ⛓️','onchain',renderOnchain,false],
    ['OKX 强平级联 💥','okx_liquidations',renderOkxLiquidations,false],
    ['OKX 跨所信号 🌐','okx_signals',renderOkxSignals,false],
    ['HL 挂单墙 🧱','okx_walls',renderHlWalls,false],
  ];

  document.getElementById('main').innerHTML=sections.map(([title,key,fn,accent])=>
    `<div class="card${{accent?' accent':''}}">
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
    if(r.ok){{
      renderAll(await r.json());
      // 数据更新时对全部 .card-body 触发 flashin 动画（视觉反馈）
      document.querySelectorAll('.card-body').forEach(el=>{{
        el.style.animation='none';
        el.offsetHeight;
        el.style.animation='flashin .6s ease-out';
      }});
    }}
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

_HL_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HL 聪明钱追踪终端 · SMC</title>
<style>
/* ---- 设计系统 token（浅色金融终端，复用 _HARMONIC_DETAIL_TEMPLATE token）---- */
:root{{
  --bg:#eef3fa;--panel:#ffffff;--line:#e4eaf3;--line2:#eff3f9;
  --t1:#0f1c33;--t2:#5b6b85;--t3:#9aa7bd;
  --blue:#2563eb;--blue2:#1d4ed8;--bluebg:#eaf1ff;
  --long:#16a34a;--short:#e23744;--longbg:#e8f6ee;--shortbg:#fdecee;
  --amber:#e6a23c;--violet:#a855f7;--orange:#c2600a;
  --shadow:0 1px 3px rgba(0,0,0,.1);
  --hover:rgba(37,99,235,.06);--sel:var(--bluebg);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#f6f8fa;color:var(--t1);
  font-family:'IBM Plex Sans',system-ui,-apple-system,sans-serif;
  font-size:13px;line-height:1.5;
  display:flex;flex-direction:column;height:100vh;overflow:hidden}}
.mono{{font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-variant-numeric:tabular-nums;letter-spacing:-.2px}}

/* ---- Header（54px）---- */
header{{height:54px;padding:0 16px;border-bottom:1px solid var(--line);
  background:var(--panel);display:flex;align-items:center;gap:12px;flex-shrink:0;
  box-shadow:0 1px 0 rgba(37,99,235,.06)}}
.hdr-logo{{font-size:14px;font-weight:700;color:var(--t1);
  display:flex;align-items:center;gap:7px}}
.hdr-logo-dot{{width:8px;height:8px;border-radius:50%;background:var(--blue)}}
.hdr-tabs{{display:flex;gap:2px;background:var(--bg);padding:3px;border-radius:8px}}
.hdr-tab{{padding:4px 12px;border-radius:6px;font-size:12px;font-weight:600;
  cursor:pointer;color:var(--t2);background:transparent;border:none;
  text-decoration:none;display:inline-block;transition:background .15s,color .15s}}
.hdr-tab:hover{{color:var(--blue)}}
.hdr-tab.active{{background:var(--panel);color:var(--blue);
  box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.hdr-live{{display:inline-flex;align-items:center;gap:5px;font-size:11px;
  font-weight:700;color:var(--long)}}
.hdr-live-dot{{width:7px;height:7px;border-radius:50%;background:var(--long);
  animation:pulse 1.4s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.25}}}}
#hdr-clock{{font-size:11px;color:var(--t3);font-family:'IBM Plex Mono',monospace}}
#meta{{color:var(--t2);font-size:11px;margin-left:auto}}

/* ---- KPI strip（6 格）---- */
#kpi-strip{{background:var(--panel);border-bottom:1px solid var(--line);
  padding:6px 16px;display:grid;
  grid-template-columns:repeat(6,1fr);gap:0;flex-shrink:0}}
.kpi-cell{{display:flex;flex-direction:column;gap:1px;padding:4px 8px 4px 0;
  border-right:1px solid var(--line2)}}
.kpi-cell:last-child{{border-right:none}}
.kpi-label{{font-size:9.5px;color:var(--t3);font-weight:600;text-transform:uppercase;
  letter-spacing:.4px}}
.kpi-val{{font-size:15px;font-weight:700;color:var(--t1);
  font-family:'IBM Plex Mono',monospace;font-variant-numeric:tabular-nums;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.kpi-val.blue{{color:var(--blue)}}
.kpi-val.long{{color:var(--long)}}
.kpi-val.short{{color:var(--short)}}

/* ---- 三栏主体（262px / 1fr / 372px）---- */
.hl-layout{{display:grid;grid-template-columns:262px minmax(0,1fr) 372px;
  flex:1;overflow:hidden}}
@media(max-width:960px){{
  .hl-layout{{grid-template-columns:1fr;overflow:auto}}
  #hl-left,#hl-right{{width:auto;border:none}}
}}
@media(max-width:1100px) and (min-width:961px){{
  .hl-layout{{grid-template-columns:220px minmax(0,1fr) 320px}}
  #kpi-strip{{grid-template-columns:repeat(3,1fr)}}
}}

/* ---- 左面板 ---- */
#hl-left{{border-right:1px solid var(--line);background:var(--panel);
  display:flex;flex-direction:column;overflow:hidden}}
#hl-left-header{{padding:8px 10px 6px;border-bottom:1px solid var(--line);
  display:flex;align-items:center;justify-content:space-between;
  background:var(--panel)}}
.left-title{{font-size:12.5px;font-weight:700}}
.left-sub{{font-size:10.5px;color:var(--t3)}}
#hl-search-bar{{padding:5px 8px;border-bottom:1px solid var(--line2)}}
#hl-coin-search{{width:100%;font-size:11px;padding:4px 7px;border:1px solid var(--line);
  border-radius:6px;background:var(--bg);color:var(--t1);outline:none}}
#hl-coin-search:focus{{border-color:var(--blue);background:var(--panel)}}
#hl-flow-filters{{padding:5px 8px;border-bottom:1px solid var(--line2);
  display:flex;gap:3px}}
.hl-filter-btn{{font-size:10.5px;padding:2px 8px;border-radius:5px;
  border:1px solid var(--line);background:transparent;
  color:var(--t2);cursor:pointer;font-weight:600}}
.hl-filter-btn:hover{{border-color:var(--blue);color:var(--blue)}}
.hl-filter-btn.active{{border-color:var(--blue);color:var(--blue);
  background:var(--bluebg)}}
#hl-coin-list{{overflow-y:auto;flex:1}}
.hl-coin-row{{padding:9px 12px 9px 10px;cursor:pointer;
  border-left:3px solid transparent;border-bottom:1px solid var(--line2);
  display:flex;flex-direction:column;gap:5px}}
.hl-coin-row:hover{{background:var(--hover)}}
.hl-coin-row.selected{{background:var(--bluebg);border-left-color:var(--blue)}}
.hl-coin-row-top{{display:flex;align-items:center;justify-content:space-between;gap:8px}}
.hl-coin-name{{font-size:13px;font-weight:700;color:var(--orange)}}
.hl-coin-price{{font-size:12px;font-weight:600;font-family:'IBM Plex Mono',monospace}}
.hl-flow-bar-wrap{{display:flex;align-items:center;gap:8px;margin-top:4px}}
.hl-flow-track{{flex:1;height:5px;border-radius:3px;background:var(--bg);
  position:relative;overflow:hidden}}
.hl-flow-fill{{position:absolute;top:0;height:100%;border-radius:3px}}
.hl-flow-label{{font-size:10px;font-family:'IBM Plex Mono',monospace;
  min-width:54px;text-align:right}}
.hl-coin-bot{{display:flex;align-items:center;justify-content:space-between;
  font-size:10px;color:var(--t3)}}

/* ---- 中栏 ---- */
#hl-main{{overflow-y:auto;padding:12px 14px;
  display:flex;flex-direction:column;gap:12px}}
.hl-card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  overflow:hidden;box-shadow:var(--shadow)}}
.hl-card-title{{padding:8px 14px;border-bottom:1px solid var(--line2);
  font-size:13.5px;font-weight:700;
  display:flex;align-items:center;justify-content:space-between}}
.hl-card-sub{{font-size:10.5px;color:var(--t3);font-weight:400}}
.hl-card-body{{padding:10px 14px;overflow-x:auto}}
/* 地址排行表 */
.hl-addr-table{{width:100%;border-collapse:collapse;font-size:11px}}
.hl-addr-table th{{color:var(--t3);font-weight:600;padding:3px 5px;
  border-bottom:1px solid var(--line2);text-align:left;white-space:nowrap}}
.hl-addr-table td{{padding:4px 5px;vertical-align:top;white-space:nowrap}}
.hl-addr-table tr:hover td{{background:var(--hover)}}
.addr{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--violet,#a855f7)}}
.tag{{display:inline-block;padding:1px 6px;border-radius:5px;font-size:10px;font-weight:600}}
.tag-long{{background:var(--longbg);color:var(--long)}}
.tag-short{{background:var(--shortbg);color:var(--short)}}
.pos{{color:var(--long)}} .neg{{color:var(--short)}}

/* ---- 右侧栏 ---- */
#hl-right{{border-left:1px solid var(--line);background:var(--panel);
  overflow:hidden;display:flex;flex-direction:column}}
.hl-feed-tabs{{display:flex;gap:2px;padding:7px 10px;
  border-bottom:1px solid var(--line);flex-shrink:0;flex-wrap:wrap}}
.hl-feed-tab{{padding:5px 10px;border-radius:6px;font-size:11.5px;font-weight:600;
  cursor:pointer;color:var(--t2);border:none;background:transparent}}
.hl-feed-tab.active{{background:var(--bluebg);color:var(--blue)}}
#hl-feed-body{{flex:1;overflow-y:auto}}

/* ---- 右侧面板条目 ---- */
.hl-event-row{{padding:9px 13px;border-bottom:1px solid var(--line2);
  display:flex;gap:10px}}
.hl-event-icon{{width:40px;height:40px;border-radius:8px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  flex-shrink:0;font-size:11px;font-weight:700}}
.hl-event-body{{flex:1;min-width:0}}
.hl-event-sym{{font-size:12.5px;font-weight:700}}
.hl-event-meta{{font-size:10.5px;color:var(--t2);
  font-family:'IBM Plex Mono',monospace}}
.hl-event-bot{{display:flex;justify-content:space-between;
  font-size:10px;color:var(--t3)}}
.hl-whale-row{{padding:10px 13px;border-bottom:1px solid var(--line2)}}
.hl-whale-top{{display:flex;align-items:center;
  justify-content:space-between;gap:8px}}
.hl-whale-grid{{display:grid;grid-template-columns:repeat(4,1fr);
  gap:5px;margin-top:7px}}
.hl-whale-kv{{display:flex;flex-direction:column}}
.hl-whale-k{{font-size:9px;color:var(--t3)}}
.hl-whale-v{{font-size:11.5px;font-weight:700;
  font-family:'IBM Plex Mono',monospace}}
.hl-cons-row{{padding:8px 10px;border:1px solid var(--line);
  border-radius:9px;margin-bottom:7px}}
.hl-div-row{{padding:9px 10px;border:1px solid var(--line);
  border-radius:9px;margin-bottom:7px;border-left-width:3px}}
.hl-wall-row{{padding:7px 13px;border-bottom:1px solid var(--line2);
  display:flex;align-items:center;gap:10px;font-size:11px}}

/* 底部状态条 */
#refresh-bar{{font-size:11px;color:var(--t2);padding:3px 16px;
  background:var(--panel);border-top:1px solid var(--line);flex-shrink:0}}
</style>
</head>
<body>
<!-- ======== Header ======== -->
<header>
  <div class="hdr-logo">
    <span class="hdr-logo-dot"></span>
    SMC 聪明钱追踪终端
  </div>
  <div class="hdr-tabs">
    <span class="hdr-tab active">HL 系统</span>
    <a href="/harmonic2" class="hdr-tab" style="text-decoration:none">谐波系统</a>
  </div>
  <span class="hdr-live"><span class="hdr-live-dot"></span>LIVE</span>
  <span id="hdr-clock">--:--:--</span>
  <span id="meta">加载中…</span>
</header>

<!-- ======== KPI strip（6 格）======== -->
<div id="kpi-strip">
  <div class="kpi-cell">
    <span class="kpi-label">聪明钱地址</span>
    <span class="kpi-val blue mono" id="kpi-addrs">—</span>
  </div>
  <div class="kpi-cell">
    <span class="kpi-label">监控币种</span>
    <span class="kpi-val mono" id="kpi-coins">—</span>
  </div>
  <div class="kpi-cell">
    <span class="kpi-label">庄家集团</span>
    <span class="kpi-val mono" id="kpi-clusters">—</span>
  </div>
  <div class="kpi-cell">
    <span class="kpi-label">净主动买入</span>
    <span class="kpi-val long mono" id="kpi-netbuy">—</span>
  </div>
  <div class="kpi-cell">
    <span class="kpi-label">鲸鱼信号</span>
    <span class="kpi-val mono" id="kpi-wsigs">—</span>
  </div>
  <div class="kpi-cell">
    <span class="kpi-label">数据时间</span>
    <span class="kpi-val mono" id="kpi-time">—</span>
  </div>
</div>

<!-- ======== 三栏主体 ======== -->
<div class="hl-layout">
  <!-- === 左 262px：币种列表（净主动流向）=== -->
  <div id="hl-left">
    <div id="hl-left-header">
      <span class="left-title">监控币种</span>
      <span class="left-sub" id="hl-left-count">净主动流向</span>
    </div>
    <div id="hl-search-bar">
      <input id="hl-coin-search" type="text" placeholder="搜索币种…"
             oninput="hlOnSearch(this.value)">
    </div>
    <div id="hl-flow-filters">
      <button class="hl-filter-btn active" data-f="all"
              onclick="hlSetFilter('all')">全部</button>
      <button class="hl-filter-btn" data-f="buy"
              onclick="hlSetFilter('buy')">净买</button>
      <button class="hl-filter-btn" data-f="sell"
              onclick="hlSetFilter('sell')">净卖</button>
    </div>
    <div id="hl-coin-list"><!-- JS 渲染 --></div>
  </div>

  <!-- === 中栏：主区域（流向图 + 地址排行 + 集团）=== -->
  <div id="hl-main">
    <!-- 净主动流向 SVG bar chart -->
    <div class="hl-card">
      <div class="hl-card-title">
        聪明钱净主动流向
        <span class="hl-card-sub">hl_meme_trades · taker 主动方聚合</span>
      </div>
      <div class="hl-card-body">
        <svg id="flow-svg" viewBox="0 0 800 150" style="width:100%;display:block">
          <line x1="6" y1="75" x2="794" y2="75" stroke="#e4eaf3" stroke-width="1"/>
          <text x="10" y="16" font-size="10.5" fill="#9aa7bd" font-family="IBM Plex Sans,system-ui,sans-serif">主动买入 ↑</text>
          <text x="10" y="146" font-size="10.5" fill="#9aa7bd" font-family="IBM Plex Sans,system-ui,sans-serif">主动卖出 ↓</text>
          <g id="flow-bars"></g>
          <path id="flow-cum-path" fill="none" stroke="#2563eb" stroke-width="1.6"/>
          <text id="flow-cum-label" x="792" y="16" text-anchor="end"
                font-size="10.5" font-family="IBM Plex Mono,monospace" fill="#2563eb"></text>
        </svg>
      </div>
    </div>

    <!-- 地址排行 -->
    <div class="hl-card">
      <div class="hl-card-title">
        聪明钱地址排行
        <span class="hl-card-sub">address_profiles · 评分/持仓/月PnL</span>
      </div>
      <div class="hl-card-body">
        <table class="hl-addr-table">
          <thead>
            <tr>
              <th>地址</th><th>评分</th><th>持仓净值</th>
              <th>月PnL</th><th>胜率</th><th>偏好</th>
            </tr>
          </thead>
          <tbody id="addr-tbody"></tbody>
        </table>
        <div id="addr-empty" style="display:none;padding:20px;text-align:center;
             color:var(--t3);font-size:12px">
          暂无数据 — 等待地址画像入库
        </div>
      </div>
    </div>

    <!-- 庄家集团 -->
    <div class="hl-card">
      <div class="hl-card-title">
        庄家集团
        <span class="hl-card-sub">address_correlation · 跨币协同 ≥2 币</span>
      </div>
      <div class="hl-card-body" id="clusters-body">
        <div style="color:var(--t3);font-size:12px">暂无数据</div>
      </div>
    </div>
  </div>

  <!-- === 右侧栏 372px：多 tab feed === -->
  <div id="hl-right">
    <div class="hl-feed-tabs">
      <button class="hl-feed-tab active"
              onclick="switchFeed('events')">鲸鱼动作</button>
      <button class="hl-feed-tab"
              onclick="switchFeed('whales')">地址画像</button>
      <button class="hl-feed-tab"
              onclick="switchFeed('consensus')">共识/背离</button>
      <button class="hl-feed-tab"
              onclick="switchFeed('walls')">挂单墙</button>
      <button class="hl-feed-tab"
              onclick="switchFeed('onchain')">链上</button>
    </div>
    <div id="hl-feed-body"></div>
  </div>
</div>

<div id="refresh-bar">
  5s 自动刷新 · 数据源：/api/state（HL 主动流 + 地址画像 + 鲸鱼信号）
</div>

<script>
const S = __INITIAL_STATE__;
let _state = S;
let _curFeed = 'events';
let _selectedCoin = '';  // 当前选中币（刷新后保持高亮）
let _hlSearchQ = '';     // 左面板搜索关键词
let _hlFilter = 'all';   // 左面板过滤：all/buy/sell

// ---- 工具函数（零伪造，确定性）----
function fmtUsd(v){{
  if(v==null)return'—';
  const n=parseFloat(v);if(isNaN(n))return'—';
  const a=Math.abs(n);
  let s;
  if(a>=1e9)s=(n/1e9).toFixed(2)+'B';
  else if(a>=1e6)s=(n/1e6).toFixed(2)+'M';
  else if(a>=1e3)s=(n/1e3).toFixed(1)+'K';
  else s=n.toFixed(2);
  return(n>=0?'$':'−$')+s.replace('-','');
}}
function fmtN(v,d){{
  if(v==null)return'—';const n=parseFloat(v);
  return isNaN(n)?'—':n.toFixed(d!=null?d:2);
}}
function fmtPct(v){{
  if(v==null)return'—';
  return(parseFloat(v)*100).toFixed(1)+'%';
}}
function fmtTime(ms){{
  if(!ms)return'—';
  const d=new Date(ms);
  return d.toLocaleTimeString('zh-CN',{{hour12:false}});
}}
function esc(s){{
  return String(s==null?'':s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
function dirTag(d){{
  if(d==='long'||d==='bullish')
    return'<span class="tag tag-long">看多</span>';
  if(d==='short'||d==='bearish')
    return'<span class="tag tag-short">看空</span>';
  return esc(d||'—');
}}

// ---- 时钟 ----
function tickClock(){{
  document.getElementById('hdr-clock').textContent=
    new Date().toLocaleTimeString('zh-CN',{{hour12:false}});
}}
setInterval(tickClock,1000);tickClock();

// ---- KPI strip ----
function renderKpi(s){{
  const wf=s.whale_flows||[];
  const ta=s.top_addresses||[];
  const cl=s.clusters||[];
  const ws=s.whale_signals||[];
  const netBuy=wf.reduce((a,r)=>a+(parseFloat(r.net)||0),0);
  document.getElementById('kpi-addrs').textContent=ta.length||'—';
  document.getElementById('kpi-coins').textContent=wf.length||'—';
  document.getElementById('kpi-clusters').textContent=cl.length||'—';
  document.getElementById('kpi-netbuy').textContent=fmtUsd(netBuy);
  document.getElementById('kpi-wsigs').textContent=ws.length||'—';
  const gen=s.meta&&s.meta.generated?s.meta.generated:'—';
  document.getElementById('kpi-time').textContent=
    gen.length>16?gen.slice(11,16):gen;
  document.getElementById('meta').textContent=
    '生成: '+(s.meta&&s.meta.generated?s.meta.generated:'—');
}}

// ---- 左面板搜索/过滤 ----
function hlOnSearch(q){{
  _hlSearchQ=q.trim().toLowerCase();
  renderCoinList(_state);
}}
function hlSetFilter(f){{
  _hlFilter=f;
  document.querySelectorAll('.hl-filter-btn').forEach(b=>{{
    b.classList.toggle('active',b.dataset.f===f);
  }});
  renderCoinList(_state);
}}

// ---- 左面板：币种列表（whale_flows + oi_surges 交叉）----
function renderCoinList(s){{
  const wf=s.whale_flows||[];
  const oi=s.oi_surges||[];
  // oi_surges symbol 如 "BONKUSDT" → coin "BONK"
  const oiMap={{}};
  oi.forEach(r=>{{
    const sym=(r.symbol||'').replace(/USDT.*$/,'').replace(/PERP.*$/,'');
    oiMap[sym]={{funding:r.funding,oi_size:r.oi_size}};
  }});
  // 过滤：搜索 + buy/sell 过滤
  let filtered=[...wf];
  if(_hlSearchQ)filtered=filtered.filter(r=>(r.coin||'').toLowerCase().includes(_hlSearchQ));
  if(_hlFilter==='buy')filtered=filtered.filter(r=>(parseFloat(r.net)||0)>0);
  else if(_hlFilter==='sell')filtered=filtered.filter(r=>(parseFloat(r.net)||0)<0);
  // 更新计数
  const countEl=document.getElementById('hl-left-count');
  if(countEl)countEl.textContent=filtered.length+'币';
  // 按 abs(net) 降序
  const sorted=filtered.sort((a,b)=>
    Math.abs(b.net||0)-Math.abs(a.net||0));
  const el=document.getElementById('hl-coin-list');
  if(!sorted.length){{
    el.innerHTML='<div style="padding:24px;text-align:center;'
      +'color:var(--t3);font-size:12px">暂无流向数据</div>';
    return;
  }}
  const maxAbs=Math.max(...sorted.map(r=>Math.abs(r.net||0)),1);
  el.innerHTML=sorted.map(r=>{{
    const net=parseFloat(r.net)||0;
    const isPos=net>=0;
    const barColor=isPos?'var(--long)':'var(--short)';
    const w=Math.round(Math.abs(net)/maxAbs*50);
    const barL=isPos?50:50-w;
    const info=oiMap[r.coin]||{{}};
    const fundingVal=info.funding!=null
      ?(parseFloat(info.funding)*100).toFixed(4)+'%':'—';
    const oiStr=fmtUsd(info.oi_size!=null?info.oi_size:null);
    const sel=r.coin===_selectedCoin?' selected':'';
    return '<div class="hl-coin-row'+sel+'" onclick="selectCoin(\\''+esc(r.coin)+'\\')">'
      +'<div class="hl-coin-row-top">'
      +'<span class="hl-coin-name">'+esc(r.coin)+'</span>'
      +'<span class="hl-coin-price mono" style="color:'+barColor+'">'
      +fmtUsd(net)+'</span>'
      +'</div>'
      +'<div class="hl-flow-bar-wrap">'
      +'<div class="hl-flow-track">'
      +'<div style="position:absolute;left:50%;top:0;height:100%;'
      +'width:1px;background:var(--t3);opacity:.4"></div>'
      +'<div class="hl-flow-fill" style="left:'+barL+'%;width:'+w+'%;'
      +'background:'+barColor+'"></div>'
      +'</div>'
      +'<span class="hl-flow-label mono" style="color:'+barColor+'">'
      +fmtUsd(net)+'</span>'
      +'</div>'
      +'<div class="hl-coin-bot">'
      +'<span>资金费 '+esc(fundingVal)+'</span>'
      +'<span>OI '+oiStr+'</span>'
      +'</div>'
      +'</div>';
  }}).join('');
}}

// ---- 中栏：净主动流向 SVG bars（whale_flows 数据，确定性）----
// SVG token（与 :root CSS 变量值保持一致）
const HL_T = {{long:'#16a34a', short:'#e23744', blue:'#2563eb', t3:'#9aa7bd', line:'#e4eaf3'}};
function renderFlowSvg(s){{
  const wf=s.whale_flows||[];
  if(!wf.length)return;
  const sorted=[...wf].sort((a,b)=>
    Math.abs(b.net||0)-Math.abs(a.net||0)).slice(0,40);
  const maxAbs=Math.max(...sorted.map(r=>Math.abs(r.net||0)),1);
  const n=sorted.length;
  const W=800,H=150,mid=75;
  const bw=Math.max(2,Math.floor((W-12)/n)-2);
  const gap=(W-12-bw*n)/(n+1);
  let barsSvg='';
  let cumNet=0;
  const pts=[];
  sorted.forEach((r,i)=>{{
    const net=parseFloat(r.net)||0;
    cumNet+=net;
    const norm=net/maxAbs;
    const bh=Math.max(2,Math.round(Math.abs(norm)*(H/2-8)));
    const x=6+gap+(bw+gap)*i;
    const y=net>=0?mid-bh:mid;
    const col=net>=0?HL_T.long:HL_T.short;
    barsSvg+='<rect x="'+x+'" y="'+y+'" width="'+bw+'" height="'+bh
      +'" rx="1.5" fill="'+col+'" opacity="0.85"/>';
    const cumNorm=cumNet/maxAbs/n*0.4;
    const cy=mid-Math.max(-0.9,Math.min(0.9,cumNorm))*(H/2-12);
    pts.push((i===0?'M':'L')+(x+bw/2)+','+cy);
  }});
  document.getElementById('flow-bars').innerHTML=barsSvg;
  document.getElementById('flow-cum-path').setAttribute('d',pts.join(' '));
  document.getElementById('flow-cum-label').textContent=
    '累计净流向 '+fmtUsd(cumNet);
}}

// ---- 中栏：地址排行（top_addresses）----
function renderAddrTable(s){{
  const ta=s.top_addresses||[];
  const tbody=document.getElementById('addr-tbody');
  const empty=document.getElementById('addr-empty');
  if(!ta.length){{
    tbody.innerHTML='';
    empty.style.display='';
    return;
  }}
  empty.style.display='none';
  tbody.innerHTML=ta.slice(0,12).map(r=>{{
    const bias=r.net_bias||'—';
    const biasColor=bias==='多'?'var(--long)':
      bias==='空'?'var(--short)':'var(--t2)';
    const addr=String(r.address||'').slice(0,12)+'…';
    const mpnl=parseFloat(r.month_pnl)||0;
    return '<tr>'
      +'<td class="addr">'+esc(addr)+'</td>'
      +'<td class="mono" style="color:var(--blue);font-weight:700">'
      +fmtN(r.score,1)+'</td>'
      +'<td class="mono">'+fmtUsd(r.account_value)+'</td>'
      +'<td class="mono" style="color:'+(mpnl>=0?'var(--long)':'var(--short)')+';">'
      +fmtUsd(r.month_pnl)+'</td>'
      +'<td class="mono">'+fmtPct(r.win_rate)+'</td>'
      +'<td style="color:'+biasColor+';font-weight:600">'+esc(bias)+'</td>'
      +'</tr>';
  }}).join('');
}}

// ---- 中栏：庄家集团（clusters）----
function renderClusters(s){{
  const cl=s.clusters||[];
  const el=document.getElementById('clusters-body');
  if(!cl.length){{
    el.innerHTML='<div style="color:var(--t3);font-size:12px;padding:8px">'
      +'暂无协同集团 — 等待地址协同检测入库</div>';
    return;
  }}
  el.innerHTML=cl.slice(0,6).map(c=>{{
    const members=(c.members||[])
      .map(m=>String(m).slice(0,10)+'…').join(' · ');
    const coinList=(c.coin_list||c.coins||[]);
    return '<div style="padding:8px 0;border-bottom:1px solid var(--line2)">'
      +'<div style="display:flex;align-items:center;'
      +'justify-content:space-between;gap:8px">'
      +'<span style="font-size:12px;font-weight:700">集团 ×'
      +(c.size||0)+' 地址</span>'
      +'<span style="font-size:10.5px;color:var(--blue)">跨 '
      +coinList.length+' 币</span>'
      +'</div>'
      +'<div class="mono" style="font-size:10px;color:var(--t2);margin-top:3px">'
      +esc(members)+'</div>'
      +'<div style="font-size:10px;color:var(--t3);margin-top:2px">'
      +'参与币种: '+esc(coinList.join(' / '))+'</div>'
      +'</div>';
  }}).join('');
}}

// ---- 右侧 feed 切换 ----
function switchFeed(tab){{
  _curFeed=tab;
  const tabs=['events','whales','consensus','walls','onchain'];
  document.querySelectorAll('.hl-feed-tab').forEach((b,i)=>{{
    b.classList.toggle('active',tabs[i]===tab);
  }});
  renderFeed(_state);
}}

function renderFeed(s){{
  const el=document.getElementById('hl-feed-body');
  if(_curFeed==='events')el.innerHTML=renderEvents(s);
  else if(_curFeed==='whales')el.innerHTML=renderWhales(s);
  else if(_curFeed==='consensus')el.innerHTML=renderConsensus(s);
  else if(_curFeed==='walls')el.innerHTML=renderWalls(s);
  else if(_curFeed==='onchain')el.innerHTML=renderOnchain(s);
}}

// 右侧：鲸鱼动作（whale_signals）
function renderEvents(s){{
  const ws=s.whale_signals||[];
  if(!ws.length)return'<div style="padding:24px;text-align:center;'
    +'color:var(--t3);font-size:12px">暂无鲸鱼动作</div>';
  return ws.slice(0,20).map(r=>{{
    const isL=r.direction==='long';
    const bg=isL?'var(--longbg)':'var(--shortbg)';
    const col=isL?'var(--long)':'var(--short)';
    return '<div class="hl-event-row">'
      +'<div class="hl-event-icon" style="background:'+bg+';color:'+col+'">'
      +'<span>'+esc(String(r.label||'🐋').split('(')[0])+'</span>'
      +'<span style="font-size:9px">'+(isL?'多':'空')+'</span>'
      +'</div>'
      +'<div class="hl-event-body">'
      +'<div style="display:flex;align-items:center;'
      +'justify-content:space-between">'
      +'<span class="hl-event-sym">'+esc(r.coin||'—')+'</span>'
      +'<span class="mono" style="font-size:11px;font-weight:600;color:'+col+'">'
      +fmtUsd(r.notional)+'</span>'
      +'</div>'
      +'<div class="mono hl-event-meta">'+esc(String(r.label||'—'))+'</div>'
      +'<div class="hl-event-bot">'
      +'<span>'+fmtTime(r.ts)+'</span>'
      +dirTag(r.direction)
      +'</div>'
      +'</div>'
      +'</div>';
  }}).join('');
}}

// 右侧：地址画像（top_addresses）
function renderWhales(s){{
  const ta=s.top_addresses||[];
  if(!ta.length)return'<div style="padding:24px;text-align:center;'
    +'color:var(--t3);font-size:12px">暂无地址画像数据</div>';
  return ta.slice(0,10).map((r,i)=>{{
    const addr=String(r.address||'').slice(0,14)+'…';
    const mpnl=parseFloat(r.month_pnl)||0;
    const dayColor=mpnl>=0?'var(--long)':'var(--short)';
    return '<div class="hl-whale-row">'
      +'<div class="hl-whale-top">'
      +'<div style="display:flex;align-items:center;gap:8px">'
      +'<div style="width:22px;height:22px;border-radius:6px;'
      +'background:var(--bluebg);display:flex;align-items:center;'
      +'justify-content:center;font-size:11px;font-weight:700;'
      +'color:var(--blue)">'+(i+1)+'</div>'
      +'<span class="mono" style="font-size:11.5px;font-weight:600">'
      +esc(addr)+'</span>'
      +'</div>'
      +'<span style="font-size:10px;font-weight:600;color:var(--blue)">'
      +fmtN(r.score,1)+'分</span>'
      +'</div>'
      +'<div class="hl-whale-grid">'
      +'<div class="hl-whale-kv"><span class="hl-whale-k">净值</span>'
      +'<span class="hl-whale-v">'+fmtUsd(r.account_value)+'</span></div>'
      +'<div class="hl-whale-kv"><span class="hl-whale-k">月PnL</span>'
      +'<span class="hl-whale-v" style="color:'+dayColor+'">'
      +fmtUsd(r.month_pnl)+'</span></div>'
      +'<div class="hl-whale-kv"><span class="hl-whale-k">胜率</span>'
      +'<span class="hl-whale-v">'+fmtPct(r.win_rate)+'</span></div>'
      +'<div class="hl-whale-kv"><span class="hl-whale-k">偏好</span>'
      +'<span class="hl-whale-v">'+esc(r.net_bias||'—')+'</span></div>'
      +'</div>'
      +'</div>';
  }}).join('');
}}

// 右侧：共识/背离（signals + divergence）
function renderConsensus(s){{
  const sigs=s.signals||[];
  const divs=s.divergence||[];
  let html='<div style="padding:12px 13px;display:flex;flex-direction:column;gap:14px">';
  html+='<div><div style="font-size:12px;font-weight:700;margin-bottom:8px;'
    +'color:var(--t2)">共振信号 · signals</div>';
  if(!sigs.length){{
    html+='<div style="font-size:11px;color:var(--t3)">暂无共振信号</div>';
  }}else{{
    html+=sigs.slice(0,8).map(r=>{{
      const isL=(r.direction||'').includes('long')
        ||(r.direction||'').includes('bull');
      const col=isL?'var(--long)':'var(--short)';
      const bg=isL?'var(--longbg)':'var(--shortbg)';
      return '<div class="hl-cons-row" style="display:flex;'
        +'align-items:center;gap:10px">'
        +'<div style="flex:1">'
        +'<div style="display:flex;align-items:center;gap:7px">'
        +'<span style="font-size:12.5px;font-weight:700">'+esc(r.coin||'—')+'</span>'
        +'<span class="tag" style="color:'+col+';background:'+bg+'">'
        +(isL?'看多':'看空')+'</span>'
        +'</div>'
        +'<span style="font-size:10.5px;color:var(--t2)">'+fmtTime(r.ts)+'</span>'
        +'</div>'
        +'<div style="text-align:right">'
        +'<div class="mono" style="font-size:13px;font-weight:700;color:'+col+'">'
        +fmtN(r.score,1)+'</div>'
        +'<div class="mono" style="font-size:10px;color:var(--t3)">'
        +'RR '+fmtN(r.rr,1)+'</div>'
        +'</div></div>';
    }}).join('');
  }}
  html+='</div>';
  html+='<div><div style="font-size:12px;font-weight:700;margin-bottom:8px;'
    +'color:var(--t2)">三源背离 · divergence</div>';
  if(!divs.length){{
    html+='<div style="font-size:11px;color:var(--t3)">暂无背离信号</div>';
  }}else{{
    html+=divs.slice(0,6).map(r=>{{
      const isB=(r.direction||'').includes('bull');
      const col=isB?'var(--long)':'var(--short)';
      const bg=isB?'var(--longbg)':'var(--shortbg)';
      return '<div class="hl-div-row" style="border-left-color:'+col+'">'
        +'<div style="display:flex;align-items:center;justify-content:space-between">'
        +'<span style="font-size:12.5px;font-weight:700">'+esc(r.coin||'—')+'</span>'
        +'<span class="tag" style="color:'+col+';background:'+bg+'">'
        +esc(r.direction||'—')+'</span>'
        +'</div>'
        +'<div style="font-size:10.5px;color:var(--t2);margin-top:4px">'
        +'资金费 <span style="font-weight:600">'+fmtN(r.funding,4)+'</span>'
        +' · 评分 '+fmtN(r.score,1)
        +'</div></div>';
    }}).join('');
  }}
  html+='</div></div>';
  return html;
}}

// 右侧：挂单墙（okx_walls）
function renderWalls(s){{
  const walls=s.okx_walls||[];
  if(!walls.length)return'<div style="padding:24px;text-align:center;'
    +'color:var(--t3);font-size:12px">暂无挂单墙数据</div>';
  return walls.slice(0,20).map(r=>{{
    const isBid=r.side==='bid';
    const col=isBid?'var(--long)':'var(--short)';
    const kindLbl=r.kind==='build'?'建仓':r.kind==='pull'?'撤单':esc(r.kind||'—');
    return '<div class="hl-wall-row">'
      +'<span style="width:36px;font-size:10px;font-weight:700;color:'+col+'">'
      +(isBid?'买墙':'卖墙')+'</span>'
      +'<span style="font-weight:700;min-width:60px">'+esc(r.coin||'—')+'</span>'
      +'<span class="mono" style="flex:1">'+fmtN(r.px,4)+'</span>'
      +'<span class="mono" style="color:'+col+'">'+fmtUsd(r.notional)+'</span>'
      +'<span style="font-size:10px;color:var(--t3);margin-left:6px">'
      +kindLbl+'</span>'
      +'</div>';
  }}).join('');
}}

// 右侧：链上大额转账（onchain）
function renderOnchain(s){{
  const oc=s.onchain||[];
  if(!oc.length)return'<div style="padding:24px;text-align:center;'
    +'color:var(--t3);font-size:12px">暂无链上大额转账数据</div>';
  return oc.slice(0,15).map(r=>{{
    return '<div style="padding:9px 13px;border-bottom:1px solid var(--line2)">'
      +'<div style="display:flex;align-items:center;'
      +'justify-content:space-between;gap:8px">'
      +'<div style="display:flex;align-items:center;gap:7px">'
      +'<span style="font-size:12px;font-weight:700">'+esc(r.coin||'—')+'</span>'
      +'<span style="font-size:9.5px;font-weight:600;color:var(--t2);'
      +'background:var(--bg);padding:1px 6px;border-radius:4px">'
      +esc(r.chain||'—')+'</span>'
      +'</div>'
      +'<span class="mono" style="font-size:11.5px;font-weight:700;color:var(--blue)">'
      +fmtUsd(r.amount_usd)+'</span>'
      +'</div>'
      +'<div class="mono" style="font-size:10px;color:var(--t2);margin-top:3px">'
      +fmtTime(r.ts)+'</div>'
      +'</div>';
  }}).join('');
}}

function selectCoin(coin){{
  _selectedCoin=coin;
  renderCoinList(_state);
}}

// ---- 主渲染 ----
function renderAll(s){{
  _state=s;
  renderKpi(s);
  renderCoinList(s);
  renderFlowSvg(s);
  renderAddrTable(s);
  renderClusters(s);
  renderFeed(s);
}}

// ---- 5s 轮询刷新 ----
async function refresh(){{
  try{{
    const r=await fetch('/api/state');
    if(r.ok)renderAll(await r.json());
  }}catch(e){{console.warn('hl2 refresh err',e)}}
}}
setInterval(refresh,5000);

// 首屏渲染
renderAll(S);
</script>
</body>
</html>"""


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
