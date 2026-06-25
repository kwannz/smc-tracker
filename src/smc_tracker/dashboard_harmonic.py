"""谐波渲染层（扁平模块，从 dashboard.py 迁出）。

提供两个自包含 HTML 渲染函数：
  render_harmonic_html(state)            → str
  render_harmonic_detail_html(list_state) → str

HTML 模板已外置到 templates/ 目录（模块导入时一次性读入并缓存），
render 函数沿用 {{/}} 转义 + __INITIAL_STATE__ 注入模式。
"""
from __future__ import annotations

import json
from pathlib import Path

# 模板目录：模块导入时读一次并缓存（深色谐波 Setup 页 / 谐波主-详情 SPA）
_TPL_DIR = Path(__file__).parent / "templates"
_HARMONIC_HTML_TEMPLATE = (_TPL_DIR / "harmonic_list.html").read_text(encoding="utf-8")
_HARMONIC_DETAIL_TEMPLATE = (_TPL_DIR / "harmonic_detail.html").read_text(encoding="utf-8")


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
