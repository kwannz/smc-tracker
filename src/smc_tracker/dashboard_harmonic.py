"""谐波渲染层（扁平模块，从 dashboard.py 迁出）。

提供两个自包含 HTML 渲染函数：
  render_harmonic_html(state)            → str
  render_harmonic_detail_html(list_state) → str

HTML 模板已外置到 templates/ 目录（模块导入时一次性读入并缓存），
render 函数沿用 {{/}} 转义 + __INITIAL_STATE__ 注入模式。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .dashboard_common import _safe_rows, _row_to_dict

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


# 谐波 setups 列序（29 列，与表契约对齐）
_HARMONIC_KEYS = [
    "ts", "coin", "tf", "kind", "pattern", "direction", "price",
    "entry_lo", "entry_hi", "stop", "target1", "target2", "rr",
    "confidence", "knn", "orderflow", "fib_note", "prz_lo", "prz_hi",
    # XABCD 点坐标（v2 新增，forming 行为 None）
    "x_idx", "x_px", "a_idx", "a_px", "b_idx", "b_px",
    "c_idx", "c_px", "d_idx", "d_px",
]


def build_harmonic_state(store: Any, now_ms: int) -> dict:
    """从 store.conn 查询 harmonic_setups，分 completed/forming 两组返回 dict。

    每行含 asset_class 字段（'tradfi'/'crypto'），用于前端渲染徽章。
    表不存在/为空时各组返回 []，不抛异常（防御性查询）。
    """
    from .asset_class import asset_class as _asset_class  # 延迟导入，避免循环

    gen_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ms / 1000))

    rows = _safe_rows(
        store.conn,
        "SELECT ts,coin,tf,kind,pattern,direction,price,"
        "entry_lo,entry_hi,stop,target1,target2,rr,"
        "confidence,knn,orderflow,fib_note,prz_lo,prz_hi,"
        "x_idx,x_px,a_idx,a_px,b_idx,b_px,c_idx,c_px,d_idx,d_px "
        "FROM harmonic_setups ORDER BY confidence DESC",
    )

    completed: list[dict] = []
    forming: list[dict] = []
    for r in rows:
        d = _row_to_dict(r, _HARMONIC_KEYS)
        # 注入资产类别（TradFi/加密），供前端显示徽章
        d["asset_class"] = _asset_class(d.get("coin") or "")
        if d.get("kind") == "completed":
            completed.append(d)
        else:
            forming.append(d)

    return {
        "completed": completed,
        "forming": forming,
        "generated_at": gen_str,
    }


def build_harmonic_list(store: Any) -> list[dict]:
    """聚合 recent_harmonic_setups → 每币一条汇总行，按 best_conf 降序。

    返回字段：coin, asset_class, best_conf, direction, n_setups, has_completed, ts。
    ts=该币最新 setup 计算时刻（供前端显示真实"数据时间/数据年龄"，而非浏览器时钟）。
    表缺/空时返回 []，不抛。
    """
    from .asset_class import asset_class as _asset_class

    try:
        rows = store.recent_harmonic_setups()
    except Exception:  # noqa: BLE001
        return []

    # 按 coin 聚合
    agg: dict[str, dict] = {}
    for r in rows:
        d = _row_to_dict(r, _HARMONIC_KEYS)
        coin = d.get("coin") or ""
        if coin not in agg:
            agg[coin] = {
                "coin": coin,
                "asset_class": _asset_class(coin),
                "best_conf": None,
                "direction": None,
                "n_setups": 0,
                "has_completed": False,
                "ts": None,
            }
        entry = agg[coin]
        entry["n_setups"] += 1
        conf = d.get("confidence")
        if conf is not None:
            if entry["best_conf"] is None or conf > entry["best_conf"]:
                entry["best_conf"] = conf
                entry["direction"] = d.get("direction")
        if d.get("kind") == "completed":
            entry["has_completed"] = True
        # 跟踪该币最新 setup ts（数据新鲜度）
        ts = d.get("ts")
        if ts is not None and (entry["ts"] is None or ts > entry["ts"]):
            entry["ts"] = ts

    # 按 best_conf 降序（None 排最后）
    result = list(agg.values())
    result.sort(key=lambda x: (x["best_conf"] is None, -(x["best_conf"] or 0)))
    return result


def _knn_note_from_flag(knn_flag: str | None) -> str:
    """把 DB knn 列（'✓'/'✗'/'?'/None）映射为友好说明文字。

    注：KNN 命中率实测 ≈50%（随机基线），诚实标注，不伪造概率。
    """
    if knn_flag == "✓":
        return "找到历史相似态（注：KNN≈随机基线，仅辅助参考）"
    if knn_flag == "✗":
        return "历史无相似态（注：KNN≈随机基线，仅辅助参考）"
    return "样本不足或未计算（KNN≈随机基线，不可单独依赖）"


def _prz_proximity(price: float | None, prz_lo: float | None, prz_hi: float | None,
                   is_completed: bool = False) -> str:
    """描述当前价格相对 PRZ 区间的位置（前瞻信号强度指示）。

    返回中文描述字符串，用于"前瞻接近度"展示。价格/PRZ 缺失时返回 '—'。
    util.to_float(None) 返回 0.0 而非 None，故先检查原始值是否为 None。

    is_completed=True（D点已发生的 completed 形态）用回顾语义，不说"前瞻等待"——
    completed 的 D 点已反应过 PRZ，当前价格只是反应后的位置，说"前瞻等待"语义矛盾。
    forming（默认）保持前瞻语义（D 未到，等价格逼近 PRZ 是真前瞻提前量）。
    """
    from smc_tracker.util import to_float as _to_float
    if price is None or prz_lo is None or prz_hi is None:
        return "—"
    p = _to_float(price)
    lo = _to_float(prz_lo)
    hi = _to_float(prz_hi)
    if p is None or lo is None or hi is None:
        return "—"
    if lo > hi:
        lo, hi = hi, lo
    span = hi - lo
    if span <= 0:
        return "—"
    mid = (lo + hi) / 2
    if mid <= 0 or p <= 0:
        return "—"
    dist_pct = abs(p - mid) / mid * 100
    if lo <= p <= hi:
        zone = "D点反应区" if is_completed else "⚡ 距中轴"
        return f"价格在 PRZ 内（{zone} {dist_pct:.1f}%）"
    elif p < lo:
        gap_pct = (lo - p) / p * 100
        tail = "D点已反应，现价回落" if is_completed else "尚未触及，前瞻等待"
        return f"价格低于 PRZ {gap_pct:.1f}%（{tail}）"
    else:
        gap_pct = (p - hi) / p * 100
        tail = "D点已反应，现价上行" if is_completed else "已突破 PRZ 上沿"
        return f"价格高于 PRZ {gap_pct:.1f}%（{tail}）"


def _compute_confluence(all_setups: list[dict]) -> list[dict]:
    """检测跨 TF PRZ 区间重叠（多周期共振）——前瞻强化信号。

    算法：枚举所有 TF pair，两个 setup 的 [prz_lo, prz_hi] 有非空交集
    且方向一致 → 共振。返回共振列表，每项含：
      tf_a, tf_b, direction, overlap_lo, overlap_hi, kind_a, kind_b

    业界 multi-TF confluence 标准：多周期在同价区均有反转意愿 = 更高确定性。
    共振 forming 优于共振 completed（forming 是前瞻信号）。
    """
    from smc_tracker.util import to_float as _to_float
    results: list[dict] = []
    seen: set[tuple] = set()
    for i, a in enumerate(all_setups):
        tf_a = a.get("tf") or ""
        dir_a = a.get("direction") or ""
        raw_lo_a = a.get("prz_lo")
        raw_hi_a = a.get("prz_hi")
        # util.to_float(None)=0.0 不是 None，须先检查原始值
        if raw_lo_a is None or raw_hi_a is None or not dir_a:
            continue
        lo_a = _to_float(raw_lo_a)
        hi_a = _to_float(raw_hi_a)
        if lo_a is None or hi_a is None:
            continue
        if lo_a > hi_a:
            lo_a, hi_a = hi_a, lo_a
        for b in all_setups[i + 1:]:
            tf_b = b.get("tf") or ""
            if tf_b == tf_a:
                continue
            dir_b = b.get("direction") or ""
            if dir_b != dir_a:
                continue
            raw_lo_b = b.get("prz_lo")
            raw_hi_b = b.get("prz_hi")
            if raw_lo_b is None or raw_hi_b is None:
                continue
            lo_b = _to_float(raw_lo_b)
            hi_b = _to_float(raw_hi_b)
            if lo_b is None or hi_b is None:
                continue
            if lo_b > hi_b:
                lo_b, hi_b = hi_b, lo_b
            overlap_lo = max(lo_a, lo_b)
            overlap_hi = min(hi_a, hi_b)
            if overlap_lo > overlap_hi:
                continue
            key = tuple(sorted([tf_a, tf_b]) + [dir_a])
            if key in seen:
                continue
            seen.add(key)
            kind_a = a.get("kind") or "—"
            kind_b = b.get("kind") or "—"
            fwd_count = sum(1 for k in (kind_a, kind_b) if k == "forming")
            results.append({
                "tf_a": tf_a,
                "tf_b": tf_b,
                "direction": dir_a,
                "overlap_lo": round(overlap_lo, 6),
                "overlap_hi": round(overlap_hi, 6),
                "kind_a": kind_a,
                "kind_b": kind_b,
                "fwd_count": fwd_count,
            })
    results.sort(key=lambda x: x["fwd_count"], reverse=True)
    return results


def _enrich_setup(d: dict, current_price: float | None) -> dict:
    """补充 setup dict 的派生展示字段（纯函数，不改原始字段）。

    新增字段：
      knn_note   — 由 knn 旗标派生的友好说明
      honest_label — completed=回顾型/forming=前瞻预警
      prz_proximity — 当前价格 vs PRZ 位置描述（前瞻接近度）
    """
    d = dict(d)
    d["knn_note"] = _knn_note_from_flag(d.get("knn"))
    kind = d.get("kind") or ""
    if kind == "completed":
        d["honest_label"] = "completed（回顾型：D点已发生，反应式信号）"
    elif kind == "forming":
        d["honest_label"] = "forming（前瞻预警：XABCD 成形中，D点尚未到达）"
    else:
        d["honest_label"] = "—"
    price = current_price if current_price is not None else d.get("price")
    d["prz_proximity"] = _prz_proximity(
        price, d.get("prz_lo"), d.get("prz_hi"), is_completed=(kind == "completed"))
    # 交易计划诚实标注：有 PRZ 但无 entry = build_setups 因止损距离(X点失效位)超合理阈值
    # 诚实跳过(不产劣质 setup，trade_setup.py §238)。网页据此显示原因而非困惑的空白 —。
    if d.get("entry_lo") is None and d.get("prz_lo") is not None:
        d["plan_note"] = "⚠️ 止损距离超合理阈值，未生成交易计划（诚实跳过劣质 setup）"
    else:
        d["plan_note"] = ""
    return d


def build_coin_detail(store: Any, coin: str, tf: str | None = None) -> dict:
    """组装指定 coin（和 tf）的详情数据：蜡烛/setup/S/R/历史/多周期共振。

    tf 缺省时取该币在 recent_harmonic_setups 中首个 setup 的 tf。
    tfs_available 固定返回 CANONICAL_TIMEFRAMES 7 周期（15m/1H/4H/6H/12H/1D/1W），无论该周期是否有形态。
    无形态周期的 setups=[]，candles 仍尝试拉取（让前端显示 K 线）。
    表缺/空时各字段返回 []，不抛。

    新增字段：
      setups[].knn_note      — KNN 旗标友好说明（从 knn 列派生）
      setups[].honest_label  — 形态类型诚实标注（completed=回顾/forming=前瞻）
      setups[].prz_proximity — 当前价格相对 PRZ 位置（前瞻接近度描述）
      confluence             — 多周期 PRZ 共振列表（前瞻强化信号）
    """
    from .asset_class import asset_class as _asset_class

    # 固定 7 周期 tab（统一 CANONICAL_TIMEFRAMES，前端始终显示完整周期导航）
    from .config import CANONICAL_TIMEFRAMES as _FIXED_TFS  # noqa: PLC0415

    # 1. 读该币全部最新 setup 行（所有 tf）
    all_setups: list[dict] = []
    first_setup_tf: str = ""
    try:
        for r in store.recent_harmonic_setups():
            d = _row_to_dict(r, _HARMONIC_KEYS)
            if d.get("coin") != coin:
                continue
            d["asset_class"] = _asset_class(coin)
            all_setups.append(d)
            if not first_setup_tf:
                first_setup_tf = d.get("tf") or ""
    except Exception:  # noqa: BLE001
        pass

    # tf 缺省 → 用该币第一个 setup 的 tf；若无 setup，取固定列表第一个
    resolved_tf: str = tf or first_setup_tf or _FIXED_TFS[0]

    # 只保留目标 tf 的 setup
    setups_raw = [d for d in all_setups if d.get("tf") == resolved_tf]

    # 2. 蜡烛（200 根）——无形态的周期也拉（K 线仍有意义）
    candles: list[list] = []
    try:
        raw_candles = store.get_candles(coin, resolved_tf, 200)
        candles = [
            [c.open_time_ms, c.o, c.h, c.l, c.c, c.v]
            for c in raw_candles
        ]
    except Exception:  # noqa: BLE001
        pass

    # 当前价格（最新蜡烛收盘，供 prz_proximity 计算）
    current_price: float | None = None
    if candles:
        try:
            current_price = float(candles[-1][4])
        except (IndexError, TypeError, ValueError):
            pass

    # setup 字段丰富化（补 knn_note / honest_label / prz_proximity）
    setups = [_enrich_setup(d, current_price) for d in setups_raw]

    # 3. S/R（该币所有 tf 的最新 bb_levels）
    sr: list[dict] = []
    try:
        for r in store.recent_bb_levels(coin):
            sr.append({
                "tf":      r[1],
                "upper":   r[3],
                "lower":   r[5],
                "pct_b":   r[6],
                "squeeze": r[7],
            })
    except Exception:  # noqa: BLE001
        pass

    # 4. 历史形态
    history: list[dict] = []
    try:
        for r in store.harmonic_history(coin, 30):
            d = _row_to_dict(r, _HARMONIC_KEYS)
            d["asset_class"] = _asset_class(coin)
            history.append(d)
    except Exception:  # noqa: BLE001
        pass

    # 5. 多周期 PRZ 共振（跨所有 tf setups，前瞻强化）
    confluence: list[dict] = _compute_confluence(all_setups)

    return {
        "coin": coin,
        "asset_class": _asset_class(coin),
        "tf": resolved_tf,
        # 固定 7 周期 tab，不受「是否有形态」影响（前端按此列表渲染完整导航）
        "tfs_available": _FIXED_TFS,
        "candles": candles,
        "setups": setups,
        "sr": sr,
        "history": history,
        "confluence": confluence,
    }
