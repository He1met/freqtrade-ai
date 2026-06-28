# GitHub Project Plan

## Project 名称

`Freqtrade AI Roadmap`

用途：管理 Freqtrade AI 从 Phase 0 到 Phase 7 的完整开发进度。

如果 GitHub Project API 或账号权限不足，按本文档手动创建 Project、字段、视图和 Issue 关联。

## 看板列

Project 的 `Status` 字段使用以下选项：

- `Backlog`: 以后要做，但还没准备开发。
- `Ready`: 需求清楚，可以交给 Codex。
- `In Progress`: 正在开发。
- `Review`: 等待检查或 PR Review。
- `Blocked`: 被依赖、权限、环境或设计问题卡住。
- `Done`: 已完成并验收。

## Project 自定义字段

### Phase

- `Phase 0 - 项目治理与工程骨架`
- `Phase 1 - 最小闭环 MVP`
- `Phase 2 - 策略研发增强`
- `Phase 3 - 回测体系增强`
- `Phase 4 - Hyperopt 参数优化`
- `Phase 5 - Dry-run / FreqUI 运行管理`
- `Phase 6 - 实盘候选与部署管理`
- `Phase 7 - 工程化升级与规模化运行`

### Type

- `Epic`
- `Feature`
- `Task`
- `Bug`
- `Docs`
- `Refactor`
- `Test`
- `DevOps`
- `Spike`
- `Review`

### Area

- `backend`
- `frontend`
- `database`
- `freqtrade-adapter`
- `config`
- `docs`
- `scripts`
- `testing`
- `devops`
- `github-project`
- `frequi`
- `security`
- `deployment`

### Priority

- `P0 - 阻塞项目`
- `P1 - 当前阶段必须完成`
- `P2 - 重要但可稍后`
- `P3 - 可选优化`

### Size

- `XS - 30分钟内`
- `S - 半天内`
- `M - 1天左右`
- `L - 2到3天`
- `XL - 需要继续拆分`

规则：如果一个 Issue 是 `XL - 需要继续拆分`，不允许直接开发，必须继续拆分。

## Milestones

- `Phase 0 - 项目治理与工程骨架`: 把项目从 0 变成一个可管理、可启动、可继续开发的真实工程项目。
- `Phase 1 - 最小闭环 MVP`: 跑通 AI 生成策略 -> 保存策略版本 -> 调用 Freqtrade 回测 -> 解析结果 -> 评分 -> 前端展示。
- `Phase 2 - 策略研发增强`: 提高策略生成质量，增加策略校验、策略审查、失败原因、版本 Diff。
- `Phase 3 - 回测体系增强`: 增强多周期、多窗口、随机窗口、结果聚合、失败重试、报告归档。
- `Phase 4 - Hyperopt 参数优化`: 复用 Freqtrade Hyperopt，对候选策略做参数优化。
- `Phase 5 - Dry-run / FreqUI 运行管理`: 把候选策略放入 Freqtrade dry-run，复用 FreqUI 展示运行状态。
- `Phase 6 - 实盘候选与部署管理`: 建立人工审批、风险检查、部署记录、回滚机制。
- `Phase 7 - 工程化升级与规模化运行`: 引入 Redis / Worker / 权限 / 审计 / 监控 / CI/CD，让项目可以长期运行。

## Labels

### 阶段 Labels

- `phase-0`
- `phase-1`
- `phase-2`
- `phase-3`
- `phase-4`
- `phase-5`
- `phase-6`
- `phase-7`

### 类型 Labels

- `type/epic`
- `type/feature`
- `type/task`
- `type/bug`
- `type/docs`
- `type/refactor`
- `type/test`
- `type/devops`
- `type/spike`
- `type/review`

### 模块 Labels

- `area/backend`
- `area/frontend`
- `area/database`
- `area/freqtrade-adapter`
- `area/config`
- `area/docs`
- `area/scripts`
- `area/testing`
- `area/devops`
- `area/github-project`
- `area/frequi`
- `area/security`
- `area/deployment`

### 优先级 Labels

- `priority/p0`
- `priority/p1`
- `priority/p2`
- `priority/p3`

### 管控 Labels

- `codex-task`
- `needs-review`
- `needs-design`
- `blocked`
- `good-first-issue`
- `do-not-merge`

## Phase 0 Issues

所有 Phase 0 Issues 关联 Milestone `Phase 0 - 项目治理与工程骨架`、Label `phase-0` 和 Project `Freqtrade AI Roadmap`。

