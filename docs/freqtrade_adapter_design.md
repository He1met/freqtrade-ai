# Freqtrade Adapter Design

## 目标

Freqtrade Adapter 是本项目访问 Freqtrade 的唯一边界。Service 层不直接调用 `subprocess`、不直接拼 Freqtrade 命令、不直接解析 Freqtrade 原始报告。

## 目录

```text
backend/app/adapters/freqtrade/
  cli_runner.py
  config_builder.py
  data_downloader.py
  backtest_runner.py
  result_parser.py
  strategy_file_manager.py
  webserver_runner.py
  rest_client.py
  runtime_manager.py
  exceptions.py
```

## 职责

- `cli_runner.py`: 统一执行 Freqtrade CLI。
- `config_builder.py`: 根据配置快照生成临时 Freqtrade 配置。
- `data_downloader.py`: 后续封装 `freqtrade download-data`。
- `backtest_runner.py`: 后续封装 `freqtrade backtesting`。
- `result_parser.py`: 解析 Freqtrade 回测 JSON。
- `strategy_file_manager.py`: 管理生成策略文件。
- `webserver_runner.py`: 后续封装 Freqtrade webserver。
- `rest_client.py`: 后续封装 Freqtrade REST API。
- `runtime_manager.py`: 后续管理本地 Freqtrade 运行态。

## Phase 0 状态

Phase 0 只创建边界和占位类，不执行真实 Freqtrade 命令。
