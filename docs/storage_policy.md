# Storage Policy

## PostgreSQL

PostgreSQL 只保存长期结构化数据：

- 策略主信息
- 策略版本
- 策略 JSON 蓝图
- 生成后的 Python 策略代码
- 策略生成批次
- 行情数据文件索引
- 回测批次
- 回测任务
- 回测结果
- 策略评分

## YAML

YAML 保存静态配置，包括大模型配置、交易所配置、回测币种、回测周期、回测窗口、回测方案、本地路径和默认参数。

YAML 不保存真实密钥。

## ENV

ENV 保存敏感信息：

- PostgreSQL 密码
- OKX API Key
- OKX API Secret
- OKX API Passphrase
- LLM API Key

真实 `.env` 不允许提交到仓库。

## 本地文件系统

本地文件系统保存大文件和运行产物：

- Freqtrade 行情数据文件
- Feather / Parquet 数据文件
- 生成后的策略 `.py` 文件
- Freqtrade 回测 JSON 报告
- 回测日志
- 系统运行日志

## 明确不保存

Phase 1 不保存 K 线明细到 PostgreSQL，不保存每笔交易明细，不保存真实 API Key，不保存实盘交易运行数据。
