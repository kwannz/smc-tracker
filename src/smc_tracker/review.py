"""预测正确性回顾/校准层：把每条前瞻推送连同发出时价格落库，
事后到期用真实价格核对方向对错，产出准确率回顾报告（诚实复盘与纠正）。

自管 SQLite 表（CREATE TABLE IF NOT EXISTS），参考 onchain/monitor.py 风格，不改 storage/db.py。
"""
from __future__ import annotations

from typing import Any, Callable

from .util import fmt_ts, to_float

# realized_ret 离群阈值：|ret|>10(=1000%) 几乎必为 emit/eval 单位错配/陈旧价，非真实行情（#98）。
_RET_OUTLIER = 10.0


# ---- 纯函数：市场中性命中率（横截面去均值，剔除趋势 beta） ----

def market_neutral_stats(
    records: list[tuple[int, str, float]],
    bucket_ms: int = 3_600_000,
) -> dict:
    """横截面去均值，计算剔除趋势 beta 后的纯 alpha 命中率。

    参数
    ----
    records    : [(ts_ms, direction, realized_ret), ...]
                 direction 取值：long / up（看多）或 short / down（看空）
    bucket_ms  : 时间桶宽度（ms），默认 1 小时；同桶内所有预测共享同期市场漂移均值

    算法
    ----
    1. 按 ts // bucket_ms 分桶，算每桶 realized_ret 均值 = 同期市场漂移（beta 近似）。
    2. 逐条：excess = realized_ret − 桶均值（横截面去均值）。
    3. 按方向判中性命中：long/up → excess > 0 命中；short/down → excess < 0 命中。
    4. 方向调整超额：sexc = excess if up else −excess（统一表示策略超额盈亏）。

    返回 dict
    ---------
    n          样本总数
    hits       中性命中数
    hit_rate   中性命中率（0.0~1.0）
    edge       hit_rate − 0.5（纯 alpha 边际）
    avg_excess 均方向调整超额收益（按预测方向，正值代表真正跑赢市场）

    注：空 records 安全返回零值，不抛异常（诚实复盘：零样本=无信息）。
    """
    if not records:
        return {"n": 0, "hits": 0, "hit_rate": 0.0, "edge": 0.0, "avg_excess": 0.0}

    # 1) 按时间桶分组，算每桶平均原始收益 = 同期市场漂移
    from collections import defaultdict
    bucket_rets: dict[int, list[float]] = defaultdict(list)
    for ts, _direction, rret in records:
        bucket_rets[int(ts) // bucket_ms].append(to_float(rret, 0.0))
    bucket_mean: dict[int, float] = {
        b: (sum(v) / len(v) if v else 0.0) for b, v in bucket_rets.items()
    }

    # 2) 逐条计算中性命中 + 超额收益
    hits = 0
    excess_sum = 0.0
    n = len(records)
    for ts, direction, rret in records:
        r = to_float(rret, 0.0)
        mkt = bucket_mean[int(ts) // bucket_ms]
        excess = r - mkt                              # 横截面去均值
        is_up = direction in ("long", "up")
        neu_hit = (excess > 0) if is_up else (excess < 0)
        sexc = excess if is_up else -excess           # 方向调整超额（策略超额盈亏）
        if neu_hit:
            hits += 1
        excess_sum += sexc

    hit_rate = hits / n if n > 0 else 0.0
    return {
        "n": n,
        "hits": hits,
        "hit_rate": hit_rate,
        "edge": hit_rate - 0.5,
        "avg_excess": excess_sum / n if n > 0 else 0.0,
    }

# ---- 自管建表 DDL ----
_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,       -- 发出时刻 epoch ms
    dt           TEXT    NOT NULL,       -- fmt_ts(ts) 完整可读时间(日期+时区)
    coin         TEXT    NOT NULL,
    kind         TEXT    NOT NULL,       -- 跟庄/前瞻/暴涨/共识/背离
    direction    TEXT    NOT NULL,       -- long/short/up/down
    px_emit      REAL    NOT NULL,       -- 发出时参考价
    hl_px        REAL,                   -- Hyperliquid 源价(交叉验证)
    bg_px        REAL,                   -- Bitget 源价(交叉验证)
    px_gap_pct   REAL,                   -- 两源价差比率 |hl-bg|/mid，数据正确性指标
    horizon_ms   INTEGER NOT NULL,       -- 评估水平线(ms)，默认 1 小时
    evaluated    INTEGER DEFAULT 0,      -- 0 未评 / 1 已评
    eval_ts      INTEGER,                -- 评估时刻 epoch ms
    eval_dt      TEXT,                   -- fmt_ts(eval_ts)
    px_eval      REAL,                   -- 评估时真实价格
    realized_ret REAL,                   -- (px_eval - px_emit) / px_emit
    correct      INTEGER,                -- 1 方向对 / 0 错 / NULL 未评
    note         TEXT                    -- 备注(可选)
);
CREATE INDEX IF NOT EXISTS ix_pred_eval_ts ON predictions(evaluated, ts);
CREATE INDEX IF NOT EXISTS ix_pred_kind    ON predictions(kind);
"""


class PredictionReview:
    """前瞻预测落库、事后评估、准确率聚合。

    参考 OnchainMemeMonitor 的自管表风格：
    - __init__ 中 executescript 建表，不依赖 storage/db.py。
    - record()        记录一条前瞻预测（发出时即调）。
    - evaluate_due()  到期评估（定期调，传入价格查询函数）。
    - accuracy_report() 产出聚合回顾报告 dict。
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        store.conn.executescript(_SCHEMA)

    # ---- 记录预测 ----
    def record(
        self,
        *,
        ts: int,
        coin: str,
        kind: str,
        direction: str,
        hl_px: float,
        bg_px: float,
        horizon_ms: int = 3_600_000,
        note: str = "",
    ) -> None:
        """记录一条前瞻预测到 predictions 表。

        px_emit：hl_px > 0 时用 hl_px，否则 bg_px；两者都 <= 0 则跳过，不记录。
        px_gap_pct：两源都 > 0 时计算 |hl-bg|/mid，否则 NULL（数据质量指标）。

        #98 关键修复：px_emit **必须与 evaluate_due 的 price_of「HL 优先」同源同单位**。
        此前 px_emit 优先 Bitget，而 price_of 优先 HL —— 对 HL 千倍计价币（kSHIB/kFLOKI 等，
        HL 价≈Bitget 原始价×1000）造成 emit/eval 单位错配，realized_ret 爆炸成 +1000(+10万%)，
        污染命中率统计（实测 SMC 平均 ret +347）。改 HL 优先后两端同源，realized_ret 正确。
        """
        hl = to_float(hl_px, 0.0)
        bg = to_float(bg_px, 0.0)

        # 发出时参考价：优先 HL（与 evaluate_due price_of 同源，保证 emit/eval 单位一致），回退 Bitget
        if hl > 0:
            px_emit = hl
        elif bg > 0:
            px_emit = bg
        else:
            return  # 无有效价格，不落库

        # 两源价差（数据质量指标）
        px_gap_pct: float | None = None
        if hl > 0 and bg > 0:
            mid = (hl + bg) / 2
            px_gap_pct = abs(hl - bg) / mid if mid > 0 else None

        dt = fmt_ts(ts)
        self.store.conn.execute(
            "INSERT INTO predictions"
            "(ts,dt,coin,kind,direction,px_emit,hl_px,bg_px,px_gap_pct,horizon_ms,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, dt, coin, kind, direction, px_emit,
             hl if hl > 0 else None,
             bg if bg > 0 else None,
             px_gap_pct, horizon_ms, note or None),
        )

    # ---- 多时间段批量记录 ----
    def record_mtf(
        self,
        *,
        ts: int,
        coin: str,
        kind: str,
        direction: str,
        hl_px: float,
        bg_px: float,
        horizons_ms: list[int],
        note: str = "",
    ) -> int:
        """在多个时间水平线各记一条预测（MTF 批量记录）。

        对 horizons_ms 中每个水平线调用 self.record()，复用单条记录逻辑（px_emit 选择、
        px_gap_pct 计算、无效价格跳过），不重复实现。

        返回实际记录的条数（无效价格时 record 内部跳过，总数 <= len(horizons_ms)）。
        """
        count = 0
        for hz in horizons_ms:
            before = self.store.conn.execute(
                "SELECT COUNT(*) FROM predictions"
            ).fetchone()[0]
            self.record(
                ts=ts, coin=coin, kind=kind, direction=direction,
                hl_px=hl_px, bg_px=bg_px, horizon_ms=hz, note=note,
            )
            after = self.store.conn.execute(
                "SELECT COUNT(*) FROM predictions"
            ).fetchone()[0]
            if after > before:
                count += 1
        return count

    # ---- 到期评估 ----
    def evaluate_due(
        self,
        price_of: Callable[[str], float | None],
        now_ms: int,
    ) -> int:
        """评估所有到期未评估的预测。

        price_of: coin -> 当前价格（None 或 <= 0 则跳过该条）。
        返回：本次实际评估的条数。
        """
        rows = self.store.conn.execute(
            "SELECT id, coin, direction, px_emit, ts, horizon_ms"
            " FROM predictions"
            " WHERE evaluated = 0 AND ? >= ts + horizon_ms",
            (now_ms,),
        ).fetchall()

        evaluated_count = 0
        for row_id, coin, direction, px_emit, ts, horizon_ms in rows:
            px = price_of(coin)
            px_f = to_float(px, 0.0) if px is not None else 0.0
            if px_f <= 0:
                continue  # 当前价格无效，跳过（留待下次）

            realized_ret = (px_f - px_emit) / px_emit if px_emit > 0 else 0.0

            # 方向正确性判断
            if direction in ("long", "up"):
                correct = 1 if realized_ret > 0 else 0
            elif direction in ("short", "down"):
                correct = 1 if realized_ret < 0 else 0
            else:
                correct = 0  # 未知方向，保守算错

            eval_dt = fmt_ts(now_ms)
            self.store.conn.execute(
                "UPDATE predictions SET"
                " evaluated=1, eval_ts=?, eval_dt=?, px_eval=?, realized_ret=?, correct=?"
                " WHERE id=?",
                (now_ms, eval_dt, px_f, realized_ret, correct, row_id),
            )
            evaluated_count += 1

        return evaluated_count

    # ---- 准确率聚合报告 ----
    def accuracy_report(self, since_ms: int, now_ms: int,
                        min_sample: int = 20) -> dict:
        """产出准确率聚合报告 dict。

        仅统计 evaluated=1 且 ts>=since_ms 的行（已评估的历史预测）。

        min_sample：判定样本是否足以下结论的阈值（默认 20）。方向预测的随机基线是 50%，
        样本太少时命中率噪声极大，必须诚实标注「仅供参考」而非夸大（CLAUDE.md:不夸大）。

        返回 dict 结构：
          total_n        总样本数
          total_hits     总命中数
          hit_rate       总体命中率（0.0~1.0）
          edge           相对随机基线的方向边际（hit_rate - 0.5）
          sufficient     样本是否达 min_sample（False 时结论仅供参考）
          min_sample     使用的样本充分性阈值
          avg_ret        平均「按预测方向」收益率（做空价格跌=盈利，符号已按 direction 调整；
                         避免做空为主的预测集 avg_ret 误导。原始 realized_ret 仍按原值入库）
          by_kind        {kind: {n, hits, hit_rate, edge, avg_ret}} 分类统计（avg_ret 同为按向收益）
          n_long/n_short 预测方向分布（base-rate 校正）
          avg_market_move 预测币种同期净市场漂移（方向无关原始价变动均值）
          beta_suspect   方向一边倒(≥80%)且市场同向漂移 → 边际或含趋势 beta 非纯 alpha
          gap_warn_count 两源价差 > 1% 的条数（数据质量告警）
          recent         最近 10 条 {dt,coin,kind,direction,realized_ret,correct}
        """
        rows = self.store.conn.execute(
            "SELECT ts, kind, correct, realized_ret, px_gap_pct, dt, coin, direction, horizon_ms"
            " FROM predictions"
            " WHERE evaluated=1 AND ts>=?",
            (since_ms,),
        ).fetchall()

        # 分类聚合
        by_kind: dict[str, dict] = {}
        by_horizon: dict[int, dict] = {}    # 按水平线分解(检验信号在哪个时间尺度有 alpha)
        # 每个水平线的 records，供各 TF 单独计算市场中性命中率
        hz_mn_records: dict[int, list[tuple[int, str, float]]] = {}
        total_n = 0
        total_hits = 0
        total_ret_sum = 0.0
        market_move_sum = 0.0      # 原始价变动累加(方向无关)=预测币种同期净市场漂移
        n_long = 0                 # 看多/看涨方向数
        n_short = 0                # 看空/看跌方向数
        gap_warn_count = 0
        outlier_count = 0          # |realized_ret|>1000% 的离群行(几乎必为单位错配/陈旧价)，剔除不计
        # 市场中性计算所需 records
        mn_records: list[tuple[int, str, float]] = []

        for ts_row, kind, correct, realized_ret, px_gap_pct, dt, coin, direction, horizon_ms in rows:
            # 数据质量守卫(#98)：|realized_ret|>1000% 几乎必是 emit/eval 单位错配（k 计价币历史脏数据）
            # 或陈旧价，绝非真实行情 → 剔除，避免污染命中率/avg_ret（真实 meme 极端波动远不及 10 倍）。
            if abs(to_float(realized_ret, 0.0)) > _RET_OUTLIER:
                outlier_count += 1
                continue
            total_n += 1
            if correct == 1:
                total_hits += 1
            # 方向调整收益(策略盈亏)：做空/看跌时价格下跌=盈利，故原始 ret 取负。
            # 原始 realized_ret 仍按原值入库；此处仅令 avg_ret 表「按预测方向的真实盈亏」，
            # 避免做空为主的预测集 avg_ret 符号误导(诚实标注)。
            ret = to_float(realized_ret, 0.0)
            is_up = direction in ("long", "up")
            sret = ret if is_up else -ret
            total_ret_sum += sret
            market_move_sum += ret             # 方向无关的市场漂移(base-rate 校正用)
            ts_int = int(to_float(ts_row, 0.0))
            mn_records.append((ts_int, direction, ret))  # 供总体市场中性计算
            if is_up:
                n_long += 1
            else:
                n_short += 1
            if px_gap_pct is not None and to_float(px_gap_pct, 0.0) > 0.01:
                gap_warn_count += 1
            if kind not in by_kind:
                by_kind[kind] = {"n": 0, "hits": 0, "ret_sum": 0.0}
            by_kind[kind]["n"] += 1
            if correct == 1:
                by_kind[kind]["hits"] += 1
            by_kind[kind]["ret_sum"] += sret
            # 按水平线聚合
            hz = int(to_float(horizon_ms, 0.0))
            if hz not in by_horizon:
                by_horizon[hz] = {"n": 0, "hits": 0, "ret_sum": 0.0}
                hz_mn_records[hz] = []
            by_horizon[hz]["n"] += 1
            if correct == 1:
                by_horizon[hz]["hits"] += 1
            by_horizon[hz]["ret_sum"] += sret
            hz_mn_records[hz].append((ts_int, direction, ret))

        # 整理分类统计（edge=命中率相对 50% 随机基线的方向边际）
        by_kind_out: dict[str, dict] = {}
        for kind, d in by_kind.items():
            n = d["n"]
            hits = d["hits"]
            hr = hits / n if n > 0 else 0.0
            by_kind_out[kind] = {
                "n": n,
                "hits": hits,
                "hit_rate": hr,
                "edge": hr - 0.5,
                "avg_ret": d["ret_sum"] / n if n > 0 else 0.0,
            }

        # 整理按水平线统计（检验信号在哪个时间尺度有 alpha）
        # 同时为每个 TF 计算市场中性命中率（横截面去均值，剔除趋势 beta 的纯 alpha）
        by_horizon_out: dict[int, dict] = {}
        by_horizon_mn_out: dict[int, dict] = {}
        for hz, d in by_horizon.items():
            n = d["n"]
            hits = d["hits"]
            hr = hits / n if n > 0 else 0.0
            by_horizon_out[hz] = {
                "n": n,
                "hits": hits,
                "hit_rate": hr,
                "edge": hr - 0.5,
                "avg_ret": d["ret_sum"] / n if n > 0 else 0.0,
            }
            # 各 TF 独立市场中性统计（bucket_ms 取该 TF 水平线自身宽度，适合横截面分析）
            by_horizon_mn_out[hz] = market_neutral_stats(
                hz_mn_records.get(hz, []), bucket_ms=max(hz, 3_600_000)
            )

        # 最近 10 条（按 ts desc）
        recent_rows = self.store.conn.execute(
            "SELECT dt, coin, kind, direction, realized_ret, correct"
            " FROM predictions"
            " WHERE evaluated=1 AND ts>=?"
            " ORDER BY ts DESC LIMIT 10",
            (since_ms,),
        ).fetchall()
        recent = [
            {
                "dt": r[0], "coin": r[1], "kind": r[2],
                "direction": r[3],
                "realized_ret": to_float(r[4], 0.0),                       # 原始价格变动
                # 方向调整收益(按预测方向的真实盈亏)：做空/看跌取负原始变动
                "strategy_ret": (to_float(r[4], 0.0)
                                 if r[3] in ("long", "up")
                                 else -to_float(r[4], 0.0)),
                "correct": r[5],
            }
            for r in recent_rows
        ]

        hit_rate = total_hits / total_n if total_n > 0 else 0.0
        # base-rate 校正：预测方向是否一边倒 + 同期净市场漂移。若预测一边倒且市场同向漂移，
        # 命中率的边际部分来自趋势 beta(下跌市做空什么都赢)，非纯选币 alpha → 诚实标注。
        avg_market_move = market_move_sum / total_n if total_n > 0 else 0.0
        dir_skew = max(n_long, n_short) / total_n if total_n > 0 else 0.0
        # 一边倒(同向≥80%)且市场同向漂移(漂移方向与多数预测方向一致) → 疑趋势 beta
        majority_short = n_short >= n_long
        market_favors_majority = (avg_market_move < 0) if majority_short else (avg_market_move > 0)
        beta_suspect = total_n > 0 and dir_skew >= 0.8 and market_favors_majority
        # 市场中性命中率：横截面去均值，剔除趋势 beta 后的纯 alpha 近似
        mn_stats = market_neutral_stats(mn_records)
        return {
            "total_n": total_n,
            "total_hits": total_hits,
            "hit_rate": hit_rate,
            "edge": hit_rate - 0.5,
            "sufficient": total_n >= min_sample,
            "min_sample": min_sample,
            "avg_ret": total_ret_sum / total_n if total_n > 0 else 0.0,
            "n_long": n_long,
            "n_short": n_short,
            "dir_skew": dir_skew,
            "avg_market_move": avg_market_move,
            "beta_suspect": beta_suspect,
            "by_kind": by_kind_out,
            "by_horizon": by_horizon_out,
            "by_horizon_market_neutral": by_horizon_mn_out,  # 各 TF 市场中性命中率（纯 alpha）
            "gap_warn_count": gap_warn_count,
            "outlier_count": outlier_count,   # 剔除的单位错配/陈旧离群行数（数据质量）
            "recent": recent,
            "market_neutral": mn_stats,    # 市场中性命中率（剔除趋势 beta 的纯 alpha）
        }


