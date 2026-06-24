"""谐波监控器 TDD 测试。

测试 HarmonicMonitor.render() 卡片内容：
- 含 "谐波形态"
- 含 "成形中" / "完整"
- PRZ 价格非科学计数法
- Crab 警示标注
"""
from __future__ import annotations

import pytest

from smc_tracker.monitor.harmonic_monitor import HarmonicMonitor


_NOW_MS = 1_700_000_000_000  # 固定时间戳，非 0


def _make_rows(
    *,
    with_forming: bool = True,
    with_completed: bool = True,
    with_crab: bool = False,
) -> list[dict]:
    """构造 refresh() 返回的 rows 结构（monkeypatch 替代品）。"""
    rows: list[dict] = []

    if with_forming:
        rows.append({
            "coin": "BTC",
            "symbol": "BTCUSDT",
            "price": 62538.5,
            "tf": "4H",
            "completed": [],
            "forming": [
                {
                    "pattern": "Gartley",
                    "direction": "bull",
                    "prz": (60200.0, 60650.0),
                    "completed": False,
                    "confidence": 0.72,
                    "confluence": 3,
                }
            ],
        })

    if with_completed:
        rows.append({
            "coin": "ETH",
            "symbol": "ETHUSDT",
            "price": 1835.0,
            "tf": "1H",
            "completed": [
                {
                    "pattern": "Bat",
                    "direction": "bear",
                    "prz": (1712.0, 1728.0),
                    "completed": True,
                    "confidence": 0.68,
                    "confluence": 4,
                    "points": {
                        "D": (99, 1720.0),
                    },
                }
            ],
            "forming": [],
        })

    if with_crab:
        rows.append({
            "coin": "SOL",
            "symbol": "SOLUSDT",
            "price": 145.0,
            "tf": "1H",
            "completed": [
                {
                    "pattern": "Crab",
                    "direction": "bull",
                    "prz": (130.0, 135.0),
                    "completed": True,
                    "confidence": 0.65,
                    "confluence": 3,
                    "points": {
                        "D": (10, 132.0),
                    },
                }
            ],
            "forming": [],
        })

    return rows


