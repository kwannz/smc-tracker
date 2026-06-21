# round-002 — OKX 强平级联监控

## 结果: KEEP
- OKXSub inst_type(firehose) + _on_liquidation 聚合 + okx_liquidations 表 + 8 测试
- workflow(实现+对抗验证) + 主会话独立核验: 626 passed, 符号真 live, 落盘核验通过
- commit 核验四文件(monitor+62/ws+11/db+25/test+132)

## round-003(待开发): 强平级联消费
- 缺口(verify 标注): all_liquidations/recent_okx_liquidations 未被 signal/push 消费
- 设计: _on_liquidation 加级联检测(某coin某向强平越阈值→on_liquidation_signal)→ run_stream 展示+告警
- 诚实定位: 强平级联=告警(强制流向/潜在极值), 非确定方向预测, 不喂 confluence(方向语义模糊)
