---
name: Feature Request
about: 新增功能入口和风险评估
title: "[Feature] "
labels: "type/feature"
assignees: ""
---

## 功能背景

说明为什么需要该功能、关联哪个阶段、Issue、验收缺口或用户场景。

## 功能目标

用可验收语言说明最终产出。

## Feature Intake

请完整填写；详细规则见 `docs/feature_intake.md`。

| 字段 | 答案 |
| --- | --- |
| 所属阶段 |  |
| 风险等级：safe / medium / high |  |
| 是否只读 |  |
| 是否涉及真实交易所 |  |
| 是否涉及真实密钥 |  |
| 是否涉及 dry-run |  |
| 是否涉及 live trading |  |
| 是否涉及部署 |  |
| 是否涉及 worker / queue |  |
| 是否需要人工审批 |  |
| 是否需要更新 smoke |  |
| 是否需要更新前端 |  |
| 是否需要更新文档 |  |

## 范围

### 需要做

- [ ] xxx

### 不需要做

- [ ] 不启动 Phase 8
- [ ] 不新增交易控制能力
- [ ] 不修改 Freqtrade 源码

## 明确禁止项

- [ ] 不执行真实下单
- [ ] 不启动 live trading
- [ ] 不连接真实交易所
- [ ] 不下载真实 K 线
- [ ] 不提交真实 API key、secret、passphrase
- [ ] 不修改 Freqtrade 源码
- [ ] 不实现 Redis、Celery、Kafka、RabbitMQ 或 worker pool
- [ ] 不实现 deployment executor
- [ ] 不实现 start / stop / deploy live bot 控制
- [ ] 不把 fixture / fallback 数据展示成真实运行数据

## 高风险审批

如涉及 real dry-run execution、exchange connectivity、live candidate execution、
deployment executor、queue infrastructure 或 production operation，必须填写：

- 审批人：
- 审批记录：
- fail-closed 条件：
- 密钥处理边界：
- 额外验收命令：

## 验收标准

请按 `docs/acceptance_checklist.md` 选择适用项。

- [ ] backend pytest
- [ ] compileall
- [ ] frontend build
- [ ] relevant smoke command
- [ ] secret scan
- [ ] git diff check
- [ ] API endpoint check，如涉及 API
- [ ] frontend manual check，如涉及 UI
- [ ] artifact / manifest check，如涉及运行证据
- [ ] fail-closed check
- [ ] fallback/mock source visibility check

## Codex 注意事项

- 只完成这个 Issue。
- 不要顺手开发其他功能。
- 不要大面积重构。
- 不要删除已有重要文件。
- 缺少阶段授权、审批、数据、ENV、测试或验收标准时必须 fail closed。
