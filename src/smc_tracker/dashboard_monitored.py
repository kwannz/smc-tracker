"""监控清单管理 dashboard 面板（扁平模块，从 dashboard.py 巨文件迁出）。

设计（CLAUDE.md：模块化扁平 + ≤800 行）：
  - apply_monitored_action：纯逻辑（add/rm/list → JSON 态），可单测。
  - render_monitored_page：自包含迷你页（无 CDN，独立于主页 {{/}} 转义模板，括号即字面量安全）。
  - register(app, store)：把 GET /monitored + GET/POST /api/monitored 挂到 dashboard app，
    dashboard.py 仅一行调用（巨文件零增长，逐个稀释）。
"""
from __future__ import annotations

import json
import time
from typing import Any

import aiohttp.web


def render_monitored_page() -> str:
    """监控清单管理迷你页（自包含 HTML，无 CDN）。前端 fetch /api/monitored 增删查，5s 无依赖。"""
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>监控币种清单</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#c9d1d9;margin:0;padding:24px}
 h1{font-size:18px} .row{display:flex;gap:8px;margin:12px 0;flex-wrap:wrap}
 input{background:#161b22;border:1px solid #30363d;color:#c9d1d9;padding:8px;border-radius:6px}
 button{background:#238636;border:0;color:#fff;padding:8px 14px;border-radius:6px;cursor:pointer}
 button.rm{background:#da3633} table{border-collapse:collapse;width:100%;margin-top:12px}
 td,th{border-bottom:1px solid #21262d;padding:6px 8px;text-align:left;font-size:13px}
 .note{color:#8b949e;font-size:12px}
</style></head><body>
<h1>监控币种清单 <span class="note">（enabled 时只采清单内币 7 周期；增删运行中热载入）</span></h1>
<div class="row">
 <input id="coins" placeholder="币种，空格/逗号分隔，如 BTC ETH SOL" size="40">
 <input id="note" placeholder="备注（可选）" size="20">
 <button onclick="doAdd()">加入</button>
</div>
<table id="tb"><thead><tr><th>币</th><th>symbol</th><th>备注</th><th></th></tr></thead><tbody></tbody></table>
<script>
async function load(){
 const r=await fetch('/api/monitored'); const j=await r.json();
 const tb=document.querySelector('#tb tbody'); tb.innerHTML='';
 (j.monitored||[]).forEach(m=>{
  const tr=document.createElement('tr');
  tr.innerHTML='<td>'+m.coin+'</td><td>'+m.symbol+'</td><td class="note">'+(m.note||'')+'</td>';
  const td=document.createElement('td'); const b=document.createElement('button');
  b.className='rm'; b.textContent='移除'; b.onclick=()=>doRm(m.coin); td.appendChild(b); tr.appendChild(td);
  tb.appendChild(tr);
 });
}
async function post(body){await fetch('/api/monitored',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await load();}
function doAdd(){const cs=document.getElementById('coins').value.split(/[\\s,]+/).filter(Boolean);const note=document.getElementById('note').value;if(cs.length)post({action:'add',coins:cs,note:note});document.getElementById('coins').value='';}
function doRm(c){post({action:'rm',coins:[c]});}
load(); setInterval(load,5000);
</script></body></html>"""


def apply_monitored_action(
    store: Any, action: str, coins: list[str], note: str, now_ms: int,
) -> dict:
    """监控清单 API 纯逻辑：执行 add/rm/list，返回 {"monitored": rows, "changed": n}。

    coins 统一大写归一；add 用 coin+'USDT' 作 symbol。可单测，不碰 HTTP。
    """
    changed = 0
    cs = [c.upper() for c in (coins or []) if c]
    if action == "add" and cs:
        store.add_monitored_coins([(c, f"{c}USDT", now_ms, note or "") for c in cs])
        changed = len(cs)
    elif action == "rm" and cs:
        changed = store.remove_monitored_coins(cs)
    rows = [
        {"coin": coin, "symbol": sym, "added_ts": ts, "note": n}
        for coin, sym, ts, n in store.list_monitored_coins()
    ]
    return {"monitored": rows, "changed": changed}


def register(app: aiohttp.web.Application, store: Any) -> None:
    """把监控清单面板路由挂到 dashboard app（dashboard.py 一行调用，巨文件零增长）。"""
    async def handle_page(_req: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text=render_monitored_page(), content_type="text/html")

    async def handle_api(request: aiohttp.web.Request) -> aiohttp.web.Response:
        now = int(time.time() * 1000)
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            action = str(body.get("action") or "list")
            coins = body.get("coins") or []
            if isinstance(coins, str):
                coins = [coins]
            note = str(body.get("note") or "")
        else:
            action, coins, note = "list", [], ""
        try:
            result = apply_monitored_action(store, action, list(coins), note, now)
            return aiohttp.web.json_response(result, dumps=lambda o: json.dumps(o, default=str))
        except Exception as exc:  # noqa: BLE001
            return aiohttp.web.json_response({"monitored": [], "error": str(exc)}, status=500)

    app.router.add_get("/monitored", handle_page)
    app.router.add_get("/api/monitored", handle_api)
    app.router.add_post("/api/monitored", handle_api)
