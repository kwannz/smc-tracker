"""Dashboard 导航页（扁平模块）：列出全部面板入口，解决可发现性。register(app) 挂 GET /nav。"""
from __future__ import annotations

import aiohttp.web

# 全部面板入口链接（路径 + 标签）
_LINKS = [
    ("/", "主页 总览"),
    ("/volatility", "🌀 实时波动追踪（逐周期 速度/PD/regime + 动向摘要）"),
    ("/harmonic", "谐波形态"),
    ("/signals", "全信号总览"),
    ("/monitored", "监控币种清单（增删）"),
    ("/hl2", "HL 抓庄系统"),
]


def render_nav_page() -> str:
    """渲染导航页 HTML（自包含，无 CDN，无外部链接）。"""
    items = "".join(
        f'<li><a href="{href}">{label}</a></li>' for href, label in _LINKS
    )
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>导航</title>'
        "<style>body{font-family:-apple-system,Segoe UI,sans-serif;background:#0d1117;color:#c9d1d9;"
        "margin:0;padding:32px}h1{font-size:18px}li{margin:10px 0;font-size:15px}"
        "a{color:#58a6ff;text-decoration:none}a:hover{text-decoration:underline}</style></head><body>"
        "<h1>SMC 抓庄系统 · 面板导航</h1><ul>" + items + "</ul></body></html>"
    )


def register(app: aiohttp.web.Application) -> None:
    """将 GET /nav 路由注册到 aiohttp app。"""
    async def handle_nav(_req: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text=render_nav_page(), content_type="text/html")

    app.router.add_get("/nav", handle_nav)
