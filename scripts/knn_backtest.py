#!/usr/bin/env python3
"""KNN 预测器历史回测：检验其方向预测性（用户要的"预测性"）。

方法（走查回测 walk-forward，无未来泄漏）：
  对 data/history 每个币：
    1. 读 CSV → 构造 Candle 列表；
    2. 用前 70% K 线 fit KNNPredictor(k=15, horizon=12)；
    3. 后 30% 逐根 predict（仅用截至该根的特征，标签来自训练集），
       与「该根之后 horizon 根的真实涨跌」比对；
    4. 统计方向准确率 vs base rate(50%)，并单独统计 confidence>0.6 的子集准确率。
  最后汇总跨币平均提升。诚实报告（很可能接近随机）。

运行：./.venv/bin/python scripts/knn_backtest.py
输出：终端表格 + data/history/knn_backtest.json
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smc_tracker.indicators import KNNPredictor, feature_matrix  # noqa: E402
from smc_tracker.models import Candle  # noqa: E402

HISTORY_DIR = ROOT / "data" / "history"
OUT_PATH = HISTORY_DIR / "knn_backtest.json"

K = 15
HORIZON = 12
TRAIN_FRAC = 0.70
CONF_THRESHOLD = 0.6      # "高置信"阈值
INTERVAL_MS = 3_600_000   # 1H


def load_candles(path: Path, coin: str) -> list[Candle]:
    """读历史 CSV（ts,open,high,low,close,base_vol,quote_vol）→ Candle 列表。"""
    candles: list[Candle] = []
    with path.open(encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # 跳过表头
        for row in reader:
            if len(row) < 7:
                continue
            try:
                ts = int(row[0])
                o, h, l, c = (float(row[1]), float(row[2]),
                              float(row[3]), float(row[4]))
                base_vol = float(row[5])
            except (ValueError, IndexError):
                continue
            candles.append(Candle(
                coin=coin, interval="1H",
                open_time_ms=ts, close_time_ms=ts + INTERVAL_MS,
                o=o, h=h, l=l, c=c, v=base_vol, n=0,
            ))
    return candles


def backtest_coin(coin: str, candles: list[Candle]) -> dict[str, Any] | None:
    """单币走查回测，返回统计字典；样本不足返回 None。"""
    n = len(candles)
    # 需要：训练段足够 fit，测试段每根都能看到未来 horizon。
    if n < 200:
        return None

    split = int(n * TRAIN_FRAC)
    train = candles[:split]

    predictor = KNNPredictor(k=K, horizon=HORIZON)
    if not predictor.fit(train):
        return None

    # 全段特征一次算好（feature_matrix 第 i 行只依赖截至 i 的数据，无泄漏）。
    feats = feature_matrix(candles)
    close = np.array([cd.c for cd in candles], dtype=float)

    total = 0          # 所有有效预测
    correct = 0
    up_label = 0       # 真实上涨数（用于真实 base rate）
    conf_total = 0     # confidence>0.6 子集
    conf_correct = 0
    long_pred = 0      # 预测 long 数（看是否有方向偏置）

    # 测试段：从 split 到 n-horizon-1（保证有未来真值）。
    for i in range(split, n - HORIZON):
        feat = feats[i]
        if not np.all(np.isfinite(feat)):
            continue
        out = predictor.predict(feat)
        if out is None:
            continue
        actual_up = close[i + HORIZON] > close[i]
        pred_up = out["direction"] == "long"
        hit = (pred_up == actual_up)

        total += 1
        up_label += int(actual_up)
        long_pred += int(pred_up)
        if hit:
            correct += 1
        if out["confidence"] > CONF_THRESHOLD:
            conf_total += 1
            if hit:
                conf_correct += 1

    if total == 0:
        return None

    acc = correct / total
    conf_acc = conf_correct / conf_total if conf_total else None
    return {
        "coin": coin,
        "n_candles": n,
        "train_n": split,
        "test_predictions": total,
        "accuracy": acc,
        "lift_vs_base": acc - 0.5,           # 相对 50% 随机的提升
        "true_up_rate": up_label / total,    # 真实上涨占比（真实 base rate）
        "long_pred_rate": long_pred / total, # 预测做多占比（方向偏置诊断）
        "high_conf_n": conf_total,
        "high_conf_accuracy": conf_acc,
        "high_conf_lift_vs_base": (conf_acc - 0.5) if conf_acc is not None else None,
    }


def fmt_pct(x: float | None) -> str:
    return "  n/a " if x is None else f"{x * 100:5.1f}%"


def fmt_signed(x: float | None) -> str:
    return "  n/a " if x is None else f"{x * 100:+5.1f}"


def main() -> int:
    files = sorted(HISTORY_DIR.glob("*_1H.csv"))
    if not files:
        print(f"未找到历史数据：{HISTORY_DIR}")
        return 1

    results: list[dict[str, Any]] = []
    for fp in files:
        coin = fp.stem.replace("_1H", "")
        candles = load_candles(fp, coin)
        res = backtest_coin(coin, candles)
        if res is not None:
            results.append(res)

    if not results:
        print("没有任何币产生有效回测结果。")
        return 1

    # ---- 表格 ----
    print("=" * 92)
    print(f"KNN 预测器走查回测  (k={K}, horizon={HORIZON}根, 训练比例={TRAIN_FRAC:.0%}, "
          f"高置信阈值 conf>{CONF_THRESHOLD})")
    print(f"base rate = 50%（多空二分类随机基线）。预测窗口 = 未来 {HORIZON} 根 1H K 线。")
    print("=" * 92)
    header = (f"{'币种':<10}{'测试样本':>8}{'方向准确率':>11}{'vs50%':>8}"
              f"{'真上涨率':>9}{'预测多率':>9}{'高置信样本':>10}{'高置信准确率':>13}{'vs50%':>8}")
    print(header)
    print("-" * 92)

    for r in sorted(results, key=lambda x: x["accuracy"], reverse=True):
        print(f"{r['coin']:<11}"
              f"{r['test_predictions']:>7}"
              f"{fmt_pct(r['accuracy']):>11}"
              f"{fmt_signed(r['lift_vs_base']):>8}"
              f"{fmt_pct(r['true_up_rate']):>10}"
              f"{fmt_pct(r['long_pred_rate']):>10}"
              f"{r['high_conf_n']:>9}"
              f"{fmt_pct(r['high_conf_accuracy']):>13}"
              f"{fmt_signed(r['high_conf_lift_vs_base']):>8}")

    # ---- 跨币汇总 ----
    accs = [r["accuracy"] for r in results]
    lifts = [r["lift_vs_base"] for r in results]
    conf_accs = [r["high_conf_accuracy"] for r in results
                 if r["high_conf_accuracy"] is not None]
    conf_lifts = [r["high_conf_lift_vs_base"] for r in results
                  if r["high_conf_lift_vs_base"] is not None]

    tot_pred = sum(r["test_predictions"] for r in results)
    # 加权（按样本数）总体准确率
    weighted_acc = sum(r["accuracy"] * r["test_predictions"] for r in results) / tot_pred
    tot_conf = sum(r["high_conf_n"] for r in results)
    weighted_conf_acc = (
        sum((r["high_conf_accuracy"] or 0.0) * r["high_conf_n"] for r in results) / tot_conf
        if tot_conf else None
    )

    mean_acc = float(np.mean(accs))
    mean_lift = float(np.mean(lifts))
    mean_conf_acc = float(np.mean(conf_accs)) if conf_accs else None
    mean_conf_lift = float(np.mean(conf_lifts)) if conf_lifts else None

    print("=" * 92)
    print(f"币种数={len(results)}  总测试样本={tot_pred}  高置信样本={tot_conf}")
    print(f"跨币平均方向准确率 = {mean_acc*100:5.2f}%  "
          f"(平均提升 {mean_lift*100:+.2f}pp vs 50%)")
    print(f"样本加权总体准确率 = {weighted_acc*100:5.2f}%  "
          f"(提升 {(weighted_acc-0.5)*100:+.2f}pp)")
    if mean_conf_acc is not None:
        print(f"高置信(conf>{CONF_THRESHOLD})跨币平均准确率 = {mean_conf_acc*100:5.2f}%  "
              f"(平均提升 {mean_conf_lift*100:+.2f}pp vs 50%)")
        if weighted_conf_acc is not None:
            print(f"高置信样本加权总体准确率 = {weighted_conf_acc*100:5.2f}%  "
                  f"(提升 {(weighted_conf_acc-0.5)*100:+.2f}pp)")
    else:
        print(f"高置信(conf>{CONF_THRESHOLD})：无足够样本。")
    print("=" * 92)
    print("诚实结论：方向准确率若仅在 50% 上下 1-3pp 浮动，说明 KNN 在该特征集上")
    print("对未来方向几乎无稳定预测性（接近随机），高置信子集亦未见可靠的系统性优势。")
    print("=" * 92)

    # ---- 落盘 ----
    summary = {
        "params": {"k": K, "horizon": HORIZON, "train_frac": TRAIN_FRAC,
                   "conf_threshold": CONF_THRESHOLD, "base_rate": 0.5},
        "n_coins": len(results),
        "total_test_predictions": tot_pred,
        "total_high_conf_predictions": tot_conf,
        "mean_accuracy": mean_acc,
        "mean_lift_vs_base": mean_lift,
        "weighted_accuracy": weighted_acc,
        "weighted_lift_vs_base": weighted_acc - 0.5,
        "high_conf_mean_accuracy": mean_conf_acc,
        "high_conf_mean_lift_vs_base": mean_conf_lift,
        "high_conf_weighted_accuracy": weighted_conf_acc,
        "per_coin": results,
    }
    OUT_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"已保存：{OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
