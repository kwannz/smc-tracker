# Round 001 — ATR2 多维度汇合是否提升谐波前向胜率

目标:     量化「谐波 setup + ATR2 同向确认」的 causal 前向胜率 vs 单谐波基线(74.1%)。metric=胜率(RR=2)。
已知事实:  scripts/backtest_harmonic.py causal forward 单谐波 74.1%(201笔); HL方向~50%随机(已实证); 
          ATR2/BB 数值精确(round0 交叉验证 TA-Lib/Carney rtol1e-9); atr2_confirmation 已集成 trade_setup。
要验证假设: 谐波 forming/completed setup 入场点 ATR2 bias 与 setup 方向**同向** → 子集胜率 > 74%(单变量:加ATR2同向门槛)。
决策门槛:  keep if 同向子集胜率 ≥ 77% 且样本 ≥ 50; discard if ≤ 74% 或样本 < 30(无统计意义)。
执行边界:  只读 + scripts/round1_atr2_confluence.py 新建; **不改 src**(管线 agent 正改 app/collector/bb_monitor)。
验证命令:  PYTHONPATH=src ./.venv/bin/python scripts/round1_atr2_confluence.py
结果格式:  本文件追加「结果」节: 同向/反向/中性 各胜率+样本 + keep/discard 结论。

## 结果 (2026-06-24)
- ATR2同向: n=80 胜率 **82.5%** (≥77%门槛 ✓, ≥50样本 ✓)
- ATR2反向: n=34 胜率 50.0% (随机!)
- ATR2中性: n=108 胜率 75.9%
- 全部(无过滤): n=222 胜率 74.3% (=基线74.1%, 自洽)

## 判定: **KEEP**
ATR2 同向汇合 82.5% vs 反向 50% — 多维度(谐波×ATR2方向)是有效质量过滤器, 验证 trade_setup ATR2 集成方向正确。
可执行改进: 当前 trade_setup 反向仅 confidence×0.92(太轻); 数据显示反向=50%随机, 应重罚(如×0.8)或标"低质ATR2反向"。
诚实: 小样本(in-sample枢轴/无手续费滑点), 绝对值偏乐观; 但相对分离(同向82.5 vs 反向50)有信息量。