- `[EPIC][Phase 0] 项目治理与工程骨架`
- `[Docs][Phase 0] 创建 GitHub Project 管理规范`
- `[Chore][Phase 0] 创建标准仓库目录结构`
- `[Docs][Phase 0] 创建 README 和项目说明`
- `[Config][Phase 0] 创建基础 YAML 配置文件`
- `[Config][Phase 0] 创建 .env.example`
- `[Backend][Phase 0] 初始化 FastAPI 后端骨架`
- `[Backend][Phase 0] 增加 /health 接口`
- `[Frontend][Phase 0] 初始化 React + Vite + TypeScript 前端骨架`
- `[Frontend][Phase 0] 创建基础页面空壳`
- `[Adapter][Phase 0] 创建 Freqtrade Adapter 占位层`
- `[Database][Phase 0] 创建 db/migrations/001_init.sql 占位`
- `[Docs][Phase 0] 创建 Freqtrade 复用边界说明`
- `[Review][Phase 0] Phase 0 验收`

## Phase 1 Epic Issues

只创建 Phase 1 Epic，不在 Phase 0 初始化时创建全部细分任务。所有 Phase 1 Epic 关联 Milestone `Phase 1 - 最小闭环 MVP`、Label `phase-1` 和 Project `Freqtrade AI Roadmap`。

- `[EPIC][Phase 1] 最小闭环 MVP`
- `[EPIC][Phase 1] PostgreSQL 第一阶段核心表`
- `[EPIC][Phase 1] 后端数据库模型与 Repository`
- `[EPIC][Phase 1] 策略生成批次管理`
- `[EPIC][Phase 1] 策略 JSON 蓝图与版本管理`
- `[EPIC][Phase 1] 策略代码生成与文件管理`
- `[EPIC][Phase 1] Freqtrade CLI Runner`
- `[EPIC][Phase 1] Freqtrade 配置生成`
- `[EPIC][Phase 1] 行情数据文件索引`
- `[EPIC][Phase 1] 回测批次与任务管理`
- `[EPIC][Phase 1] Freqtrade 回测执行`
- `[EPIC][Phase 1] 回测结果解析入库`
- `[EPIC][Phase 1] 策略评分与排行榜`
- `[EPIC][Phase 1] FreqUI 复用入口`
- `[Review][Phase 1] Phase 1 MVP 验收`

## Issue 模板

仓库包含以下模板：

- `.github/ISSUE_TEMPLATE/task.md`
- `.github/ISSUE_TEMPLATE/bug.md`
- `.github/ISSUE_TEMPLATE/docs.md`

模板必须要求 Issue 明确背景、目标、范围、验收标准和 Codex 注意事项。没有验收标准的 Issue 不能进入 `Ready`。

## PR 模板

仓库包含 `.github/PULL_REQUEST_TEMPLATE.md`。

PR 必须包含：

- 关联 Issue，使用 `Closes #XX`。
- Changes。
- Validation。
- 安全检查：不引入 Redis、不引入 Celery / Kafka / RabbitMQ、不修改 Freqtrade 源码、不重复实现 Freqtrade / FreqUI 已有功能、不提交真实 API Key。

## Codex 执行规则

- Codex 每次只执行一个明确任务。
- Codex 只能在对应 Issue 范围内修改。
- 不允许顺手扩展、顺手重构或提前开发远期阶段功能。
- 不允许删除已有重要文件，除非当前 Issue 明确要求。
- 涉及需求变更、阶段门变更、交易权限、密钥处理、部署或生产写入时，必须先创建或更新 Issue。
- 所有真实密钥只放 ENV，不写入代码、YAML、数据库或文档。
- 不修改 Freqtrade 源码。
- 不自研交易所 API、K 线下载器、回测引擎或 FreqUI 已有功能。

## 每个 PR 只解决一个 Issue

- 每个 Pull Request 必须只关闭一个 Issue。
- 多个需求必须拆成多个 Issue 和多个 PR。
- 如果发现一个 Issue 是 `XL - 需要继续拆分`，先拆分 Issue，不进入开发。
- PR 标题、描述和提交信息都应能追溯到唯一 Issue。

## 不允许 Codex 顺手扩展

- 不创建未要求的业务功能。
- 不写策略生成逻辑。
- 不写回测执行逻辑。
- 不写数据库完整实现。
- Phase 0 不引入 Redis、Celery、Kafka、RabbitMQ。
- Phase 0 不创建实盘模块或模拟盘模块。
- Phase 0 不启用 Freqtrade trade、dry-run 或 live 运行。

## 手动创建 Project 步骤

如果 GitHub Project 创建或字段配置失败：

1. 打开 GitHub 用户或组织的 Projects 页面。
2. 创建 Project，名称为 `Freqtrade AI Roadmap`。
3. 创建或更新 `Status` 字段，加入 `Backlog`、`Ready`、`In Progress`、`Review`、`Blocked`、`Done`。
4. 创建 `Phase`、`Type`、`Area`、`Priority`、`Size` 单选字段，并使用本文档列出的选项。
5. 把 Phase 0 Issues 和 Phase 1 Epic Issues 加入 Project。
6. Phase 0 Issues 的 `Phase` 设为 `Phase 0 - 项目治理与工程骨架`。
7. Phase 1 Epic Issues 的 `Phase` 设为 `Phase 1 - 最小闭环 MVP`。
8. 新建 Issue 默认放入 `Backlog`，已准备开发的 Issue 移到 `Ready`。
