# Project Architecture

## 项目定位

Freqtrade AI 是建立在 Freqtrade 外层的 AI 策略研发管理系统。它不替代 Freqtrade，也不修改 Freqtrade 源码。

Freqtrade 负责行情数据下载、策略加载、回测执行、Hyperopt 参数优化、Dry-run / Live 运行、FreqUI、REST API、Telegram 和交易所接入。

本项目负责 AI 策略生成、策略 JSON 蓝图管理、策略代码生成、策略版本管理、批量回测任务管理、回测结果解析归档、策略评分排行榜、项目进度管理和 FreqUI 快捷入口。

## Phase 0 架构

```text
frontend/              React + Vite + TypeScript
backend/               FastAPI + SQLAlchemy + Pydantic
backend/app/adapters/  Freqtrade Adapter 边界
config/                YAML 静态配置
db/migrations/         PostgreSQL 迁移
user_data/             Freqtrade 本地数据和生成策略
reports/               回测报告归档
logs/                  系统日志
tmp/                   临时 Freqtrade 配置
```

## 职责边界

后端 Service 层不能直接调用 `subprocess` 执行 Freqtrade。所有 Freqtrade CLI、REST、Webserver、策略文件和回测报告处理都必须经过 `backend/app/adapters/freqtrade/`。

数据库只保存长期结构化数据；大文件、K 线文件、回测 JSON 报告和日志保存到本地文件系统。

## 为什么复用 Freqtrade

Freqtrade 已经提供成熟的交易所接入、数据下载、回测、Hyperopt、运行态和 UI 能力。重复实现这些能力会扩大风险面，并让策略研究系统承担交易框架责任。

## 为什么不重复开发 FreqUI

FreqUI 已经覆盖运行状态、交易运行控制、图表和策略运行观察。本项目只提供 FreqUI 快捷入口，不复制 FreqUI 的运行态页面。

## 后续扩展方向

Phase 1 接入策略蓝图、策略版本、批量回测和评分。Phase 2 再考虑更强的数据质量、Walk-forward、Monte Carlo 和过拟合检测。Dry-run / Live 只能在独立阶段经过人工审批后开启。
