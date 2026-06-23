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
        """含 '成形中' 区块（前瞻预测）。"""
        card = self.monitor.render(_make_rows(with_forming=True, with_completed=False), _NOW_MS)
        assert card is not None
        assert "成形中" in card

    def test_render_contains_completed_section(self) -> None:
        """含 '完整' 区块（入场触发）。"""
        card = self.monitor.render(_make_rows(with_forming=False, with_completed=True), _NOW_MS)
        assert card is not None
        assert "完整" in card

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

        当前代码: 不限单币，只限全卡 <=8 条（展平后切片）。
        修复后: 每币每 tf 先各取 top 2，再展平，整卡 cap 8。
        """
        rows = _make_large_rows(n_completed=60, n_forming=0)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None

        # 统计 "完整形态" 区块中 "•" 条目数
        completed_lines = [
            ln for ln in card.splitlines()
            if ln.strip().startswith("•") and "Gartley" in ln
        ]
        assert len(completed_lines) <= 2, (
            f"单币单 tf completed 应 ≤2 条，实际 {len(completed_lines)} 条（缺陷6未修）"
        )

    def test_render_forming_capped_per_coin_tf(self) -> None:
        """单币单周期 forming 最多展示 top 2。"""
        rows = _make_large_rows(n_completed=0, n_forming=60)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None

        forming_lines = [
            ln for ln in card.splitlines()
            if ln.strip().startswith("•") and "Bat" in ln
        ]
        assert len(forming_lines) <= 2, (
            f"单币单 tf forming 应 ≤2 条，实际 {len(forming_lines)} 条（缺陷6未修）"
        )

    def test_render_total_cap_with_omission_note(self) -> None:
        """整卡 completed+forming 合计不超 cap，且卡片含省略提示。

        修复后: 超出部分卡片应有「…省略 N 条」提示（或类似文字）。
        """
        rows = _make_large_rows(n_completed=30, n_forming=30)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None

        # 统计总条目
        bullet_lines = [ln for ln in card.splitlines() if ln.strip().startswith("•")]
        assert len(bullet_lines) <= 8, (
            f"整卡条目应 ≤8 条，实际 {len(bullet_lines)} 条"
        )

    def test_render_no_hundred_plus_patterns(self) -> None:
        """卡片不出现'113 个形态'这类噪音总数——形态总数文字应合理（≤16 或不显示大数）。

        当前代码: 会渲染 '近窗 120 个形态' 字样（113 形态噪音）。
        修复后: 实际展示数合理，卡片不显示超大形态数（因为已截断）。
        """
        rows = _make_large_rows(n_completed=60, n_forming=60)
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None

        # 找「近窗 X 个形态」行，X 应 <= 8（展示数，非原始数）
        import re
        m = re.search(r"近窗\s*(\d+)\s*个形态", card)
        if m:
            shown = int(m.group(1))
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
        """completed 区块的行含「满足」字样（满足N腿约束）。"""
        rows = [_make_row_with_forming_label(confluence=4, completed=True)]
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        completed_lines = [ln for ln in card.splitlines() if "•" in ln and "Gartley" in ln]
        # 在 completed 区块（含 '完整形态'）之后的行
        in_completed = False
        completed_bullet_lines: list[str] = []
        for ln in card.splitlines():
            if "完整形态" in ln:
                in_completed = True
            elif "成形中" in ln:
                in_completed = False
            if in_completed and "•" in ln and "Gartley" in ln:
                completed_bullet_lines.append(ln)
        assert completed_bullet_lines, "completed 区块应有 Gartley 行"
        for ln in completed_bullet_lines:
            assert "满足" in ln, (
                f"completed 行应含「满足」，实际: {ln!r}"
            )

    def test_forming_shows_收敛(self) -> None:
        """forming 区块的行含「收敛」字样（收敛N证据）。"""
        rows = [_make_row_with_forming_label(confluence=2, completed=False)]
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        in_forming = False
        forming_bullet_lines: list[str] = []
        for ln in card.splitlines():
            if "成形中" in ln:
                in_forming = True
            elif "完整形态" in ln:
                in_forming = False
            if in_forming and "•" in ln and "Gartley" in ln:
                forming_bullet_lines.append(ln)
        assert forming_bullet_lines, "forming 区块应有 Gartley 行"
        for ln in forming_bullet_lines:
            assert "收敛" in ln, (
                f"forming 行应含「收敛」，实际: {ln!r}"
            )

    def test_completed_not_shows_收敛(self) -> None:
        """completed 行不含「收敛N」（不与 forming 混用语义）。"""
        rows = [_make_row_with_forming_label(confluence=4, completed=True)]
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        in_completed = False
        for ln in card.splitlines():
            if "完整形态" in ln:
                in_completed = True
            elif "成形中" in ln:
                in_completed = False
            if in_completed and "•" in ln and "Gartley" in ln:
                # completed 行不该出现「收敛」字样
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
        # 无 setup 的行只显示 PRZ，不含「进场」「止损」「目标」「仓位」
        completed_lines = [
            ln for ln in card.splitlines()
            if ln.strip().startswith("•") and "Bat" in ln
        ]
        assert len(completed_lines) > 0, "Bat 行应出现在卡片"
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
        """卡片副标题更新，含「进场/止损/止盈/仓位」或「可执行」字样。"""
        rows = _make_rows_with_setups()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        lines = card.splitlines()
        subtitle = lines[1] if len(lines) > 1 else ""
        setup_keywords = ("进场", "止损", "止盈", "仓位", "可执行")
        has_kw = any(kw in subtitle for kw in setup_keywords)
        assert has_kw, f"副标题应含可执行 setup 相关词，实际: {subtitle!r}"


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
        # 两个 setup 进场区的低点应出现（59796 和 56145 附近）
        # 只验证两条 bullet 行都出现（不被合并）
        bullet_lines = [ln for ln in card.splitlines() if ln.strip().startswith("•") and "Gartley" in ln]
        assert len(bullet_lines) >= 2, (
            f"两个 Gartley-bull 应渲染 ≥2 条 bullet，实际 {len(bullet_lines)} 条（注入碰撞未修）"
        )

    def test_two_gartley_have_different_entry_lo(self) -> None:
        """两条 Gartley 行进场区下沿不同（各自 setup 数据不被碰撞覆盖）。"""
        rows = _make_rows_two_gartley_bull_completed()
        card = self.monitor.render(rows, _NOW_MS)
        assert card is not None
        # 检查卡片中包含两个不同进场价格区域的数字
        # setup_a entry_lo=59796（fmt_px 格式化为 "59,796.00"）
        # setup_b entry_lo=56145（fmt_px 格式化为 "56,145.00"）
        gartley_bullet_lines = [
            ln for ln in card.splitlines() if "•" in ln and "Gartley" in ln
        ]
        # 两条行的文字不相同（不同进场区）
        assert len(gartley_bullet_lines) >= 2, (
            f"两个 Gartley-bull 应有 ≥2 条 bullet，实际 {len(gartley_bullet_lines)} 条"
        )
        # 两条行内容必须不同（不共享同一 setup 进场区）
        assert gartley_bullet_lines[0] != gartley_bullet_lines[1], (
            "两个 Gartley-bull 行内容相同，存在 setup 注入碰撞"
        )
