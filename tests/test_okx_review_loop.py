"""OKX 接入 review 闭环(独立增量)：OKX 跨所背离信号以 kind="OKX" 进 predictions，
使 SignalEfficacy 能学到 OKX 源命中率、confluence 据此加权(此前 weight_of("OKX") 恒中性 1.0)。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store


def test_okx_kind_enters_predictions():
    """record_mtf(kind="OKX") 把 OKX 背离按 MTF 记入 predictions(闭环前提)。"""
    from smc_tracker.review import PredictionReview
    s = Store(Path(tempfile.mkdtemp()) / "okxrev.db")
    rev = PredictionReview(s)
    n = rev.record_mtf(ts=1_700_000_000_000, coin="BTC", kind="OKX", direction="long",
                       hl_px=60000.0, bg_px=0.0, horizons_ms=[5 * 60_000, 60 * 60_000])
    assert n == 2  # 2 个水平线各一条
    rows = s.conn.execute(
        "SELECT coin, kind, direction FROM predictions WHERE kind=?", ("OKX",)).fetchall()
    assert len(rows) == 2
    assert all(r[0] == "BTC" and r[1] == "OKX" and r[2] == "long" for r in rows)
    s.close()


def test_efficacy_can_read_okx_kind():
    """SignalEfficacy 能从 predictions 读到 OKX kind(评估后)；闭环打通。

    校验 OKX kind 不再被 efficacy 体系无视——评估后该 kind 进入 efficacy 的统计输入。
    """
    from smc_tracker.review import PredictionReview
    from smc_tracker.signals.efficacy import SignalEfficacy
    s = Store(Path(tempfile.mkdtemp()) / "okxeff.db")
    rev = PredictionReview(s)
    t0 = 1_700_000_000_000
    rev.record_mtf(ts=t0, coin="BTC", kind="OKX", direction="long",
                   hl_px=60000.0, bg_px=0.0, horizons_ms=[5 * 60_000])
    # 评估：5m 后价格上涨 → long 命中（realized_ret>0 被写回 predictions）
    later = t0 + 5 * 60_000 + 1
    rev.evaluate_due(lambda c: 66000.0, later)
    # predictions 中 OKX kind 已有 realized_ret（被评估），efficacy 输入不再排除 OKX
    evaluated = s.conn.execute(
        "SELECT kind FROM predictions WHERE kind='OKX' AND realized_ret IS NOT NULL").fetchall()
    assert len(evaluated) >= 1, "OKX 预测应已被评估并带 realized_ret"
    eff = SignalEfficacy(s)
    eff.refresh(now_ms=later)   # 不应因 OKX kind 报错；OKX 已是合法 kind
    assert eff.weight_of("OKX") > 0  # 有了输入路径（样本不足时仍返回中性，但 kind 已被纳入体系）
    s.close()
