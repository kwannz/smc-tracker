# external/ —— Vendored 第三方代码(参考蓝本)

本目录是**全量 vendor 的第三方代码**,用户明确要求融合进 repo。

## freqtrade/

[freqtrade](https://github.com/freqtrade/freqtrade) 全量源码(`--depth 1` 浅克隆,已移除其 `.git`)。

**用途**:为本系统的**谐波交易机器人**(基于已验证的谐波 edge +0.5R/笔 #165 + 已有 `TradeSetup`)提供
成熟的加密量化**架构蓝本**:
- `freqtrade/strategy/interface.py` —— Strategy 接口(信号/执行分离:populate_entry/exit、minimal_roi、stoploss、custom_stoploss)
- `freqtrade/optimize/backtesting.py` —— 回测引擎(历史回放→模拟成交→绩效)
- dry-run / 纸面交易、绩效报告(胜率/盈亏比/最大回撤/Sharpe)

**重要约束声明(避免审计误判)**:
1. **豁免本项目规则**:本目录是 vendored 第三方,**不受**「每文件 ≤800 行 / 极致简短 / 最小依赖」约束
   (那些约束只适用于 `src/smc_tracker/` 我们自己的代码)。freqtrade 有 36 个文件 >800 行、依赖 pandas/ccxt/
   sqlalchemy/talib 等(本 venv 未装),是预期的——它是参考蓝本,不直接运行。
2. **测试隔离**:`pyproject.toml` 的 `testpaths=["tests"]` 已限定,本系统 `pytest` **不会**收集 freqtrade 的测试。
3. **keyless 不兼容**:freqtrade 内核(`freqtradebot.py`/`exchange/`/`persistence/`)围绕交易所 API key 实盘下单,
   与本系统「keyless 纯公开数据」约束对立——故**不直接运行 freqtrade**,而是在 `src/smc_tracker/` 内
   **借鉴其架构**造符合本仓库约束的精简谐波 bot(回测+dry-run,无实盘下单)。

简言之:freqtrade 在此作**架构参考**,谐波 bot 的实现仍在 `src/smc_tracker/`、遵守本项目全部铁律。
