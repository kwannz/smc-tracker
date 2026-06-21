# SMC 聪明钱追踪系统

基于 **Hyperliquid** 的低延迟实时系统：监控指定聪明钱/巨鲸地址的交易行为，
结合 **SMC (Smart Money Concepts)** 市场结构分析，生成交易信号。

## 特性
- ⚡ **低延迟**：asyncio + `websockets` + `orjson`，全程非阻塞；新鲜成交端到端延迟 ~230ms。
- 🐋 **聪明钱监控**：实时订阅 watchlist 地址成交，自动分类 **开/加/减/平/反手仓**，聚合净流向。
- 📐 **SMC 引擎**（开发中）：市场结构 BOS/CHoCH、订单块、FVG、流动性区、溢价折价区。
- 🔁 **稳健**：WS 自动重连 + 断线重订阅 + 心跳保活。

## 安装
```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## 配置
```bash
cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml，在 watchlist 填入真实聪明钱地址
```

## 运行
```bash
PYTHONPATH=src ./.venv/bin/python -m smc_tracker.main --config config/config.yaml
```

## 冒烟测试（验证 WS 实连）
```bash
./.venv/bin/python scripts/smoke_ws.py
```

## 测试
```bash
./.venv/bin/python -m pytest -q
```

## 项目结构
```
src/smc_tracker/
  config.py              # YAML 配置加载
  models.py              # Trade/Fill/Position/Candle 数据模型
  hyperliquid/
    ws_client.py         # 异步 WS 客户端（重连/心跳/重订阅）
    info_client.py       # REST Info 客户端（持仓/成交/K线）
    constants.py
  monitor/
    address_monitor.py   # 聪明钱地址监控 + 成交分类 + 净流向
    events.py            # SmartMoneyEvent 模型
  smc/                   # SMC 引擎（开发中）
  main.py                # 端到端入口
```

进度与路线图见 [PLAN.md](PLAN.md)。

> ⚠️ 仅供研究学习，非投资建议。链上跟单有滑点、抢跑、地址误判等风险。
