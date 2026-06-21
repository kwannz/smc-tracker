"""市场中性化 alpha 审计（只读，第一性原理诚实诊断）。

问题：原始命中率会被趋势 beta 污染——下跌市里做空什么都赢，命中率高 ≠ 选币本事(alpha)。
方法：横截面去均值(cross-sectional demeaning，统计套利/Fama-French 业界标准)——
  对同一时间桶内所有预测，减去该桶的平均收益(=同期市场/板块漂移)，得到超额收益。
  再按预测方向判定「市场中性后是否仍对」→ 这才是剔除 beta 的纯 alpha 命中率。

只读 predictions 表，不写库、不改代码、不联网。用法：
  PYTHONPATH=src ./.venv/bin/python scripts/alpha_audit.py [--db data/smc.db] [--bucket-min 60]
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

# 市场中性核心算法复用 review.py，消除重复实现（CLAUDE.md §3 去重）
from smc_tracker.review import market_neutral_stats


def _f(x: object, default: float = 0.0) -> float:
    """安全转 float（数据质量，拒 None/脏值）。"""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN 守卫


def audit(db_path: str, bucket_min: int = 60) -> None:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT kind, direction, realized_ret, ts, correct, horizon_ms"
        " FROM predictions WHERE evaluated=1 AND realized_ret IS NOT NULL"
    ).fetchall()
    conn.close()

    if not rows:
        print("无已评估预测（predictions 表空或未评估），无法做 alpha 审计。")
        return

    bucket_ms = bucket_min * 60_000
    # 1) 按时间桶分组，算每桶平均原始收益 = 同期市场漂移（分 kind/horizon 仍需本地桶均值）
    buckets: dict[int, list[float]] = defaultdict(list)
    for _kind, _dir, rret, ts, _c, _hz in rows:
        buckets[int(ts) // bucket_ms].append(_f(rret))
    bucket_mean = {b: (sum(v) / len(v) if v else 0.0) for b, v in buckets.items()}

    # 2) 逐条：超额收益 = 原始 - 同桶均值；按方向调整为「策略盈亏」
    #    总体市场中性命中率由 review.market_neutral_stats 统一计算（去重）；
    #    分 kind / 分 horizon 的分层诊断是脚本特有逻辑，保留本地计算。
    raw_hits = raw_n = 0
    raw_dir_sum = 0.0           # 按向原始收益（含 beta）
    market_sum = 0.0
    by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # [n, raw_hits, neu_hits]
    by_horizon: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])  # [n, raw_hits, neu_hits]
    n_long = n_short = 0
    # 收集供 market_neutral_stats 用的 records（总体去均值复用 review 函数）
    mn_records: list[tuple[int, str, float]] = []

    for kind, direction, rret, ts, correct, horizon_ms in rows:
        r = _f(rret)
        mkt = bucket_mean[int(ts) // bucket_ms]
        excess = r - mkt                         # 横截面去均值 = 剔除同期市场漂移
        is_up = direction in ("long", "up")
        sret = r if is_up else -r                # 按预测方向的原始盈亏
        hz = int(_f(horizon_ms))
        raw_hit = 1 if ((is_up and r > 0) or ((not is_up) and r < 0)) else 0
        neu_hit = 1 if ((is_up and excess > 0) or ((not is_up) and excess < 0)) else 0
        raw_n += 1
        raw_dir_sum += sret
        market_sum += mkt
        raw_hits += raw_hit
        by_kind[kind][1] += raw_hit
        by_horizon[hz][1] += raw_hit
        neu_hits_kind = neu_hit  # 仅用于分类统计
        by_kind[kind][2] += neu_hit
        by_horizon[hz][2] += neu_hit
        by_kind[kind][0] += 1
        by_horizon[hz][0] += 1
        if is_up:
            n_long += 1
        else:
            n_short += 1
        mn_records.append((int(ts), direction, r))  # 供总体中性函数用

    # 总体市场中性命中率：复用 review.market_neutral_stats（唯一定义处）
    mn = market_neutral_stats(mn_records, bucket_ms=bucket_ms)
    neu_hits = mn["hits"]
    neu_n = mn["n"]
    neu_excess_sum = mn["avg_excess"] * neu_n if neu_n > 0 else 0.0

    def pct(h: int, n: int) -> float:
        return h / n * 100 if n else 0.0

    print(f"=== 市场中性化 alpha 审计（{raw_n} 条已评估 · 桶={bucket_min}min）===")
    print(f"方向分布: {n_long} 多/看涨, {n_short} 空/看跌"
          f"  (偏斜 {max(n_long, n_short) / raw_n * 100:.0f}%)")
    print(f"同期净市场漂移(原始均值): {market_sum / raw_n * 100:+.3f}%  "
          f"← 非 0 且方向与多数预测一致 = 命中率含趋势 beta")
    print()
    print(f"原始命中率(含 beta):    {raw_hits}/{raw_n} = {pct(raw_hits, raw_n):.1f}% "
          f"(边际 {pct(raw_hits, raw_n) - 50:+.1f}pp)  均按向收益 {raw_dir_sum / raw_n * 100:+.3f}%")
    print(f"市场中性命中率(纯alpha): {neu_hits}/{neu_n} = {pct(neu_hits, neu_n):.1f}% "
          f"(边际 {pct(neu_hits, neu_n) - 50:+.1f}pp)  均超额收益 {neu_excess_sum / neu_n * 100:+.3f}%")
    beta_share = pct(raw_hits, raw_n) - pct(neu_hits, neu_n)
    print(f"→ beta 贡献 ≈ {beta_share:+.1f}pp（原始 − 中性）；"
          f"中性后边际 {'仍为正(疑似真 alpha)' if pct(neu_hits, neu_n) > 52 else '≈随机(无纯选币 alpha)'}")
    print()
    print("分类(市场中性后):")
    for kind, (n, rh, nh) in sorted(by_kind.items(), key=lambda x: -x[1][0]):
        flag = "" if n >= 20 else "  ⚠️样本不足"
        print(f"  {kind:<4} n={n:<3} 原始{pct(rh, n):.0f}% → 中性{pct(nh, n):.0f}%{flag}")

    # 按 horizon 分层：检验信号在哪个时间尺度有 alpha（庄持仓周期小时~天级，#61 指出希望在 4h/24h）
    print("\n分 horizon(市场中性后)——找匹配庄持仓周期的有效尺度:")
    for hz, (n, rh, nh) in sorted(by_horizon.items()):
        hl = f"{hz / 3_600_000:g}h" if hz else "?"
        flag = "" if n >= 20 else "  ⚠️样本不足"
        edge = pct(nh, n) - 50
        verdict = ("←疑似真 alpha" if (n >= 20 and edge > 5)
                   else ("←≈随机" if n >= 20 else ""))
        print(f"  {hl:<5} n={n:<3} 原始{pct(rh, n):.0f}% → 中性{pct(nh, n):.0f}% "
              f"(边际{edge:+.0f}pp){flag} {verdict}")

    print("\n注：横截面去均值是市场中性的一阶近似（用同桶预测均值代理市场）。诚实标注：")
    print("    样本<20 的分类统计意义有限；中性后边际仍需更大样本与更长 horizon(4h/24h)确认。")


def main() -> None:
    ap = argparse.ArgumentParser(description="市场中性化 alpha 审计（只读）")
    root = Path(__file__).resolve().parents[1]
    ap.add_argument("--db", default=str(root / "data" / "smc.db"))
    ap.add_argument("--bucket-min", type=int, default=60,
                    help="市场漂移时间桶（分钟，默认 60）")
    args = ap.parse_args()
    audit(args.db, args.bucket_min)


if __name__ == "__main__":
    main()
