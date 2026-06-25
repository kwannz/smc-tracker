"""dashboard_nav 导航页单测（纯 HTML 逻辑，无 HTTP）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.dashboard_nav import render_nav_page  # noqa: E402


def test_nav_lists_all_panels():
    """render_nav_page 输出包含全部主要面板入口路径。"""
    html = render_nav_page()
    for panel in ("/volatility", "/monitored", "/signals", "/harmonic"):
        assert panel in html, f"导航页缺少面板入口: {panel}"


def test_nav_self_contained():
    """导航页不含任何外部链接（无 http:// / https://），保持自包含。"""
    html = render_nav_page()
    assert "http://" not in html and "https://" not in html, (
        "导航页含外部链接，违反自包含规范"
    )