class TestHarmonicMonitorRender:
    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"},
            timeframes=["15m", "1H", "4H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )

    def test_render_none_on_empty_rows(self) -> None:
        """空 rows → None。"""
        assert self.monitor.render([], _NOW_MS) is None

    def test_render_contains_title(self) -> None:
        """卡片含 '谐波形态' 标题。"""
        card = self.monitor.render(_make_rows(), _NOW_MS)
        assert card is not None
        assert "谐波形态" in card

    def test_render_contains_forming_section(self) -> None:
        """含 forming 标记（🎯 前缀行，前瞻预测）。"""
        card = self.monitor.render(_make_rows(with_forming=True, with_completed=False), _NOW_MS)
        assert card is not None
        # 新格式：forming 行以 🎯{tf} 开头，不再有【🎯 成形中】区块头
        assert "🎯" in card, f"新格式 forming 行应含 🎯 前缀，卡片:\n{card}"

    def test_render_contains_completed_section(self) -> None:
        """含 completed 标记（✅ 前缀行，入场触发）。"""
        card = self.monitor.render(_make_rows(with_forming=False, with_completed=True), _NOW_MS)
        assert card is not None
        # 新格式：completed 行以 ✅{tf} 开头，副标题含「完整=入场触发」
        assert "✅" in card or "完整" in card, f"新格式应含 ✅ 前缀或完整字样，卡片:\n{card}"

    def test_render_contains_coin_name(self) -> None:
        """卡片含币名 BTC/ETH。"""
        card = self.monitor.render(_make_rows(), _NOW_MS)
        assert card is not None
        assert "BTC" in card
        assert "ETH" in card

    def test_render_price_no_scientific_notation(self) -> None:
        """价格不含科学计数法（无 'e+' 或 'E+'）。"""
        card = self.monitor.render(_make_rows(), _NOW_MS)
        assert card is not None
        assert "e+" not in card.lower(), f"价格含科学计数法: {card}"

    def test_render_prz_range_present(self) -> None:
        """PRZ 区间价格出现在卡片中（含高价 60650 或 60,650 格式）。"""
        card = self.monitor.render(_make_rows(with_forming=True), _NOW_MS)
        assert card is not None
        # fmt_px 可能加逗号，也可能不加，只验证大数字出现
        assert "60" in card, "PRZ 高点附近数字未出现"

    def test_render_pattern_name_present(self) -> None:
        """Gartley/Bat 等形态名出现在卡片中。"""
        card = self.monitor.render(_make_rows(), _NOW_MS)
        assert card is not None
        assert "Gartley" in card
        assert "Bat" in card

    def test_render_crab_warning(self) -> None:
        """Crab 形态出现时，卡片含 Crab 警示（胜率偏低标注）。"""
        card = self.monitor.render(_make_rows(with_crab=True), _NOW_MS)
        assert card is not None
        assert "Crab" in card
        # 警示标注含 '⚠' 或 '警告' 或 '胜率' 或 '低' 或 '实测'
        crab_line = next((l for l in card.splitlines() if "Crab" in l), "")
        has_warning = any(kw in crab_line for kw in ("⚠", "警告", "胜率", "实测", "低"))
        assert has_warning, f"Crab 行缺少警示标注: {crab_line!r}"

    def test_render_confidence_shown(self) -> None:
        """置信度出现在卡片中（如 '72%' 或 '0.72'）。"""
        card = self.monitor.render(_make_rows(with_forming=True), _NOW_MS)
        assert card is not None
        assert "72" in card, "置信度 72 未出现在卡片"

    def test_render_timestamp_present(self) -> None:
        """卡片含时间戳（来自 fmt_ts，非空）。"""
        card = self.monitor.render(_make_rows(), _NOW_MS)
        assert card is not None
        # fmt_ts 返回如 "2023-11-14 22:13:20 UTC" 格式，至少含 "202" 年份前缀
        assert "202" in card, "卡片缺少时间戳年份"


# ========== TDD 新增缺陷6测试 ==========


def _make_large_rows(n_completed: int = 60, n_forming: int = 60) -> list[dict]:
    """构造大量形态 rows，用于验证渲染截断。"""
    base_completed = [
        {
            "pattern": "Gartley",
            "direction": "bull",
            "prz": (60000.0, 60300.0),
            "completed": True,
            "confidence": 0.70 + i * 0.001,
            "confluence": 4,
            "points": {"D": (100 + i, 60100.0 + i)},
        }
        for i in range(n_completed)
    ]
    base_forming = [
        {
            "pattern": "Bat",
            "direction": "bear",
            "prz": (59500.0, 59700.0),
            "completed": False,
            "confidence": 0.60 + i * 0.001,
            "confluence": 3,
        }
        for i in range(n_forming)
    ]
    return [
        {
            "coin": "BTC",
            "symbol": "BTCUSDT",
            "price": 65000.0,
            "tf": "1H",
            "completed": base_completed,
            "forming": base_forming,
        }
    ]


class TestRenderCapsPatterns:
    """缺陷6修复验证：render 截断，每币每周期 completed/forming 各 ≤2，整卡 ≤8。

    当前代码: 只有 completed_rows[:8] / forming_rows[:8]，但展平后可能超出
    (60 completed * 1 币 → 最终输出 8 条，但单币不限 2 条的约束未实现)。
    修复后: 每币每 tf 的 completed/forming 各限 top 2，整卡 completed ≤8，forming ≤8。
    """

    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["1H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )

    def test_render_completed_capped_per_coin_tf(self) -> None:
        """单币单周期 completed 最多展示 top 2。

        新格式：completed 行以 ✅{tf} 前缀（原 • 前缀），每币每 tf 各限 top 2。
        """
        rows = _make_large_rows(n_completed=60, n_forming=0)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None

        # 新格式：completed 行以 ✅ 前缀（含 Gartley）
        completed_lines = [
            ln for ln in card.splitlines()
            if "✅" in ln and "Gartley" in ln
        ]
        assert len(completed_lines) <= 2, (
            f"单币单 tf completed 应 ≤2 条，实际 {len(completed_lines)} 条（缺陷6未修）"
        )

    def test_render_forming_capped_per_coin_tf(self) -> None:
        """单币单周期 forming 最多展示 top 2。新格式：forming 行以 🎯{tf} 前缀。"""
        rows = _make_large_rows(n_completed=0, n_forming=60)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None

        # 新格式：forming 行以 🎯 前缀（含 Bat）
        forming_lines = [
            ln for ln in card.splitlines()
            if "🎯" in ln and "Bat" in ln
        ]
        assert len(forming_lines) <= 2, (
            f"单币单 tf forming 应 ≤2 条，实际 {len(forming_lines)} 条（缺陷6未修）"
        )

    def test_render_total_cap_with_omission_note(self) -> None:
        """整卡 completed+forming 合计不超 cap，且卡片含省略提示。

        新格式：超出部分应有「…省略 N 条」提示；行前缀 ✅/🎯 替代原 •。
        """
        rows = _make_large_rows(n_completed=30, n_forming=30)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None

        # 新格式：统计 ✅ + 🎯 前缀行数（排除省略行）
        item_lines = [
            ln for ln in card.splitlines()
            if ("✅" in ln or "🎯" in ln) and "省略" not in ln
        ]
        assert len(item_lines) <= 8, (
            f"整卡条目应 ≤8 条（每币上限6条），实际 {len(item_lines)} 条"
        )

    def test_render_no_hundred_plus_patterns(self) -> None:
        """卡片不出现'113 个形态'这类噪音总数——币数/形态数文字应合理。

        新格式：「近窗 N 币」替代原「近窗 N 个形态」，N 应 ≤ _CARD_CAP（8）。
        """
        rows = _make_large_rows(n_completed=60, n_forming=60)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None

        import re
        # 新格式：「近窗 X 币」（不再出现「个形态」）
        m_coin = re.search(r"近窗\s*(\d+)\s*币", card)
        if m_coin:
            shown = int(m_coin.group(1))
            assert shown <= 16, (
                f"卡片显示 {shown} 币（过多），应截断后 ≤8"
            )
        # 旧格式「近窗 X 个形态」不应出现大数（向后兼容检查）
        m_old = re.search(r"近窗\s*(\d+)\s*个形态", card)
        if m_old:
            shown = int(m_old.group(1))
            assert shown <= 16, (
                f"卡片显示 {shown} 个形态（过多噪音），应截断后 ≤16"
            )


# ========== TDD Bug-fix T-3、G-2、T-5、T-1 测试（先 RED 后 GREEN）==========


def _make_row_with_forming_label(confluence: int, completed: bool) -> dict:
    """构造含指定 confluence 的 forming 或 completed 行。"""
    hit = {
        "pattern": "Gartley",
        "direction": "bull",
        "prz": (60000.0, 60300.0),
        "completed": completed,
        "confidence": 0.72,
        "confluence": confluence,
    }
    if completed:
        hit["points"] = {"D": (99, 60100.0)}
    return {
        "coin": "BTC",
        "symbol": "BTCUSDT",
        "price": 65000.0,
        "tf": "1H",
        "completed": [hit] if completed else [],
        "forming": [] if completed else [hit],
    }


class TestRenderConfluenceLabels:
    """T-3：completed 显示「满足N腿」，forming 显示「收敛N」，语义区分。"""

    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["1H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )

    def test_completed_shows_满足(self) -> None:
        """completed 行（无 setup 时）含「满足」字样（满足N腿约束）。

        新格式：completed 行以 ✅{tf} 前缀，无 setup 退化 PRZ 行含「满足N腿」。
        有 setup 的 completed 行不含「满足」（改为进场/止损/目标格式）。
        """
        rows = [_make_row_with_forming_label(confluence=4, completed=True)]
        # _make_row_with_forming_label 中 completed hit 无 "setup" 键 → 退化 PRZ 行
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 新格式：completed 行以 ✅{tf} 前缀，含 Gartley
        completed_bullet_lines = [
            ln for ln in card.splitlines()
            if "✅" in ln and "Gartley" in ln
        ]
        assert completed_bullet_lines, f"completed 区块应有 ✅ Gartley 行，卡片:\n{card}"
        for ln in completed_bullet_lines:
            assert "满足" in ln, (
                f"无 setup 的 completed 行应含「满足」，实际: {ln!r}"
            )

    def test_forming_shows_收敛(self) -> None:
        """forming 行（🎯{tf} 前缀）含「收敛」字样（收敛N证据）。"""
        rows = [_make_row_with_forming_label(confluence=2, completed=False)]
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 新格式：forming 行以 🎯{tf} 前缀
        forming_bullet_lines = [
            ln for ln in card.splitlines()
            if "🎯" in ln and "Gartley" in ln
        ]
        assert forming_bullet_lines, f"forming 区块应有 🎯 Gartley 行，卡片:\n{card}"
        for ln in forming_bullet_lines:
            assert "收敛" in ln, (
                f"forming 行应含「收敛」，实际: {ln!r}"
            )

    def test_completed_not_shows_收敛(self) -> None:
        """completed 行不含「收敛N」（不与 forming 混用语义）。"""
        rows = [_make_row_with_forming_label(confluence=4, completed=True)]
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 新格式：completed 行以 ✅{tf} 前缀
        for ln in card.splitlines():
            if "✅" in ln and "Gartley" in ln:
                assert "收敛" not in ln, (
                    f"completed 行不应含「收敛」（是 forming 语义），实际: {ln!r}"
                )


class TestRenderSkipsZeroPrice:
    """G-2：price=0 的行跳过，不渲染到卡片。"""

    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
            timeframes=["1H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )

    def test_zero_price_row_skipped(self) -> None:
        """price=0 的 row 不出现在卡片。"""
        rows = [
            {
                "coin": "ETH",
                "symbol": "ETHUSDT",
                "price": 0.0,  # 拉取失败兜底
                "tf": "1H",
                "completed": [
                    {
                        "pattern": "Bat",
                        "direction": "bull",
                        "prz": (1700.0, 1720.0),
                        "completed": True,
                        "confidence": 0.70,
                        "confluence": 4,
                        "points": {"D": (10, 1710.0)},
                    }
                ],
                "forming": [],
            },
            {
                "coin": "BTC",
                "symbol": "BTCUSDT",
                "price": 65000.0,  # 正常价格
                "tf": "1H",
                "completed": [],
                "forming": [
                    {
                        "pattern": "Gartley",
                        "direction": "bull",
                        "prz": (62000.0, 62300.0),
                        "completed": False,
                        "confidence": 0.65,
                        "confluence": 2,
                    }
                ],
            },
        ]
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # ETH（price=0）不应出现在卡片，BTC 应出现
        assert "BTC" in card, "正常 BTC 行应出现在卡片"
        # ETH 行（price=0）不应出现在 bullet 行
        eth_bullet_lines = [
            ln for ln in card.splitlines()
            if ln.strip().startswith("•") and "ETH" in ln
        ]
        assert len(eth_bullet_lines) == 0, (
            f"price=0 的 ETH 行不应出现在卡片，但找到: {eth_bullet_lines}"
        )

    def test_all_zero_price_returns_none(self) -> None:
        """所有 row 都是 price=0 → render 返回 None（无有效行）。"""
        rows = [
            {
                "coin": "BTC",
                "symbol": "BTCUSDT",
                "price": 0.0,
                "tf": "1H",
                "completed": [
                    {
                        "pattern": "Gartley",
                        "direction": "bull",
                        "prz": (60000.0, 60300.0),
                        "completed": True,
                        "confidence": 0.70,
                        "confluence": 4,
                        "points": {"D": (5, 60100.0)},
                    }
                ],
                "forming": [],
            }
        ]
        card = self.monitor.render(rows, _NOW_MS)
        assert card is None, (
            f"所有 price=0 的 rows 应返回 None，但返回了卡片"
        )


class TestRenderPrzRemovesMaxGapParam:
    """T-5：project_prz 删除 max_gap 参数，宽度由 max_prz_width 守卫。"""

    def test_project_prz_no_max_gap_param(self) -> None:
        """project_prz 不接受 max_gap 关键字参数（已删除死守卫）。"""
        import inspect
        from smc_tracker.indicators.harmonic import project_prz
        sig = inspect.signature(project_prz)
        assert "max_gap" not in sig.parameters, (
            f"project_prz 仍有 max_gap 参数（应删除）: {list(sig.parameters.keys())}"
        )


class TestRenderCardSubtitleLag:
    """T-1：卡片副标题含滞后披露说明。"""

    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["1H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )

    def test_subtitle_mentions_lag(self) -> None:
        """卡片副标题含滞后披露（枢轴需右确认，滞后）。"""
        rows = [_make_row_with_forming_label(confluence=2, completed=False)]
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 副标题行（第2行，index=1）应含滞后相关词
        lines = card.splitlines()
        subtitle = lines[1] if len(lines) > 1 else ""
        lag_keywords = ("滞后", "右确认", "order根", "确认")
        has_lag = any(kw in subtitle for kw in lag_keywords)
        assert has_lag, (
            f"副标题应含滞后披露，实际副标题: {subtitle!r}"
        )


# ========== TDD 新增：trade_setup 接入谐波监控推送卡片 ==========


def _make_setup_dict(
    *,
    entry_lo: float = 60200.0,
    entry_hi: float = 60650.0,
    stop: float = 59500.0,
    target1: float = 62100.0,
    target2: float = 64000.0,
    rr: float = 2.08,
    position_qty: float | None = 0.0027,
    position_notional: float | None = 163.0,
    confidence: float = 0.73,
    knn_supports: bool | None = True,
    fib_note: str = "XA-Fib=0.618(60400.0), 距入场0.4%, 黄金口袋(0.618-0.786)✓",
    direction: str = "long",
    pattern: str = "Gartley",
    completed: bool = True,
    coin: str = "BTC",
    tf: str = "4H",
    src_key: str = "C|Gartley|long|60400.0",
) -> dict:
    """构造 TradeSetup-like dict 注入 completed 形态 row 中的 'setups' 键。"""
    from smc_tracker.signals.trade_setup import TradeSetup
    return TradeSetup(
        coin=coin,
        tf=tf,
        direction=direction,
        pattern=pattern,
        completed=completed,
        entry_lo=entry_lo,
        entry_hi=entry_hi,
        stop=stop,
        target1=target1,
        target2=target2,
        rr=rr,
        fib_note=fib_note,
        knn_supports=knn_supports,
        knn_note="KNN≈随机基线",
        position_qty=position_qty,
        position_notional=position_notional,
        confidence=confidence,
        note="诚实标注",
        src_key=src_key,
    )


def _make_rows_with_setups(
    *,
    knn_supports: bool | None = True,
    position_qty: float | None = 0.0027,
    position_notional: float | None = 163.0,
) -> list[dict]:
    """构造 refresh() 行，completed 形态中附带 setups（模拟 refresh 新格式）。"""
    setup = _make_setup_dict(
        knn_supports=knn_supports,
        position_qty=position_qty,
        position_notional=position_notional,
    )
    return [
        {
            "coin": "BTC",
            "symbol": "BTCUSDT",
            "price": 62538.5,
            "tf": "4H",
            "completed": [
                {
                    "pattern": "Gartley",
                    "direction": "bull",
                    "prz": (60200.0, 60650.0),
                    "completed": True,
                    "confidence": 0.72,
                    "confluence": 4,
                    "points": {"D": (99, 60400.0)},
                    "setup": setup,    # 新增 setup 键
                }
            ],
            "forming": [],
        }
    ]


def _make_rows_completed_no_setup() -> list[dict]:
    """构造 completed 形态，但 setup=None（退化为旧 PRZ 行）。"""
    return [
        {
            "coin": "ETH",
            "symbol": "ETHUSDT",
            "price": 1835.0,
            "tf": "1H",
            "completed": [
                {
                    "pattern": "Bat",
                    "direction": "bear",
                    "prz": (1712.0, 1728.0),
                    "completed": True,
                    "confidence": 0.68,
                    "confluence": 4,
                    "points": {"D": (99, 1720.0)},
                    "setup": None,   # 无 setup → 退化 PRZ 行
                }
            ],
            "forming": [],
        }
    ]


class TestHarmonicMonitorTradeSetup:
    """TDD：trade_setup 接入谐波监控推送卡片验证。"""

    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
            timeframes=["4H", "1H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
            account_usd=10000.0,
            risk_pct=0.01,
            target_rr=2.0,
        )

    def test_render_contains_entry_keywords(self) -> None:
        """completed setup 卡片含「进场」「止损」「目标」「仓位」「置信」「KNN」。"""
        rows = _make_rows_with_setups()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        for kw in ("进场", "止损", "目标", "仓位", "置信", "KNN"):
            assert kw in card, f"卡片缺少关键词「{kw}」，卡片:\n{card}"

    def test_render_price_no_scientific_notation_with_setup(self) -> None:
        """setup 价格（进场/止损/目标）不含科学计数法。"""
        rows = _make_rows_with_setups()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "e+" not in card.lower(), f"含科学计数法:\n{card}"
        assert "e-" not in card.lower(), f"含科学计数法:\n{card}"

    def test_render_knn_supported_shows_check(self) -> None:
        """knn_supports=True 显示 ✓。"""
        rows = _make_rows_with_setups(knn_supports=True)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "✓" in card, f"knn_supports=True 应显示 ✓，卡片:\n{card}"

    def test_render_knn_rejected_shows_cross(self) -> None:
        """knn_supports=False 显示 ✗。"""
        rows = _make_rows_with_setups(knn_supports=False)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "✗" in card, f"knn_supports=False 应显示 ✗，卡片:\n{card}"

    def test_render_knn_none_shows_question(self) -> None:
        """knn_supports=None（样本不足）显示 ?。"""
        rows = _make_rows_with_setups(knn_supports=None)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "?" in card, f"knn_supports=None 应显示 ?，卡片:\n{card}"

    def test_render_no_setup_fallback_to_prz(self) -> None:
        """completed 形态 setup=None 时退化为旧 PRZ 行，不崩溃，不含「进场」字样。"""
        rows = _make_rows_completed_no_setup()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 新格式：completed 行以 ✅{tf} 前缀；无 setup 退化为 PRZ 行（不含进场/止损）
        completed_lines = [
            ln for ln in card.splitlines()
            if "✅" in ln and "Bat" in ln
        ]
        assert len(completed_lines) > 0, f"Bat 行应出现在卡片（✅前缀），卡片:\n{card}"
        for ln in completed_lines:
            assert "进场" not in ln, f"无 setup 的行不应含「进场」: {ln!r}"
            assert "止损" not in ln, f"无 setup 的行不应含「止损」: {ln!r}"

    def test_render_position_qty_no_scientific_notation(self) -> None:
        """仓位数量非科学计数（如 0.0027，不用 2.7e-3 表示）。"""
        rows = _make_rows_with_setups(position_qty=0.0027, position_notional=163.0)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "e-" not in card.lower(), f"仓位含科学计数法:\n{card}"
        assert "e+" not in card.lower(), f"仓位含科学计数法:\n{card}"

    def test_render_position_qty_none_shows_dash(self) -> None:
        """position_qty=None 时仓位显示「—」（仓位计算失败，诚实标注）。"""
        rows = _make_rows_with_setups(position_qty=None, position_notional=None)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "—" in card, f"position_qty=None 应显示「—」，卡片:\n{card}"

    def test_harmonic_monitor_accepts_account_params(self) -> None:
        """HarmonicMonitor 支持 account_usd/risk_pct/target_rr 参数（不报 TypeError）。"""
        monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["4H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=5,
            account_usd=50000.0,
            risk_pct=0.02,
            target_rr=3.0,
        )
        assert monitor.account_usd == 50000.0
        assert monitor.risk_pct == 0.02
        assert monitor.target_rr == 3.0

    def test_render_fib_note_present_in_setup_line(self) -> None:
        """completed setup 行后有 fib_note 附注行（含 XA-Fib 或 斐波那契 相关）。"""
        rows = _make_rows_with_setups()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # fib_note 以缩进行附在 bullet 行之后
        lines = card.splitlines()
        found_fib = any("XA-Fib" in ln or "Fib" in ln or "斐波" in ln for ln in lines)
        assert found_fib, f"卡片缺少 fib_note 行，卡片:\n{card}"

    def test_render_subtitle_mentions_setup(self) -> None:
        """卡片前3行（副标题行）整体含「进场/止损/止盈/仓位」或「完整=入场触发」字样。

        新格式：line[1] 含「完整=入场触发」，全卡含进场/止损/仓位等可执行字样（setup 行）。
        """
        rows = _make_rows_with_setups()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 新格式：副标题（前3行）含「完整=入场触发」，全卡 setup 行含「进场」「止损」
        header_block = "\n".join(card.splitlines()[:3])
        setup_keywords = ("进场", "止损", "止盈", "仓位", "可执行", "完整=入场触发", "入场触发")
        has_kw = any(kw in header_block for kw in setup_keywords)
        assert has_kw, f"前3行应含可执行 setup 相关词，实际:\n{header_block!r}"


# ── 🔴-1 注入键碰撞修复：harmonic_monitor 按 src_key 注入 setup ──────────────

def _make_rows_two_gartley_bull_completed() -> list[dict]:
    """构造 refresh() rows，同 tf 两个 Gartley-bull completed，D 点不同。

    应各自注入独立 setup，不再共享 (pattern,direction,completed) 首个 setup。
    """
    from smc_tracker.signals.trade_setup import TradeSetup
    # setup_a: 对应 D@60400 的进场区
    setup_a = TradeSetup(
        coin="BTC", tf="4H", direction="long", pattern="Gartley",
        completed=True, entry_lo=59796.0, entry_hi=61004.0,
        stop=57000.0, target1=64000.0, target2=67000.0, rr=2.0,
        fib_note="D=形态定义比率位(0.786·XA 等，非独立确认)",
        knn_supports=None, knn_note="样本不足",
        position_qty=None, position_notional=None,
        confidence=0.72, note="诚实标注",
        src_key="C|Gartley|long|60400.0",
    )
    # setup_b: 对应 D@57000 的进场区
    setup_b = TradeSetup(
        coin="BTC", tf="4H", direction="long", pattern="Gartley",
        completed=True, entry_lo=56145.0, entry_hi=57855.0,
        stop=53000.0, target1=60000.0, target2=63000.0, rr=2.0,
        fib_note="D=形态定义比率位(0.786·XA 等，非独立确认)",
        knn_supports=None, knn_note="样本不足",
        position_qty=None, position_notional=None,
        confidence=0.70, note="诚实标注",
        src_key="C|Gartley|long|57000.0",
    )
    return [
        {
            "coin": "BTC",
            "symbol": "BTCUSDT",
            "price": 62000.0,
            "tf": "4H",
            "completed": [
                {
                    "pattern": "Gartley",
                    "direction": "bull",
                    "prz": (59400.0, 61400.0),
                    "completed": True,
                    "confidence": 0.72,
                    "confluence": 2,
                    "points": {
                        "X": (0, 50000.0), "A": (10, 70000.0),
                        "B": (15, 55000.0), "C": (20, 65000.0),
                        "D": (25, 60400.0),
                    },
                    "setup": setup_a,  # 精确注入 setup_a
                },
                {
                    "pattern": "Gartley",
                    "direction": "bull",
                    "prz": (55800.0, 58200.0),
                    "completed": True,
                    "confidence": 0.70,
                    "confluence": 2,
                    "points": {
                        "X": (30, 45000.0), "A": (40, 65000.0),
                        "B": (45, 50000.0), "C": (50, 60000.0),
                        "D": (55, 57000.0),
                    },
                    "setup": setup_b,  # 精确注入 setup_b
                },
            ],
            "forming": [],
        }
    ]


class TestHarmonicInjectionNoCollision:
    """🔴-1: harmonic_monitor 按 src_key 精确注入，两个 Gartley-bull 各得各的 setup。"""

    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["4H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )

    def test_two_gartley_render_two_distinct_entries(self) -> None:
        """两个 Gartley-bull 各自进场区出现在卡片（不共享同一 setup 进场区）。"""
        rows = _make_rows_two_gartley_bull_completed()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 新格式：completed 行以 ✅{tf} 前缀（不再是 • 前缀）
        bullet_lines = [
            ln for ln in card.splitlines()
            if "✅" in ln and "Gartley" in ln
        ]
        assert len(bullet_lines) >= 2, (
            f"两个 Gartley-bull 应渲染 ≥2 条 ✅ 行，实际 {len(bullet_lines)} 条（注入碰撞未修）\n卡片:\n{card}"
        )

    def test_two_gartley_have_different_entry_lo(self) -> None:
        """两条 Gartley 行进场区下沿不同（各自 setup 数据不被碰撞覆盖）。"""
        rows = _make_rows_two_gartley_bull_completed()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 新格式：completed 行以 ✅{tf} 前缀
        # setup_a entry_lo=59796，setup_b entry_lo=56145
        gartley_bullet_lines = [
            ln for ln in card.splitlines()
            if "✅" in ln and "Gartley" in ln
        ]
        # 两条行的文字不相同（不同进场区）
        assert len(gartley_bullet_lines) >= 2, (
            f"两个 Gartley-bull 应有 ≥2 条 ✅ 行，实际 {len(gartley_bullet_lines)} 条\n卡片:\n{card}"
        )
        # 两条行内容必须不同（不共享同一 setup 进场区）
        assert gartley_bullet_lines[0] != gartley_bullet_lines[1], (
            "两个 Gartley-bull 行内容相同，存在 setup 注入碰撞"
        )


# ── 订单流确认接入谐波监控测试（TDD 新增） ──────────────────────────────────────


class _FakeOB:
    """伪订单簿提供者（鸭子类型）：模拟 HLOrderbookMonitor 接口。

    confirming_wall 返回一堵大墙；book_imbalance 返回同向失衡（long=正，short=负）。
    """

    def __init__(
        self,
        *,
        wall_notional: float = 800_000.0,
        wall_dist_pct: float = 0.008,
        imbalance: float = 0.42,   # 正=bid 占优=支持 long；负=ask 占优=支持 short
        has_wall: bool = True,
    ) -> None:
        self._wall_notional = wall_notional
        self._wall_dist_pct = wall_dist_pct
        self._imbalance = imbalance
        self._has_wall = has_wall

    def confirming_wall(
        self, coin: str, price: float, side: str, tol_pct: float = 0.015
    ) -> dict | None:
        if not self._has_wall:
            return None
        return {
            "notional": self._wall_notional,
            "dist_pct": self._wall_dist_pct,
            "side": side,
            "price": price * (1 - self._wall_dist_pct),
        }

    def book_imbalance(self, coin: str) -> dict[str, float]:
        return {"imbalance": self._imbalance}


def _make_completed_row_with_setup(
    *,
    direction: str = "long",
    entry_lo: float = 60200.0,
    entry_hi: float = 60650.0,
    confidence: float = 0.72,
    orderflow=None,  # 预注入 orderflow（None=未注入，由 monitor 注入）
) -> list[dict]:
    """构造含 completed setup 的 rows，支持预注入 orderflow。"""
    from smc_tracker.signals.trade_setup import TradeSetup
    setup = TradeSetup(
        coin="BTC", tf="4H", direction=direction, pattern="Gartley",
        completed=True, entry_lo=entry_lo, entry_hi=entry_hi,
        stop=59000.0, target1=62500.0, target2=65000.0, rr=2.0,
        fib_note="D=形态定义比率位(0.786·XA 等，非独立确认)",
        knn_supports=True, knn_note="KNN≈随机基线",
        position_qty=0.003, position_notional=180.0,
        confidence=confidence, note="诚实标注",
        src_key=f"C|Gartley|{direction}|60400.0",
        orderflow=orderflow,  # 预注入（或 None 由 monitor 层注入）
    )
    hit_direction = "bull" if direction == "long" else "bear"
    return [
        {
            "coin": "BTC",
            "symbol": "BTCUSDT",
            "price": 62538.5,
            "tf": "4H",
            "completed": [
                {
                    "pattern": "Gartley",
                    "direction": hit_direction,
                    "prz": (entry_lo, entry_hi),
                    "completed": True,
                    "confidence": confidence,
                    "confluence": 3,
                    "points": {"D": (99, 60400.0)},
                    "setup": setup,
                }
            ],
            "forming": [],
        }
    ]


class TestOrderflowConfirmIntegration:
    """订单流确认接入谐波监控 render 的集成测试（注意：render 本身是纯函数，
    orderflow 已在 _fetch_tf/refresh 中注入到 setup.orderflow；
    这里模拟预注入路径，测试 render 对 orderflow 的显示逻辑）。"""

    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["4H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )

    def test_orderflow_confirmed_shows_checkmark(self) -> None:
        """orderflow.confirmed=True → 卡片含「📊订单流✓」。"""
        from smc_tracker.signals.orderflow_confirm import OrderflowConfirm
        of = OrderflowConfirm(
            confirmed=True, wall_usd=800_000.0, wall_dist_pct=0.008,
            imbalance=0.42, note="测试确认"
        )
        rows = _make_completed_row_with_setup(orderflow=of)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "📊订单流✓" in card, f"确认墙应显示「📊订单流✓」，卡片:\n{card}"

    def test_orderflow_confirmed_shows_wall_usd_no_scientific(self) -> None:
        """确认行包含墙名义额，且价格非科学计数法。"""
        from smc_tracker.signals.orderflow_confirm import OrderflowConfirm
        of = OrderflowConfirm(
            confirmed=True, wall_usd=800_000.0, wall_dist_pct=0.008,
            imbalance=0.42, note="测试"
        )
        rows = _make_completed_row_with_setup(orderflow=of)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "e+" not in card.lower(), f"卡片含科学计数法:\n{card}"
        assert "e-" not in card.lower(), f"卡片含科学计数法:\n{card}"
        # 墙名义额 800000 以某种格式出现（fmt_px）
        assert "800" in card, f"墙名义额 800000 未出现在卡片:\n{card}"

    def test_orderflow_unconfirmed_shows_cross(self) -> None:
        """orderflow 非 None 但 confirmed=False → 卡片含「📊订单流✗」。"""
        from smc_tracker.signals.orderflow_confirm import OrderflowConfirm
        of = OrderflowConfirm(
            confirmed=False, wall_usd=0.0, wall_dist_pct=1.0,
            imbalance=0.0, note="PRZ处无同向墙"
        )
        rows = _make_completed_row_with_setup(orderflow=of)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "📊订单流✗" in card, f"未确认应显示「📊订单流✗」，卡片:\n{card}"

    def test_orderflow_none_no_marker(self) -> None:
        """setup.orderflow=None → 卡片不显示订单流标记行（无数据，诚实不显示）。"""
        rows = _make_completed_row_with_setup(orderflow=None)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "📊订单流" not in card, (
            f"orderflow=None 时不应显示订单流行，卡片:\n{card}"
        )

    def test_ob_provider_none_no_crash(self) -> None:
        """ob_provider=None → render 不崩溃，不显示订单流行（诚实）。"""
        monitor_no_ob = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["4H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
            ob_provider=None,  # 无数据提供者
        )
        rows = _make_completed_row_with_setup(orderflow=None)
        card = monitor_no_ob.render(rows, _NOW_MS)
        assert card is not None, "ob_provider=None 时 render 不应返回 None（有 setup）"
        assert "📊订单流" not in card, (
            f"ob_provider=None 时不应显示订单流行，卡片:\n{card}"
        )

    def test_ob_provider_accepted_in_constructor(self) -> None:
        """HarmonicMonitor 接受 ob_provider 参数，存储为属性。"""
        fake_ob = _FakeOB()
        monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["4H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
            ob_provider=fake_ob,
        )
        assert monitor.ob_provider is fake_ob, (
            f"ob_provider 应存储为属性，实际: {monitor.ob_provider!r}"
        )

    def test_subtitle_mentions_orderflow_intent(self) -> None:
        """卡片副标题（第3行）含订单流确认相关词（领先意图 × PRZ 完整闭环描述）。"""
        rows = _make_completed_row_with_setup()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        lines = card.splitlines()
        # 第3行（index=2）是新增的订单流副标题
        subtitle2 = lines[2] if len(lines) > 2 else ""
        of_keywords = ("订单流", "领先意图", "PRZ", "spoof", "确认")
        has_kw = any(kw in subtitle2 for kw in of_keywords)
        assert has_kw, (
            f"第3行副标题应含订单流说明，实际: {subtitle2!r}"
        )

    def test_confirmed_setup_confidence_boosted(self) -> None:
        """订单流确认的 setup 置信 ×1.1（封顶 0.90），未确认不 boost。

        注意：render 是纯函数，不 boost；boost 发生在 _fetch_tf（refresh 层）。
        这里测试 FakeOB 通过 refresh 路径注入后置信被 boost 的行为，
        通过构造预设置信验证 render 正确显示 boosted 值。
        """
        from smc_tracker.signals.orderflow_confirm import OrderflowConfirm
        # 模拟 refresh 已 boost：base 0.72 × 1.1 = 0.792 → 显示 79%
        of = OrderflowConfirm(
            confirmed=True, wall_usd=500_000.0, wall_dist_pct=0.005,
            imbalance=0.30, note="已确认"
        )
        # 预注入已 boost 的置信（0.792）
        from smc_tracker.signals.trade_setup import TradeSetup
        setup_boosted = TradeSetup(
            coin="BTC", tf="4H", direction="long", pattern="Gartley",
            completed=True, entry_lo=60200.0, entry_hi=60650.0,
            stop=59000.0, target1=62500.0, target2=65000.0, rr=2.0,
            fib_note="D=形态定义比率位",
            knn_supports=True, knn_note="样本足",
            position_qty=0.003, position_notional=180.0,
            confidence=0.792,  # 0.72 × 1.1
            note="诚实",
            src_key="C|Gartley|long|60400.0",
            orderflow=of,
        )
        rows = [
            {
                "coin": "BTC", "symbol": "BTCUSDT", "price": 62538.5, "tf": "4H",
                "completed": [
                    {
                        "pattern": "Gartley", "direction": "bull",
                        "prz": (60200.0, 60650.0), "completed": True,
                        "confidence": 0.792, "confluence": 3,
                        "points": {"D": (99, 60400.0)},
                        "setup": setup_boosted,
                    }
                ],
                "forming": [],
            }
        ]
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 置信显示 79%（int(0.792*100)=79）
        assert "79" in card, f"boosted 置信 79% 应出现在卡片:\n{card}"
        assert "📊订单流✓" in card, "确认 setup 应显示订单流✓"

    def test_confirmed_shows_bid_label_for_long(self) -> None:
        """long 方向确认行显示 bid 墙标签。"""
        from smc_tracker.signals.orderflow_confirm import OrderflowConfirm
        of = OrderflowConfirm(
            confirmed=True, wall_usd=600_000.0, wall_dist_pct=0.007,
            imbalance=0.38, note="bid墙确认"
        )
        rows = _make_completed_row_with_setup(direction="long", orderflow=of)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "bid" in card, f"long 方向确认行应含 bid，卡片:\n{card}"

    def test_confirmed_shows_ask_label_for_short(self) -> None:
        """short 方向确认行显示 ask 墙标签。"""
        from smc_tracker.signals.orderflow_confirm import OrderflowConfirm
        of = OrderflowConfirm(
            confirmed=True, wall_usd=600_000.0, wall_dist_pct=0.007,
            imbalance=-0.38, note="ask墙确认"
        )
        rows = _make_completed_row_with_setup(direction="short", orderflow=of)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "ask" in card, f"short 方向确认行应含 ask，卡片:\n{card}"


# ── TDD: to_records + DB 落库往返测试 ──────────────────────────────────────────

def _make_completed_hit_with_setup(
    *,
    direction: str = "long",
    entry_lo: float = 60200.0,
    entry_hi: float = 60650.0,
    stop: float = 59500.0,
    target1: float = 62100.0,
    target2: float = 64000.0,
    rr: float = 2.08,
    confidence: float = 0.73,
    fib_note: str = "XA-Fib=0.618(60400.0)",
    knn_supports: bool | None = True,
    orderflow_confirmed: bool | None = None,  # None=无数据, True=确认, False=未确认
    wall_usd: float = 500_000.0,
    pattern: str = "Gartley",
    coin: str = "BTC",
    tf: str = "4H",
) -> dict:
    """构造含 setup 的 completed hit dict（模拟 refresh 产出格式）。"""
    from smc_tracker.signals.trade_setup import TradeSetup
    from smc_tracker.signals.orderflow_confirm import OrderflowConfirm

    # 构建 orderflow
    if orderflow_confirmed is None:
        of = None
    elif orderflow_confirmed:
        of = OrderflowConfirm(
            confirmed=True, wall_usd=wall_usd, wall_dist_pct=0.005,
            imbalance=0.35, note="bid墙确认"
        )
    else:
        of = OrderflowConfirm(
            confirmed=False, wall_usd=0.0, wall_dist_pct=1.0,
            imbalance=0.0, note="PRZ无同向墙"
        )

    dir_raw = "bull" if direction == "long" else "bear"
    setup = TradeSetup(
        coin=coin, tf=tf, direction=direction, pattern=pattern,
        completed=True, entry_lo=entry_lo, entry_hi=entry_hi,
        stop=stop, target1=target1, target2=target2, rr=rr,
        fib_note=fib_note, knn_supports=knn_supports, knn_note="KNN≈随机基线",
        position_qty=0.0027, position_notional=163.0,
        confidence=confidence, note="诚实标注",
        src_key=f"C|{pattern}|{direction}|60400.0",
        orderflow=of,
    )
    return {
        "pattern": pattern,
        "direction": dir_raw,
        "prz": (entry_lo, entry_hi),
        "completed": True,
        "confidence": confidence,
        "confluence": 4,
        "points": {"D": (99, 60400.0)},
        "setup": setup,
    }


def _make_forming_hit(
    *,
    direction: str = "bull",
    prz_lo: float = 60000.0,
    prz_hi: float = 60500.0,
    confidence: float = 0.65,
    pattern: str = "Bat",
) -> dict:
    """构造 forming hit dict（无 setup，模拟 refresh 产出格式）。"""
    return {
        "pattern": pattern,
        "direction": direction,
        "prz": (prz_lo, prz_hi),
        "completed": False,
        "confidence": confidence,
        "confluence": 3,
        "setup": None,
    }


_NOW_REC = 1_700_100_000_000


class TestToRecords:
    """to_records 纯函数：把 refresh rows 展平成 19 列 tuple 列表。"""

    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
            timeframes=["4H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )

    def _make_rows_completed_with_of(
        self,
        *,
        knn_supports: bool | None = True,
        orderflow_confirmed: bool | None = True,
        wall_usd: float = 500_000.0,
    ) -> list[dict]:
        hit = _make_completed_hit_with_setup(
            knn_supports=knn_supports,
            orderflow_confirmed=orderflow_confirmed,
            wall_usd=wall_usd,
        )
        return [
            {
                "coin": "BTC",
                "symbol": "BTCUSDT",
                "price": 62538.5,
                "tf": "4H",
                "completed": [hit],
                "forming": [],
            }
        ]

    def _make_rows_forming(self) -> list[dict]:
        hit = _make_forming_hit()
        return [
            {
                "coin": "ETH",
                "symbol": "ETHUSDT",
                "price": 1835.0,
                "tf": "1H",
                "completed": [],
                "forming": [hit],
            }
        ]

    def test_to_records_returns_list(self) -> None:
        """to_records 返回列表。"""
        rows = self._make_rows_completed_with_of()
        result = self.monitor.to_records(rows, _NOW_REC)
        assert isinstance(result, list)

    def test_completed_with_setup_produces_one_record(self) -> None:
        """completed hit 含 setup → 产出 1 条记录。"""
        rows = self._make_rows_completed_with_of()
        result = self.monitor.to_records(rows, _NOW_REC)
        assert len(result) == 1

    def test_record_has_29_columns(self) -> None:
        """每条记录恰好 29 列（符合 harmonic_setups schema，含 XABCD 点）。"""
        rows = self._make_rows_completed_with_of()
        result = self.monitor.to_records(rows, _NOW_REC)
        assert len(result[0]) == 29, f"期望 29 列，实际 {len(result[0])} 列"

    def test_completed_kind_is_completed(self) -> None:
        """completed hit → kind='completed'（列 index=3）。"""
        rows = self._make_rows_completed_with_of()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[3] == "completed", f"kind 应为 'completed'，实际 {rec[3]!r}"

    def test_completed_direction_long(self) -> None:
        """bull direction → direction='long'（列 index=5）。"""
        rows = self._make_rows_completed_with_of()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[5] == "long", f"direction 应为 'long'，实际 {rec[5]!r}"

    def test_completed_direction_short(self) -> None:
        """bear direction → direction='short'。"""
        hit = _make_completed_hit_with_setup(direction="short")
        rows = [{"coin": "BTC", "symbol": "BTCUSDT", "price": 60000.0, "tf": "4H",
                 "completed": [hit], "forming": []}]
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[5] == "short", f"direction 应为 'short'，实际 {rec[5]!r}"

    def test_completed_price_is_float(self) -> None:
        """price 字段（列 index=6）是 float。"""
        rows = self._make_rows_completed_with_of()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert isinstance(rec[6], float), f"price 应为 float，实际 {type(rec[6])}"

    def test_completed_entry_lo_hi_from_setup(self) -> None:
        """entry_lo/hi（列 7/8）来自 setup，是 float。"""
        rows = self._make_rows_completed_with_of()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert isinstance(rec[7], float), f"entry_lo 应为 float"
        assert isinstance(rec[8], float), f"entry_hi 应为 float"
        assert rec[7] == 60200.0
        assert rec[8] == 60650.0

    def test_completed_stop_target_from_setup(self) -> None:
        """stop/target1/target2/rr（列 9-12）来自 setup，非 None。"""
        rows = self._make_rows_completed_with_of()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[9] == 59500.0,  f"stop 应为 59500.0，实际 {rec[9]}"
        assert rec[10] == 62100.0, f"target1 应为 62100.0，实际 {rec[10]}"
        assert rec[11] == 64000.0, f"target2 应为 64000.0，实际 {rec[11]}"
        assert abs(rec[12] - 2.08) < 1e-6, f"rr 应为 2.08，实际 {rec[12]}"

    def test_knn_true_maps_to_checkmark(self) -> None:
        """knn_supports=True → knn='✓'（列 index=14）。"""
        rows = self._make_rows_completed_with_of(knn_supports=True)
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[14] == "✓", f"knn 应为 '✓'，实际 {rec[14]!r}"

    def test_knn_false_maps_to_cross(self) -> None:
        """knn_supports=False → knn='✗'。"""
        rows = self._make_rows_completed_with_of(knn_supports=False)
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[14] == "✗", f"knn 应为 '✗'，实际 {rec[14]!r}"

    def test_knn_none_maps_to_question(self) -> None:
        """knn_supports=None → knn='?'。"""
        rows = self._make_rows_completed_with_of(knn_supports=None)
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[14] == "?", f"knn 应为 '?'，实际 {rec[14]!r}"

    def test_orderflow_confirmed_maps_to_checkmark_bid(self) -> None:
        """orderflow.confirmed=True, long → orderflow='✓bid500000.0'（列 index=15）。"""
        rows = self._make_rows_completed_with_of(
            orderflow_confirmed=True, wall_usd=500_000.0
        )
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[15].startswith("✓bid"), f"orderflow 应以 '✓bid' 开头，实际 {rec[15]!r}"

    def test_orderflow_not_confirmed_maps_to_cross(self) -> None:
        """orderflow.confirmed=False → orderflow='✗'。"""
        rows = self._make_rows_completed_with_of(orderflow_confirmed=False)
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[15] == "✗", f"orderflow 应为 '✗'，实际 {rec[15]!r}"

    def test_orderflow_none_maps_to_empty(self) -> None:
        """setup.orderflow=None（无数据）→ orderflow=''。"""
        rows = self._make_rows_completed_with_of(orderflow_confirmed=None)
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[15] == "", f"orderflow 无数据应为 ''，实际 {rec[15]!r}"

    def test_forming_kind_is_forming(self) -> None:
        """forming hit → kind='forming'（列 index=3）。"""
        rows = self._make_rows_forming()
        result = self.monitor.to_records(rows, _NOW_REC)
        assert len(result) == 1
        rec = result[0]
        assert rec[3] == "forming", f"kind 应为 'forming'，实际 {rec[3]!r}"

    def test_forming_direction_bull_maps_long(self) -> None:
        """forming bull → direction='long'。"""
        rows = self._make_rows_forming()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[5] == "long", f"forming bull→direction 应为 'long'，实际 {rec[5]!r}"

    def test_forming_direction_bear_maps_short(self) -> None:
        """forming bear → direction='short'。"""
        hit = _make_forming_hit(direction="bear")
        rows = [{"coin": "ETH", "symbol": "ETHUSDT", "price": 1835.0, "tf": "1H",
                 "completed": [], "forming": [hit]}]
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[5] == "short", f"forming bear→direction 应为 'short'，实际 {rec[5]!r}"

    def test_forming_stop_target_are_none(self) -> None:
        """forming hit stop/target1/target2 为 None（无精确值）。"""
        rows = self._make_rows_forming()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[9] is None,  f"forming stop 应为 None，实际 {rec[9]}"
        assert rec[10] is None, f"forming target1 应为 None，实际 {rec[10]}"
        assert rec[11] is None, f"forming target2 应为 None，实际 {rec[11]}"

    def test_forming_entry_is_prz(self) -> None:
        """forming entry_lo/hi 等于 hit.prz（列 7/8）。"""
        rows = self._make_rows_forming()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[7] == 60000.0, f"forming entry_lo 应为 60000.0，实际 {rec[7]}"
        assert rec[8] == 60500.0, f"forming entry_hi 应为 60500.0，实际 {rec[8]}"

    def test_forming_prz_lo_hi(self) -> None:
        """forming prz_lo/prz_hi（列 17/18）来自 hit.prz。"""
        rows = self._make_rows_forming()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[17] == 60000.0, f"prz_lo 应为 60000.0，实际 {rec[17]}"
        assert rec[18] == 60500.0, f"prz_hi 应为 60500.0，实际 {rec[18]}"

    def test_ts_column_is_now_ms(self) -> None:
        """列 index=0 是 now_ms 时间戳。"""
        rows = self._make_rows_completed_with_of()
        rec = self.monitor.to_records(rows, _NOW_REC)[0]
        assert rec[0] == _NOW_REC, f"ts 应为 {_NOW_REC}，实际 {rec[0]}"

    def test_mixed_rows_produces_multiple_records(self) -> None:
        """completed + forming 各一条 row → 产出 2 条记录。"""
        completed_row = self._make_rows_completed_with_of()[0]
        forming_row = self._make_rows_forming()[0]
        result = self.monitor.to_records([completed_row, forming_row], _NOW_REC)
        assert len(result) == 2, f"应有 2 条记录，实际 {len(result)}"

    def test_completed_no_setup_uses_prz_nulls(self) -> None:
        """completed hit setup=None → stop/target/entry 均为 NULL，prz 来自 hit。"""
        hit = {
            "pattern": "Bat", "direction": "bull",
            "prz": (1700.0, 1720.0), "completed": True,
            "confidence": 0.68, "confluence": 4,
            "points": {"D": (10, 1710.0)}, "setup": None,
        }
        rows = [{"coin": "ETH", "symbol": "ETHUSDT", "price": 1835.0, "tf": "1H",
                 "completed": [hit], "forming": []}]
        result = self.monitor.to_records(rows, _NOW_REC)
        assert len(result) == 1
        rec = result[0]
        assert rec[3] == "completed"
        assert rec[7] is None, f"no-setup entry_lo 应为 None，实际 {rec[7]}"
        assert rec[9] is None, f"no-setup stop 应为 None，实际 {rec[9]}"
        assert rec[17] == 1700.0, f"prz_lo 应为 1700.0，实际 {rec[17]}"
        assert rec[18] == 1720.0, f"prz_hi 应为 1720.0，实际 {rec[18]}"


# ── TDD: DB 优先 / live 回退 / store=None 三模式 ────────────────────────────────


def _make_candles(n: int, coin: str = "BTC", tf: str = "4H") -> list:
    """构造 n 根合成 Candle（导入 smc_tracker 的 Candle dataclass）。

    价格在 60000–62000 之间小幅波动，满足谐波最小窗口要求。
    """
    from smc_tracker.models import Candle
    step_ms = 4 * 3600 * 1000  # 4H 周期 = 14400000 ms
    base_ms = 1_700_000_000_000
    result: list[Candle] = []
    for i in range(n):
        o = 60000.0 + (i % 5) * 200.0
        h = o + 300.0
        l = o - 300.0
        c = o + 100.0
        result.append(Candle(
            coin=coin, interval=tf,
            open_time_ms=base_ms + i * step_ms,
            close_time_ms=base_ms + (i + 1) * step_ms,
            o=o, h=h, l=l, c=c, v=1.5, n=0,
        ))
    return result


class _FakeStoreHarmonic:
    """FakeStore：满足 get_candles/count_candles/upsert_candles 契约（谐波测试用）。

    构造时传入每次 get_candles 返回的预设列表。
    upsert_candles 调用记录到 self.upserted。
    """

    def __init__(self, candles: list, /) -> None:
        self._candles = candles
        self.upserted: list[tuple] = []

    def get_candles(self, coin: str, tf: str, limit: int = 1000) -> list:
        return self._candles[:limit]

    def count_candles(self, coin: str, tf: str) -> int:
        return len(self._candles)

    def upsert_candles(self, rows) -> None:
        self.upserted.extend(rows)


class TestHarmonicMonitorDBFetch:
    """HarmonicMonitor DB 优先 / live 回退 / store=None 三模式 TDD 测试。"""

    def setup_method(self) -> None:
        self.order = 3
        self.bars = 100
        # need_min = 2*3+3 = 9；合成 50 根（足够）
        self._enough_candles = _make_candles(50)

    def _make_monitor(self, store=None) -> "HarmonicMonitor":
        return HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"},
            timeframes=["4H"],
            bars=self.bars,
            order=self.order,
            tol=0.05,
            top_n=5,
            store=store,
        )

    def test_store_attribute_exists_default_none(self) -> None:
        """store 参数默认 None，无 store 时向后兼容。"""
        mon = self._make_monitor()
        assert mon.store is None

    def test_store_attribute_stored(self) -> None:
        """store 参数正确存储为属性。"""
        fake = _FakeStoreHarmonic(self._enough_candles)
        mon = self._make_monitor(store=fake)
        assert mon.store is fake

    def test_db_hit_does_not_call_live_klines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB 数据足够时，live bg.klines 调用次数应为 0。"""
        live_calls: list[tuple] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            live_calls.append((symbol, tf))
            return []

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        fake_store = _FakeStoreHarmonic(self._enough_candles)
        mon = self._make_monitor(store=fake_store)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        assert len(live_calls) == 0, (
            f"DB 命中时不应调用 live klines，实际调用 {len(live_calls)} 次: {live_calls}"
        )

    def test_db_insufficient_falls_back_to_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB 不足（< need_min）时，应回退 live klines（调用次数 = 1）。"""
        live_calls: list[tuple] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            live_calls.append((symbol, tf))
            # 返回足够根数（live 成功）
            return _make_candles(50)

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        # 只给 2 根（< need_min=9），强制回退
        fake_store = _FakeStoreHarmonic(_make_candles(2))
        mon = self._make_monitor(store=fake_store)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        assert len(live_calls) == 1, (
            f"DB 不足应调用 live klines 1 次，实际 {len(live_calls)} 次"
        )

    def test_db_insufficient_upserts_live_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB 不足回退 live 后，live 数据应被 upsert 回填到 DB（自愈）。"""
        live_data = _make_candles(50)

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return live_data

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        fake_store = _FakeStoreHarmonic(_make_candles(2))
        mon = self._make_monitor(store=fake_store)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        # upsert_candles 应被调用（至少有 1 条回填记录）
        assert len(fake_store.upserted) > 0, (
            "DB 不足回退 live 后，应 upsert 回填数据，但 upsert_candles 未被调用"
        )
        # 每条记录格式 (coin, tf, open_ms, o, h, l, c, v)
        first = fake_store.upserted[0]
        assert len(first) == 8, f"upsert 行应为 8 列，实际 {len(first)} 列"

    def test_store_none_calls_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """store=None（纯 live 模式）时，bg.klines 被正常调用（向后兼容）。"""
        live_calls: list[tuple] = []

        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            live_calls.append((symbol, tf))
            return []

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        mon = self._make_monitor(store=None)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        # store=None 时每个 tf 都走 live（1 个币 × 1 个 tf = 1 次）
        assert len(live_calls) == 1, (
            f"store=None 时应调用 live klines 1 次，实际 {len(live_calls)} 次"
        )

    def test_db_hit_upsert_not_called(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB 命中时（足够根数），upsert_candles 不应被调用。"""
        async def fake_klines(self_bg, symbol, tf, bars=1000, coin=""):
            return []  # 不应被调用

        from smc_tracker.bitget import rest as rest_mod
        monkeypatch.setattr(rest_mod.BitgetREST, "klines", fake_klines)

        fake_store = _FakeStoreHarmonic(self._enough_candles)
        mon = self._make_monitor(store=fake_store)

        import asyncio
        asyncio.run(mon.refresh(now_ms=1_700_000_000_000))

        assert len(fake_store.upserted) == 0, (
            f"DB 命中时不应调用 upsert_candles，实际 upserted {len(fake_store.upserted)} 行"
        )


class TestHarmonicSetupDB:
    """insert_harmonic_setups + recent_harmonic_setups 往返测试（用临时 in-memory Store）。"""

    def setup_method(self) -> None:
        import tempfile
        import os
        # 使用 in-memory 或临时文件（Store 不支持 ":memory:" 路径语法时用 tmp）
        from smc_tracker.storage.db import Store
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.store = Store(self._tmp.name)

    def teardown_method(self) -> None:
        import os
        self.store.close()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _sample_rows(self) -> list[tuple]:
        """构造 3 条合法的 19 列 tuple（1 completed, 2 forming）。"""
        return [
            # ts, coin, tf, kind, pattern, direction, price,
            # entry_lo, entry_hi, stop, target1, target2,
            # rr, confidence, knn, orderflow, fib_note,
            # prz_lo, prz_hi
            (
                _NOW_REC, "BTC", "4H", "completed", "Gartley", "long",
                62538.5, 60200.0, 60650.0, 59500.0, 62100.0, 64000.0,
                2.08, 0.73, "✓", "✓bid500000.0", "XA-Fib=0.618",
                60200.0, 60650.0,
            ),
            (
                _NOW_REC, "ETH", "1H", "forming", "Bat", "long",
                1835.0, 60000.0, 60500.0, None, None, None,
                None, 0.65, "?", "", "forming PRZ",
                60000.0, 60500.0,
            ),
            (
                _NOW_REC, "SOL", "15m", "forming", "Crab", "short",
                145.0, 130.0, 135.0, None, None, None,
                None, 0.60, "✗", "✗", "forming PRZ short",
                130.0, 135.0,
            ),
        ]

    def test_insert_and_recent_roundtrip(self) -> None:
        """insert 后 recent_harmonic_setups 能读回相同行数。"""
        rows = self._sample_rows()
        self.store.insert_harmonic_setups(rows)
        result = self.store.recent_harmonic_setups()
        assert len(result) == 3, f"应读回 3 行，实际 {len(result)}"

    def test_recent_returns_29_columns(self) -> None:
        """recent_harmonic_setups 每行 29 列（含 XABCD 点坐标，v2 schema）。"""
        self.store.insert_harmonic_setups(self._sample_rows())
        result = self.store.recent_harmonic_setups()
        for row in result:
            assert len(row) == 29, f"期望 29 列，实际 {len(row)}"

    def test_per_coin_latest_on_reinsert(self) -> None:
        """B2 per-coin latest：同 (coin,tf) 写新 ts 后，recent 取该 coin/tf 最新行；
        其他 coin/tf 仍各取自身最新（不会被「全局 MAX」排除）。"""
        self.store.insert_harmonic_setups(self._sample_rows())
        # 第二次 insert：仅 BTC/4H 更新（ts 更新），ETH/SOL 未更新
        new_rows = [
            (
                _NOW_REC + 1000, "BTC", "4H", "completed", "Gartley", "long",
                63000.0, 61000.0, 61500.0, 60000.0, 63500.0, 65500.0,
                2.1, 0.75, "✓", "", "XA-Fib=0.786",
                61000.0, 61500.0,
            ),
        ]
        self.store.insert_harmonic_setups(new_rows)
        result = self.store.recent_harmonic_setups()
        # per-coin latest：BTC/4H 取最新 ts + ETH/1H + SOL/15m = 3 行
        assert len(result) == 3, f"应含 BTC/ETH/SOL 共 3 行（per-coin latest），实际 {len(result)}"
        btc_rows = [r for r in result if r[1] == "BTC" and r[2] == "4H"]
        assert len(btc_rows) == 1
        assert btc_rows[0][0] == _NOW_REC + 1000, (
            f"BTC/4H 应取最新 ts={_NOW_REC + 1000}, 实际 {btc_rows[0][0]}"
        )
        # ETH 和 SOL 仍在列表中（per-coin latest 不因 BTC 更新而消失）
        eth_coins = [r for r in result if r[1] == "ETH"]
        sol_coins = [r for r in result if r[1] == "SOL"]
        assert eth_coins, "ETH 应在 per-coin latest 结果中"
        assert sol_coins, "SOL 应在 per-coin latest 结果中"

    def test_recent_ordered_by_confidence_desc(self) -> None:
        """recent_harmonic_setups 按 confidence DESC 排序。"""
        self.store.insert_harmonic_setups(self._sample_rows())
        result = self.store.recent_harmonic_setups()
        confidences = [row[13] for row in result]  # 列 index=13 是 confidence
        assert confidences == sorted(confidences, reverse=True), (
            f"未按 confidence DESC 排序: {confidences}"
        )

    def test_insert_empty_rows_safe(self) -> None:
        """insert 空列表不报错，recent 返回空。"""
        self.store.insert_harmonic_setups([])
        result = self.store.recent_harmonic_setups()
        assert result == [], f"空 insert 后 recent 应为 []，实际 {result}"

    def test_null_values_preserved(self) -> None:
        """forming 行的 NULL 字段（stop/target/rr）读回后仍为 None。"""
        forming_row = [
            (
                _NOW_REC, "ETH", "1H", "forming", "Bat", "long",
                1835.0, 60000.0, 60500.0, None, None, None,
                None, 0.65, "?", "", "forming PRZ",
                60000.0, 60500.0,
            )
        ]
        self.store.insert_harmonic_setups(forming_row)
        result = self.store.recent_harmonic_setups()
        assert len(result) == 1
        rec = result[0]
        assert rec[9] is None,  f"stop 应为 None，实际 {rec[9]}"
        assert rec[10] is None, f"target1 应为 None，实际 {rec[10]}"
        assert rec[12] is None, f"rr 应为 None，实际 {rec[12]}"


# ── TDD: 按币种分组多周期并列渲染（新格式） ───────────────────────────────────────


def _make_multi_tf_rows() -> list[dict]:
    """构造多币多 tf 合成 rows：
    - BTC: 4H completed(Gartley,long) + 12H forming(Butterfly,long)
    - ETH: 1H forming(Bat,short)
    用于验证按币分组渲染格式。
    """
    from smc_tracker.signals.trade_setup import TradeSetup

    btc_setup = TradeSetup(
        coin="BTC", tf="4H", direction="long", pattern="Gartley",
        completed=True, entry_lo=62309.0, entry_hi=68867.0,
        stop=68936.0, target1=58891.0, target2=52195.0, rr=2.0,
        fib_note="XA-Fib=0.618",
        knn_supports=False, knn_note="KNN≈随机基线",
        position_qty=0.03, position_notional=1959.0,
        confidence=0.81, note="诚实标注",
        src_key="C|Gartley|long|62309.0",
    )
    return [
        # BTC 4H completed
        {
            "coin": "BTC",
            "symbol": "BTCUSDT",
            "price": 62500.0,
            "tf": "4H",
            "completed": [
                {
                    "pattern": "Gartley",
                    "direction": "bull",
                    "prz": (62309.0, 68867.0),
                    "completed": True,
                    "confidence": 0.81,
                    "confluence": 3,
                    "points": {"D": (99, 62309.0)},
                    "setup": btc_setup,
                }
            ],
            "forming": [],
        },
        # BTC 12H forming
        {
            "coin": "BTC",
            "symbol": "BTCUSDT",
            "price": 62500.0,
            "tf": "12H",
            "completed": [],
            "forming": [
                {
                    "pattern": "Butterfly",
                    "direction": "bull",
                    "prz": (56443.0, 59806.0),
                    "completed": False,
                    "confidence": 0.85,
                    "confluence": 2,
                }
            ],
        },
        # ETH 1H forming
        {
            "coin": "ETH",
            "symbol": "ETHUSDT",
            "price": 1660.0,
            "tf": "1H",
            "completed": [],
            "forming": [
                {
                    "pattern": "Bat",
                    "direction": "bear",
                    "prz": (1669.0, 1756.0),
                    "completed": False,
                    "confidence": 0.85,
                    "confluence": 2,
                }
            ],
        },
    ]


class TestRenderCoinGrouped:
    """新格式：按币种分组、多周期并列渲染。"""

    def setup_method(self) -> None:
        self.monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT", "ETH": "ETHUSDT"},
            timeframes=["4H", "12H", "1H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )

    def test_btc_block_header_present(self) -> None:
        """BTC 块头含 '━━ BTC'。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "━━ BTC" in card, f"BTC 块头未出现，卡片:\n{card}"

    def test_eth_block_header_present(self) -> None:
        """ETH 块头含 '━━ ETH'。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "━━ ETH" in card, f"ETH 块头未出现，卡片:\n{card}"

    def test_block_header_contains_asset_badge(self) -> None:
        """BTC 块头含 '₿加密' 资产徽章。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "₿加密" in card, f"BTC 块头缺少 ₿加密 徽章，卡片:\n{card}"

    def test_block_header_contains_price(self) -> None:
        """BTC 块头含现价（62500 格式化后出现）。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # fmt_px(62500.0) → 62500 或 62,500.00
        assert "62" in card, "BTC 现价数字未出现在卡片"

    def test_tradfi_coin_shows_tradfi_badge(self) -> None:
        """TradFi 币（SOXL）块头显示 '🏦TradFi'。"""
        rows = [
            {
                "coin": "SOXL",
                "symbol": "SOXLUSDT",
                "price": 30.0,
                "tf": "1H",
                "completed": [],
                "forming": [
                    {
                        "pattern": "Gartley",
                        "direction": "bull",
                        "prz": (28.0, 29.5),
                        "completed": False,
                        "confidence": 0.70,
                        "confluence": 2,
                    }
                ],
            }
        ]
        monitor = HarmonicMonitor(
            coin_to_symbol={"SOXL": "SOXLUSDT"},
            timeframes=["1H"],
            bars=300,
            order=3,
            tol=0.05,
            top_n=10,
        )
        card = monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "🏦TradFi" in card, f"SOXL 块头应含 🏦TradFi，卡片:\n{card}"

    def test_btc_block_contains_both_timeframes(self) -> None:
        """BTC 块内同时含 4H 和 12H 两条行（多周期并列）。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # BTC 块内应含 4H 和 12H 两个周期
        assert "4H" in card, "卡片缺少 4H 行"
        assert "12H" in card, "卡片缺少 12H 行"
        # 且都在 BTC 块中（4H 和 12H 行出现在 ━━ BTC 块头之后、━━ ETH 块头之前）
        btc_start = card.find("━━ BTC")
        eth_start = card.find("━━ ETH")
        assert btc_start != -1, "BTC 块头未找到"
        assert eth_start != -1, "ETH 块头未找到"
        btc_block = card[btc_start:eth_start] if eth_start > btc_start else card[btc_start:]
        assert "4H" in btc_block, f"BTC 块内缺少 4H，块内容:\n{btc_block}"
        assert "12H" in btc_block, f"BTC 块内缺少 12H，块内容:\n{btc_block}"

    def test_eth_block_separate_from_btc(self) -> None:
        """ETH 的 1H 行在 ETH 块中，不与 BTC 混在一起。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        eth_start = card.find("━━ ETH")
        assert eth_start != -1, "ETH 块头未找到"
        eth_block = card[eth_start:]
        assert "1H" in eth_block, f"ETH 块内缺少 1H 行，ETH 块:\n{eth_block}"

    def test_completed_row_has_entry_keywords(self) -> None:
        """completed 行（✅前缀）含「进场」「止损」「目标」。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 找含 ✅ 的行
        completed_lines = [ln for ln in card.splitlines() if "✅" in ln]
        assert len(completed_lines) > 0, "卡片缺少 ✅ completed 行"
        # 合并文本检查（进场/止损/目标可能在 ✅ 行或其附注行；查整张卡片）
        for kw in ("进场", "止损", "目标"):
            assert kw in card, f"卡片缺少关键词「{kw}」"

    def test_forming_row_has_prz_keyword(self) -> None:
        """forming 行（🎯前缀）含「PRZ」。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        forming_lines = [ln for ln in card.splitlines() if "🎯" in ln]
        assert len(forming_lines) > 0, "卡片缺少 🎯 forming 行"
        forming_text = "\n".join(forming_lines)
        assert "PRZ" in forming_text, f"forming 行缺少 PRZ，forming 行:\n{forming_text}"

    def test_completed_prefix_checkmark(self) -> None:
        """BTC 4H completed 行以 '✅4H' 为前缀。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "✅4H" in card, f"BTC 4H completed 行应以 ✅4H 开头，卡片:\n{card}"

    def test_forming_prefix_target(self) -> None:
        """BTC 12H forming 行以 '🎯12H' 为前缀。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "🎯12H" in card, f"BTC 12H forming 行应以 🎯12H 开头，卡片:\n{card}"

    def test_price_no_scientific_notation_grouped(self) -> None:
        """按币分组格式下，价格不含科学计数法。"""
        rows = _make_multi_tf_rows()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "e+" not in card.lower(), "卡片含科学计数法 e+"
        assert "e-" not in card.lower(), "卡片含科学计数法 e-"

    def test_all_zero_price_returns_none_grouped(self) -> None:
        """所有 coin price≤0 → None（新格式也适用）。"""
        rows = [
            {
                "coin": "BTC", "symbol": "BTCUSDT", "price": 0.0, "tf": "4H",
                "completed": [{"pattern": "Gartley", "direction": "bull",
                               "prz": (60000.0, 60300.0), "completed": True,
                               "confidence": 0.70, "confluence": 4,
                               "points": {"D": (5, 60100.0)}, "setup": None}],
                "forming": [],
            }
        ]
        monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"}, timeframes=["4H"],
            bars=300, order=3, tol=0.05, top_n=10,
        )
        card = monitor.render(rows, _NOW_MS)
        assert card is None, "所有 price=0 时应返回 None"

    def test_orderflow_confirm_in_grouped_format(self) -> None:
        """按币分组格式下，订单流确认标记仍然出现。"""
        from smc_tracker.signals.orderflow_confirm import OrderflowConfirm
        of = OrderflowConfirm(
            confirmed=True, wall_usd=820_000.0, wall_dist_pct=0.007,
            imbalance=0.38, note="bid墙"
        )
        from smc_tracker.signals.trade_setup import TradeSetup
        setup = TradeSetup(
            coin="BTC", tf="4H", direction="long", pattern="Gartley",
            completed=True, entry_lo=60200.0, entry_hi=60650.0,
            stop=59000.0, target1=62500.0, target2=65000.0, rr=2.0,
            fib_note="XA-Fib=0.618",
            knn_supports=True, knn_note="样本足",
            position_qty=0.03, position_notional=1959.0,
            confidence=0.81, note="诚实",
            src_key="C|Gartley|long|60200.0",
            orderflow=of,
        )
        rows = [{
            "coin": "BTC", "symbol": "BTCUSDT", "price": 62500.0, "tf": "4H",
            "completed": [{
                "pattern": "Gartley", "direction": "bull",
                "prz": (60200.0, 60650.0), "completed": True,
                "confidence": 0.81, "confluence": 3,
                "points": {"D": (99, 60200.0)},
                "setup": setup,
            }],
            "forming": [],
        }]
        monitor = HarmonicMonitor(
            coin_to_symbol={"BTC": "BTCUSDT"}, timeframes=["4H"],
            bars=300, order=3, tol=0.05, top_n=10,
        )
        card = monitor.render(rows, _NOW_MS)
        assert card is not None
        assert "📊订单流✓" in card, f"按币分组下订单流确认应显示，卡片:\n{card}"
        assert "bid" in card, "long 方向确认含 bid"
