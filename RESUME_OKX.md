# OKX 期现基差集成 — 恢复检查点

## 关键背景（务必先读）
- 环境：`/Volumes/ROG ESD-S1C Media/smc`，非 git→已 git init 关联 GitHub `zijunzhao96/smc-smart-money-tracker`(私有)。
- **根因诊断**：后台 builder-agent 工作会被并发覆盖丢失(OKX 已丢3次)；**主会话直接写则持久**。
  → 全程主会话直接写，每块完成立即 `git add+commit+push` 锁定。**不要派后台 agent**。
- 基线：547 passed（GitHub a99cf2e 已备份）。
- OKX 实证可用(www.okx.com)：现货 ticker `/api/v5/market/ticker?instId=BTC-USDT`、永续 `BTC-USDT-SWAP`；
  data 在 `d['data'][0]`，价 `last`/时间 `ts`(ms)/`vol24h`。K线 `/api/v5/market/candles?instId=&bar=&limit=` 倒序需 reverse。
  bar：5m/15m/30m/1H/4H/12H/1D(分钟小写,时/天大写)。
- ⚠️ Binance(api/fapi)本环境 451 封锁，data-api.binance.vision 滞后685天，**不可用**。
- config/config.yaml 含真实密钥，已在 .gitignore（勿提交）。

## 进度
- [x] okx/__init__.py + okx/client.py（OKXClient: ticker/candles）— **已写+已提交+已push**
- [x] db.py: spot_basis 表（SCHEMA 内 flow_predictions 之后）— 已写(未单独提交)
- [x] db.py: insert_spot_basis 方法（在 insert_flow_prediction 之后）— 已写(未提交)
- [ ] monitor/spot_basis.py（新建）：compute_basis 纯函数 + SpotFuturesBasis.scan_okx + fmt
- [ ] monitor/__init__.py：导出 SpotFuturesBasis, compute_basis
- [ ] config.py：OKXCfg(base_url="https://www.okx.com")
- [ ] app.py _DB_RETAIN：加 ("spot_basis","ts",30*86400_000)
- [ ] cli.py：spot 子命令(打印 OKX 现货+永续+基差) + _cmd_cycle 接入 scan_okx
- [ ] tests/test_okx.py：OKXClient(fake session)+compute_basis(premium/discount/spot<=0返None)+scan_okx(fake注入) 单测,不联网
- [ ] 全量 pytest 全绿(≥547+新增) + 真实实证 `python -m smc_tracker spot --symbol BTC` + commit+push
- [ ] 整理 PLAN.md：诚实记录 #82-84 OKX/Binance 曾被竞态吞噬,现主会话重做

## spot_basis.py 设计
```
def compute_basis(spot_px, perp_px) -> dict|None:  # spot<=0 返None
    basis_pct=(perp-spot)/spot; direction='premium'(>0)/'discount'(<0)
    返回 {basis_pct,spot_px,perp_px,direction}; 用 util.to_float
class SpotFuturesBasis(store):
    async scan_okx(coins, now_ms, threshold=0.003):  # coins=['BTC','ETH','SOL']
        每coin: OKXClient ticker(f"{coin}-USDT") 现货 + ticker(f"{coin}-USDT-SWAP") 永续
        compute_basis → insert_spot_basis((now,coin,'OKX',spot,perp,basis_pct,direction))
        |basis_pct|>=threshold 纳入返回; Semaphore(6); 单coin失败跳过
    def fmt(row)->str  # "BTC 期现基差 -0.05% 永续折价🔴(现货64028/永续63995)"
```
验证命令：`cd "/Volumes/ROG ESD-S1C Media/smc" && PYTHONPATH=src ./.venv/bin/python -m pytest -q`
提交格式尾：`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
