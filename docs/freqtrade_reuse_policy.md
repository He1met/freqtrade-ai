# Freqtrade Reuse Policy

## 最高原则

本项目尽可能复用 Freqtrade 已有功能，不重新开发量化交易框架。

## 复用范围

Freqtrade 负责：

- 行情数据下载
- 策略加载
- 回测执行
- Hyperopt 参数优化
- Dry-run / Live 运行
- FreqUI / REST API / Telegram
- 交易所接入

## 本项目范围

本项目负责：

- AI 策略生成
- 策略 JSON 蓝图管理
- 策略代码生成
- 策略版本管理
- 批量回测任务管理
- 回测结果解析和归档
- 策略评分排行榜
- 项目进度管理
- FreqUI 快捷入口

## 禁止重复开发

本项目不修改 Freqtrade 源码，不自己实现交易所 API，不自己实现 K 线下载器，不自己实现回测引擎，不重复开发 FreqUI 已有功能。

## Phase 1 安全边界

Phase 1 不做实盘交易，不做模拟盘交易，不引入 Redis / Celery / Kafka / RabbitMQ，不做复杂权限和审计。
