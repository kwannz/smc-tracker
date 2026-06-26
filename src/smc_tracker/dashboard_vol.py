"""波动追踪 dashboard 面板（扁平模块，独立于 dashboard.py 巨文件）。

设计（CLAUDE.md：模块化扁平 + ≤800 行 + 复用）：
  - 复用 monitor.VolatilityMonitor（逐周期 速度/加速度/σ/ATR/PD），不重造指标。
  - volatility_state：纯逻辑（store + 币集 → JSON 态），可单测。
  - render_volatility_page：自包含迷你页（无 CDN），fetch /api/volatility 逐周期渲染矩阵。
  - register(app, store)：把 GET /volatility + /api/volatility 路由挂到 dashboard app，
    dashboard.py 仅一行调用（巨文件零增长）。
"""
from __future__ import annotations

import json
import time
from typing import Any

import aiohttp.web

from .config import CANONICAL_TIMEFRAMES
from .monitor.volatility_monitor import VolatilityMonitor, volatility_highlights, market_regime


def pick_coins(store: Any) -> dict[str, str]:
    """选展示币集：优先监控清单；空则回退 DB 中已采 K 线的币（默认模式也可用）。"""
    coins = store.get_monitored_coins()
    if coins:
        return coins
    try:
        rows = store.conn.execute(
            "SELECT DISTINCT coin FROM bitget_candles LIMIT 50"
        ).fetchall()
        return {r[0]: f"{r[0]}USDT" for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def volatility_state(
    store: Any, coins: dict[str, str], timeframes: list[str],
    now_ms: int, top: int = 30,
) -> dict:
    """构建波动面板 JSON 态：{tfs, coins:[{coin,score,by_tf:{tf:metrics}}]}。纯逻辑可测。"""
    mon = VolatilityMonitor(coins, timeframes, store)
    rows = mon.rank(now_ms)[:top]
    return {"tfs": list(timeframes), "coins": rows,
            "market": market_regime(rows), "highlights": volatility_highlights(rows)}


def render_volatility_page() -> str:
    """波动追踪迷你页（自包含 HTML，无 CDN）。矩阵：行=币，列=周期，格=速度/PD。"""
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>实时波动追踪</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#c9d1d9;margin:0;padding:20px}
 h1{font-size:17px} table{border-collapse:collapse;width:100%;margin-top:10px;font-size:12px}
 td,th{border:1px solid #21262d;padding:4px 6px;text-align:center;white-space:nowrap}
 th{background:#161b22;position:sticky;top:0} td.coin{text-align:left;font-weight:600}
 .up{color:#3fb950} .dn{color:#f85149} .prem{background:#3a1d1d} .disc{background:#16301d} .eq{color:#8b949e}
 .note{color:#8b949e;font-size:12px}
</style></head><body>
<h1>🌀 实时波动追踪 <span class="note">逐周期 速度·PD溢价折价·波动状态·HVP（绿=折价/买区 红=溢价/卖区；🔸压缩 🔶扩张；HVP=波动率历史百分位 🔥≥90%异常剧烈 ❄️≤10%极静蓄势；期限结构(√t归一,回望非预测) ⏫倒挂=近端波动相对急 ⏬顺挂=远端主导）</span></h1>
<div id="hl" class="note"></div>
<div id="box" class="note">加载中…</div>
<script>
function hlbar(h){
 if(!h) return '';
 var s='';
 if(h.squeeze&&h.squeeze.length) s+='🔸蓄势: '+h.squeeze.map(function(x){return x.coin+'/'+x.tf;}).join(' ')+'　';
 if(h.expansion&&h.expansion.length) s+='🔶放量: '+h.expansion.map(function(x){return x.coin+'/'+x.tf+'('+(x.velocity>=0?'+':'')+x.velocity.toFixed(1)+'%)';}).join(' ')+'　';
 if(h.extreme_pd&&h.extreme_pd.length) s+='⚡极端PD: '+h.extreme_pd.map(function(x){return x.coin+'/'+x.tf+'('+x.pd_zone+Math.round(x.pd_pct*100)+'%)';}).join(' ');
 return s;
}
function cell(m){
 if(!m) return '<td class="eq">—</td>';
 var v=m.velocity, cls=v>=0?'up':'dn', arr=v>=0?'↑':'↓';
 var z=m.pd_zone, zc=z==='溢价'?'prem':(z==='折价'?'disc':'eq');
 var rg=m.regime, rs=rg==='压缩'?'🔸':(rg==='扩张'?'🔶':'');
 var vp=(m.vol_pct>=0)?((m.vol_pct>=0.9?'🔥':(m.vol_pct<=0.1?'❄️':''))+'HVP'+Math.round(m.vol_pct*100)+'%'):'';
 return '<td class="'+zc+'"><span class="'+cls+'">'+arr+Math.abs(v).toFixed(1)+'%</span>'+rs
   +'<br>PD'+Math.round(m.pd_pct*100)+'%<br><small class="note">'+vp+'</small></td>';
}
async function load(){
 var r=await fetch('/api/volatility'); var j=await r.json();
 var tfs=j.tfs||[], coins=j.coins||[];
 var fresh=Math.max.apply(null,[0].concat(coins.map(function(c){return c.last_ms||0;})));
 var fr='';
 if(fresh>0){var ageMin=Math.round((Date.now()-fresh)/60000);
   fr='🕒 数据更新至 '+new Date(fresh).toLocaleString()+(ageMin>30?' ⚠️陈旧'+ageMin+'分钟':'')+'<br>';}
 var mkt=(j.market&&j.market.label)?'📊 市场态势: '+j.market.label+'<br>':'';
 document.getElementById('hl').innerHTML=fr+mkt+hlbar(j.highlights);
 if(!coins.length){document.getElementById('box').textContent='暂无数据（监控清单为空或采集器未填 K 线）';return;}
 var h='<table><thead><tr><th>币</th><th>分</th>';
 tfs.forEach(function(t){h+='<th>'+t+'</th>';}); h+='</tr></thead><tbody>';
 coins.forEach(function(c){
  var al=c.align||{bias:'分歧',aligned:0,total:0};
  var bm=al.bias==='多'?'<span class="up">🟢多</span>':(al.bias==='空'?'<span class="dn">🔴空</span>':'⚪');
  var st=c.state?(' '+c.state):'';
  var tsh=(c.term&&c.term.shape)||'';
  var ts=tsh==='倒挂'?' <span class="dn">⏫倒挂</span>':(tsh==='顺挂'?' <span class="up">⏬顺挂</span>':'');
  h+='<tr><td class="coin">'+c.coin+st+'<br><small>'+bm+' '+al.aligned+'/'+al.total+ts+'</small></td><td>'+c.score.toFixed(1)+'</td>';
  tfs.forEach(function(t){h+=cell(c.by_tf[t]);}); h+='</tr>';
 });
 document.getElementById('box').innerHTML=h+'</tbody></table>';
}
load(); setInterval(load,5000);
</script></body></html>"""


def register(app: aiohttp.web.Application, store: Any) -> None:
    """把波动面板路由挂到 dashboard app（dashboard.py 一行调用，巨文件零增长）。"""
    async def handle_page(_req: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text=render_volatility_page(), content_type="text/html")

    async def handle_api(_req: aiohttp.web.Request) -> aiohttp.web.Response:
        now = int(time.time() * 1000)
        try:
            st = volatility_state(store, pick_coins(store), list(CANONICAL_TIMEFRAMES), now)
            return aiohttp.web.json_response(st, dumps=lambda o: json.dumps(o, default=str))
        except Exception as exc:  # noqa: BLE001
            return aiohttp.web.json_response({"tfs": [], "coins": [], "error": str(exc)}, status=500)

    app.router.add_get("/volatility", handle_page)
    app.router.add_get("/api/volatility", handle_api)