# ---- 文本摘要渲染 ----
def fmt_accuracy(report: dict) -> str:
    """把 accuracy_report() 返回的 dict 渲染成中文文本摘要，用于推送。"""
    total_n = report.get("total_n", 0)
    if total_n == 0:
        return "📊 预测准确率回顾\n样本不足，继续积累（尚无已到期评估记录）"

    lines: list[str] = ["📊 预测准确率回顾"]

    # 各 kind 命中率
    by_kind: dict = report.get("by_kind", {})
    if by_kind:
        lines.append("【分类命中率】")
        for kind, d in sorted(by_kind.items()):
            n = d["n"]
            rate = d["hit_rate"] * 100
            avg_r = d["avg_ret"] * 100
            sign = "+" if avg_r >= 0 else ""
            lines.append(
                f"  {kind}: {d['hits']}/{n} 命中 ({rate:.1f}%)  均收益{sign}{avg_r:.2f}%"
            )

    # 各水平线命中率（检验信号在哪个时间尺度有 alpha；庄持仓周期小时~天级）
    by_horizon: dict = report.get("by_horizon", {})
    by_horizon_mn: dict = report.get("by_horizon_market_neutral", {})
    if by_horizon:
        lines.append("【分水平线命中率(MTF alpha 诊断)】")
        for hz in sorted(by_horizon.keys(), key=lambda x: int(x)):
            d = by_horizon[hz]
            hz_int = int(hz)
            n = d["n"]
            # 友好 TF 标签：< 60min 用分钟，否则用小时
            hz_min = hz_int // 60_000
            if hz_min < 60:
                tf_label = f"{hz_min}m"
            else:
                tf_label = f"{hz_min // 60:g}h"
            rate = d["hit_rate"] * 100
            avg_r = d["avg_ret"] * 100
            sign = "+" if avg_r >= 0 else ""
            # 市场中性纯 alpha（若有）
            mn = by_horizon_mn.get(hz_int) or by_horizon_mn.get(hz)
            if mn and isinstance(mn, dict) and mn.get("n", 0) > 0:
                mn_edge = mn.get("edge", 0.0) * 100
                mn_str = f"  中性alpha{mn_edge:+.0f}pp"
            else:
                mn_str = ""
            # 样本不足诚实标注（<20 样本统计意义有限）
            insuf = "⚠️不足" if n < 20 else ""
            lines.append(
                f"  {tf_label}: {d['hits']}/{n} 命中 ({rate:.1f}%)  "
                f"均按向{sign}{avg_r:.2f}%{mn_str}  {insuf}"
            )

    # 总体命中率 + 相对随机(50%)基线的方向边际（诚实评估核心：胜在边际而非绝对值）
    hit_rate = report.get("hit_rate", 0.0) * 100
    avg_ret = report.get("avg_ret", 0.0) * 100
    sign = "+" if avg_ret >= 0 else ""
    edge_pp = report.get("edge", report.get("hit_rate", 0.0) - 0.5) * 100
    esign = "+" if edge_pp >= 0 else ""
    lines.append(
        f"【总体】样本{total_n}条  命中率{hit_rate:.1f}%  均按向收益{sign}{avg_ret:.2f}%"
    )
    lines.append(f"  相对随机(50%)边际 {esign}{edge_pp:.1f}pp（均按向收益=按预测方向真实盈亏）")
    # 市场中性命中率：剔除趋势 beta 后的纯 alpha，回退安全（旧 report 无此键时跳过）
    mn: dict | None = report.get("market_neutral")
    if mn is not None and isinstance(mn, dict):
        mn_n = mn.get("n", 0)
        mn_hit = mn.get("hit_rate", 0.0) * 100
        mn_edge = mn.get("edge", 0.0) * 100
        mn_ret = mn.get("avg_excess", 0.0) * 100
        mn_line = (
            f"  市场中性命中率 {mn_hit:.1f}%"
            f"（横截面去均值后纯 alpha，边际 {mn_edge:+.1f}pp，均超额 {mn_ret:+.2f}%）"
        )
        # 样本不足时诚实标注（不夸大）：总样本不足 OR 市场中性样本不足（两者一致但保险起见）
        min_sample_val = report.get("min_sample", 20)
        if total_n < min_sample_val or mn_n < min_sample_val:
            mn_line += "（样本不足，仅供参考）"
        lines.append(mn_line)

    # base-rate 校正：方向分布 + 同期净市场漂移，诚实区分趋势 beta 与选币 alpha
    n_long = report.get("n_long")
    n_short = report.get("n_short")
    if n_long is not None and n_short is not None and (n_long + n_short) > 0:
        mm = report.get("avg_market_move", 0.0) * 100
        msign = "+" if mm >= 0 else ""
        lines.append(f"  方向分布 {n_long}多/{n_short}空 · 同期净市场漂移 {msign}{mm:.2f}%")
        if report.get("beta_suspect"):
            lines.append("  ⚠️ 预测方向一边倒且市场同向漂移 → 边际或含趋势 beta(非纯选币 alpha)，谨慎归因")

    # 样本充分性：不足时诚实标注「仅供参考」，避免小样本噪声被当成 alpha（不夸大）
    if not report.get("sufficient", total_n >= report.get("min_sample", 20)):
        lines.append(
            f"  ⚠️ 样本不足({total_n}<{report.get('min_sample', 20)})，"
            "统计意义有限，结论仅供参考"
        )

    # 数据质量
    gap_warn = report.get("gap_warn_count", 0)
    if gap_warn > 0:
        lines.append(f"⚠️ 数据质量：{gap_warn} 条两源价差 >1%（HL/Bitget 价格偏差，谨慎参考）")

    # 最近样本
    recent: list[dict] = report.get("recent", [])
    if recent:
        lines.append("【最近样本】")
        for r in recent[:10]:
            ok = "✅" if r.get("correct") == 1 else "❌"
            # 按向收益(策略盈亏)；旧 rep 无 strategy_ret 时回退原始 realized_ret
            ret_pct = r.get("strategy_ret", r.get("realized_ret", 0.0)) * 100
            sign = "+" if ret_pct >= 0 else ""
            lines.append(
                f"  {ok} {r.get('dt','')}  {r.get('coin','')}  "
                f"{r.get('kind','')} {r.get('direction','')}  "
                f"按向{sign}{ret_pct:.2f}%"
            )

    return "\n".join(lines)
