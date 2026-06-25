"""全信号总览 dashboard 面板（扁平模块，从 dashboard.py 迁出）。

register(app, store) 挂 GET /signals + GET /api/signals。
build_all_signals_state / render_all_signals_html 原样复制自 dashboard.py（T3 阶段只新建不删原）。
"""
from __future__ import annotations

import json
import time
from typing import Any

import aiohttp.web


# ---------------------------------------------------------------------------
# 信号总览页 —— 原样复制自 dashboard.py（逐字，勿改逻辑）
# ---------------------------------------------------------------------------

def build_all_signals_state(store: Any, now_ms: int, hours: float = 1.0) -> dict:
    """聚合 11 张信号表，返回统一结构 dict，供 /signals 页和 /api/signals 使用。

    Args:
        store:   Store 实例
        now_ms:  当前时间戳 ms
        hours:   时间窗口（小时），默认 1h

    Returns:
        {
            signals_list: list[dict]  # 按 ts 倒序，统一字段
            by_type:      dict        # type → 条数（分组计数）
            meta:         dict        # generated / window_hours
        }
    """
    from .signals.all_signals import collect_all_signals

    since_ms = int(now_ms - hours * 3_600_000)
    gen_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ms / 1000))

    try:
        signals_list = collect_all_signals(store, since_ms, now_ms)
    except Exception:  # noqa: BLE001
        signals_list = []

    # 按 type 分组计数
    by_type: dict[str, int] = {}
    for row in signals_list:
        t = row.get("type") or "unknown"
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "signals_list": signals_list,
        "by_type": by_type,
        "meta": {
            "generated": gen_str,
            "window_hours": hours,
        },
    }


# 信号总览页 HTML 模板（深色主题，与主 dashboard 风格一致，无 CDN）
_SIGNALS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>信号总览 · SMC 抓庄监控</title>
<style>
:root{{
  --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;
  --muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;
  --yellow:#e3b341;--purple:#bc8cff;--orange:#ffa657;
  --card-shadow:0 1px 4px rgba(0,0,0,.35);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);
  font-family:'Inter',system-ui,-apple-system,sans-serif;
  font-size:13px;line-height:1.5}}
