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

    # ---- 钱包持仓画像（watched_wallets + wallet_positions_full）----
    wallet_portfolio: list[dict] = []
    try:
        ww_rows = _safe_rows(
            conn,
            "SELECT address,label,source,first_seen_ms,last_seen_ms,"
            "account_value,total_ntl_pos,n_positions FROM watched_wallets "
            "ORDER BY account_value DESC NULLS LAST",
        )
        for ww in ww_rows:
            # ww: (address,label,source,first_seen_ms,last_seen_ms,
            #       account_value,total_ntl_pos,n_positions)
            addr = ww[0]
            pos_rows = _safe_rows(
                conn,
                "SELECT coin,direction,szi,entry_px,position_value,"
                "unrealized_pnl,leverage,liquidation_px,open_ms,last_close_ms,hold_sec "
                "FROM wallet_positions_full "
                "WHERE address=? AND ts=(SELECT MAX(ts) FROM wallet_positions_full WHERE address=?) "
                "ORDER BY ABS(position_value) DESC LIMIT 50",
                (addr, addr),
            )
            positions = [
                {
                    "coin": r[0],
                    "direction": r[1],
                    "position_value": r[4],
                    "entry_px": r[3],
                    "unrealized_pnl": r[5],
                    "leverage": r[6],
                    "liquidation_px": r[7],
                    "open_ms": r[8] if len(r) > 8 else None,
                    "last_close_ms": r[9] if len(r) > 9 else None,
                    "hold_sec": r[10] if len(r) > 10 else None,
                }
                for r in pos_rows
            ]
            wallet_portfolio.append({
                "address": addr,
                "label": ww[1] or "",
                "source": ww[2] or "",
                "account_value": ww[5],
                "total_ntl_pos": ww[6],
                "n_positions": ww[7] or 0,
                "positions": positions,
            })
    except Exception:  # noqa: BLE001 — 表不存在/结构不对时返回 []
        wallet_portfolio = []

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
  return h+'</table>';
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

function renderTickerBoard(rows){{
  if(!rows||!rows.length)return none();
  let h='<table><tr><th>币种</th><th>价格</th><th>涨跌幅(24h)</th><th>资金费率</th><th>OI(USD)</th></tr>';
  rows.forEach(r=>{{
    // 涨跌幅：chg24 为 null 时显示「—」
    let chgStr='<span class="none">—</span>';
    if(r.chg24!=null){{
      const pct=(parseFloat(r.chg24)*100).toFixed(2);
      const cls=parseFloat(r.chg24)>=0?'pos':'neg';
      const sign=parseFloat(r.chg24)>=0?'+':'';
      chgStr=`<span class="${{cls}}">${{sign}}${{pct}}%</span>`;
    }}
    // 资金费率
    const fundingPct=(parseFloat(r.funding||0)*100).toFixed(4);
    const fundingSign=parseFloat(r.funding||0)>=0?'+':'';
    const fundingCls=parseFloat(r.funding||0)>=0?'pos':'neg';
    h+=`<tr>
      <td class="coin">${{r.coin||r.symbol||''}}</td>
      <td>${{fmtNum(r.price,6)}}</td>
      <td>${{chgStr}}</td>
      <td class="${{fundingCls}}">${{fundingSign}}${{fundingPct}}%</td>
      <td>${{fmtUsd(r.oi_usd)}}</td>
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
    ['📊 行情监控板','ticker_board',renderTickerBoard],
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

    app = aiohttp.web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_get("/health", handle_health)

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


# 兼容直接 import asyncio
import asyncio  # noqa: E402