.mono{{font-family:"SF Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}}
header{{padding:14px 24px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
h1{{font-size:19px;color:var(--blue);font-weight:700}}
#meta{{color:var(--muted);font-size:12px}}
.hdr-nav{{display:flex;gap:6px;margin-left:auto}}
.hdr-nav a{{font-size:11.5px;color:var(--muted);text-decoration:none;
  border:1px solid var(--border);border-radius:5px;padding:3px 10px;
  transition:color .15s,border-color .15s}}
.hdr-nav a:hover{{color:var(--blue);border-color:var(--blue)}}
.disclaimer{{margin:10px 16px 0;padding:8px 14px;background:#1c1a10;
  border:1px solid #5a4a00;border-radius:6px;color:var(--yellow);font-size:12px}}
main{{padding:16px;display:flex;flex-direction:column;gap:16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;
  overflow:hidden;box-shadow:var(--card-shadow)}}
.card-title{{padding:10px 14px;border-bottom:1px solid var(--border);
  font-weight:700;color:var(--blue);font-size:13px;
  display:flex;align-items:center;justify-content:space-between}}
.card-count{{font-size:11px;color:var(--muted);font-weight:400}}
.card-body{{padding:12px 14px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{color:var(--muted);font-weight:600;text-align:left;
  padding:4px 6px;border-bottom:1px solid var(--border)}}
td{{padding:3px 6px;vertical-align:top;white-space:nowrap}}
tr:hover td{{background:rgba(255,255,255,.04)}}
.long{{color:var(--green)}} .short{{color:var(--red)}}
.bullish{{color:var(--green)}} .bearish{{color:var(--red)}}
.none{{color:var(--muted);font-style:italic}}
.tag{{display:inline-block;padding:1px 5px;border-radius:4px;font-size:11px;font-weight:600}}
.tag-long{{background:#1a3a2a;color:var(--green)}}
.tag-short{{background:#3a1a1a;color:var(--red)}}
.coin{{color:var(--orange);font-weight:600}}
.score{{color:var(--yellow)}}
.ev{{color:var(--muted);max-width:280px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;font-size:11px}}
#refresh-bar{{font-size:11px;color:var(--muted);
  padding:4px 24px;border-top:1px solid var(--border)}}
.type-badge{{display:inline-block;padding:1px 6px;border-radius:4px;
  font-size:10px;font-weight:700;background:#1a2840;color:var(--blue)}}
</style>
</head>
<body>
<header>
  <h1>📡 信号总览</h1>
  <span id="meta">加载中…</span>
  <nav class="hdr-nav">
    <a href="/">主页</a>
    <a href="/hl2">HL 系统</a>
    <a href="/harmonic2">谐波系统</a>
  </nav>
</header>
<div class="disclaimer">
  ⚠️ <strong>诚实声明：1h≈随机基线，非投资建议。</strong>
  信号为技术分析辅助参考；历史 KNN/回测命中率无真实 alpha；
  止损/入场需自行确认成交量与订单流，不构成任何投资建议。
</div>
<main id="main"><!-- 由 JS renderAll() 填充 --></main>
<div id="refresh-bar">自动刷新 · 5 秒</div>
<script>
const S = __INITIAL_STATE__;

// ---------- 工具 ----------
function fmtTime(ms){{
  if(!ms)return'--';
  return new Date(ms).toLocaleTimeString('zh-CN',{{hour12:false}});
}}
function fmtNum(v,dec){{
  if(v==null)return'--';
  const n=parseFloat(v);
  return isNaN(n)?'--':n.toFixed(dec!=null?dec:4);
}}
function dirTag(d){{
  if(!d)return'<span class="none">—</span>';
  const cls=(d==='long'||d==='bullish')?'tag-long':'tag-short';
  const lbl=(d==='long')?'做多':(d==='short')?'做空':(d==='bullish')?'吸筹↑':(d==='bearish')?'分销↓':d;
  return'<span class="tag '+cls+'">'+lbl+'</span>';
}}
function svgEsc(s){{
  return String(s==null?'':s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
function none(){{return'<span class="none">（无信号）</span>';}}

// ---------- 按 type 渲染信号行 ----------
function renderTypeGroup(typeLbl, rows){{
  if(!rows||!rows.length)return'';
  const count=rows.length;
  let h='<div class="card">'
    +'<div class="card-title">'+svgEsc(typeLbl)
    +'<span class="card-count">'+count+'条</span></div>'
    +'<div class="card-body">'
    +'<table><tr><th>时间</th><th>标的</th><th>方向</th>'
    +'<th>价格</th><th>评分</th><th>证据摘要</th></tr>';
  rows.forEach(r=>{{
    h+='<tr>'
      +'<td>'+fmtTime(r.ts)+'</td>'
      +'<td class="coin">'+svgEsc(r.coin||'')+'</td>'
      +'<td>'+dirTag(r.direction)+'</td>'
      +'<td class="mono">'+fmtNum(r.price,4)+'</td>'
      +'<td class="score">'+fmtNum(r.score,2)+'</td>'
      +'<td class="ev" title="'+svgEsc(r.evidence_text||'')+'">'+svgEsc(r.evidence_text||'—')+'</td>'
      +'</tr>';
  }});
  h+='</table></div></div>';
  return h;
}}

// ---------- 主渲染 ----------
// type 显示顺序（按重要性排列）
const TYPE_ORDER=[
  'confluence','consensus','signal','whale_signal',
  'position_change','divergence','flow_prediction',
  'okx_signal','harmonic_setup','orderbook_wall','flagged_address',
];
// type → 中文标签映射（兜底用 type 本身）
const TYPE_LABELS={{
  confluence:'超级共振',consensus:'共识',signal:'SMC共振',
  whale_signal:'跟庄',position_change:'换仓',divergence:'背离',
  flow_prediction:'前瞻资金流',okx_signal:'OKX信号',
  harmonic_setup:'谐波形态',orderbook_wall:'挂单墙',
  flagged_address:'可疑地址',
}};

function renderAll(state){{
  const m=state.meta||{{}};
  document.getElementById('meta').textContent=
    '生成于 '+(m.generated||'--')+'  ·  近 '+(m.window_hours||1)+'h';

  const lst=state.signals_list||[];
  if(!lst.length){{
    document.getElementById('main').innerHTML=
      '<div class="card"><div class="card-body">'+none()+'</div></div>';
    return;
  }}

  // 按 type 分组
  const byType={{}};
  lst.forEach(r=>{{
    const t=r.type||'unknown';
    if(!byType[t])byType[t]=[];
    byType[t].push(r);
  }});

  // 按 TYPE_ORDER 渲染，未在列表中的 type 排最后
  const seen=new Set();
  let html='';
  TYPE_ORDER.forEach(t=>{{
    if(byType[t]&&byType[t].length){{
      html+=renderTypeGroup(TYPE_LABELS[t]||t, byType[t]);
      seen.add(t);
    }}
  }});
  // 其余 type（未在 TYPE_ORDER 中）
  Object.keys(byType).forEach(t=>{{
    if(!seen.has(t))html+=renderTypeGroup(TYPE_LABELS[t]||t, byType[t]);
  }});

  document.getElementById('main').innerHTML=html;
}}

// ---------- 首屏 + 5s 自动刷新 ----------
renderAll(S);
async function refresh(){{
  try{{
    const r=await fetch('/api/signals');
    if(r.ok)renderAll(await r.json());
  }}catch(e){{console.warn('signals refresh err',e)}}
}}
setInterval(refresh,5000);
</script>
</body>
</html>"""


def render_all_signals_html(state: dict) -> str:
    """将 build_all_signals_state 的结果渲染为自包含单页 HTML。

    与 render_html / render_hl_html 使用相同的双括号转义模式：
    先将模板 {{→{ / }}→} 解转义，再注入 state JSON，JSON 自身的括号不受影响。
    """
    state_json = json.dumps(state, ensure_ascii=False, default=str)
    html = _SIGNALS_HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    return html.replace("__INITIAL_STATE__", state_json)


# ---------------------------------------------------------------------------
# aiohttp 路由注册
# ---------------------------------------------------------------------------

def register(app: aiohttp.web.Application, store: Any) -> None:
    """挂载 GET /signals（HTML）与 GET /api/signals（JSON）。"""

    async def handle_signals(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            hours = float(request.rel_url.query.get("hours") or 1.0)
        except (ValueError, TypeError):
            hours = 1.0
        now_ms = int(time.time() * 1000)
        state = build_all_signals_state(store, now_ms, hours=hours)
        return aiohttp.web.Response(text=render_all_signals_html(state), content_type="text/html")

    async def handle_api_signals(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            hours = float(request.rel_url.query.get("hours") or 1.0)
        except (ValueError, TypeError):
            hours = 1.0
        now_ms = int(time.time() * 1000)
        state = build_all_signals_state(store, now_ms, hours=hours)
        return aiohttp.web.json_response(state, dumps=lambda o: json.dumps(o, default=str))

    app.router.add_get("/signals", handle_signals)
    app.router.add_get("/api/signals", handle_api_signals)
