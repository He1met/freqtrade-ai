# Acceptance Checklist

每个新增功能 PR 必须在描述中说明适用的验收项、执行命令和结果。只改文档或 Issue 模板的
PR 可以不运行 backend pytest、compileall、frontend build 或 smoke，但必须在 PR 中说明原因，
并至少运行 `git diff --check` 和必要的 Markdown / 链接检查。

## 基础验收命令

除非 PR 明确为 docs-only，并在 PR 描述中说明跳过原因，默认运行：

```bash
(cd backend && . .venv/bin/activate && pytest)
python3 -m compileall backend/app backend/tests scripts
(cd frontend && npm run build)
python3 scripts/scan_secrets.py
git diff --check
```

## Relevant Smoke Command

选择与变更所属阶段或受影响能力最接近的 smoke。新增 smoke 或新阶段必须在 Feature Intake
中说明，并在 PR 中写清命令、结果和 fixture / fallback 来源。

| 范围 | 命令 |
| --- | --- |
| Phase 1 MVP | `python3 scripts/smoke_mvp.py --offline --tmp-dir /tmp/freqtrade-ai-smoke` |
| Phase 2 策略研发增强 | `python3 scripts/smoke_phase2.py --offline --tmp-dir /tmp/freqtrade-ai-phase2-smoke` |
| Phase 3 回测体系增强 | `python3 scripts/smoke_phase3.py --offline --tmp-dir /tmp/freqtrade-ai-phase3-smoke` |
| Phase 4 Hyperopt 参数优化 | `python3 scripts/smoke_phase4.py --offline --tmp-dir /tmp/freqtrade-ai-phase4-smoke` |
| Phase 5 Dry-run / FreqUI 管理 | `python3 scripts/smoke_phase5.py --offline --tmp-dir /tmp/freqtrade-ai-phase5-smoke` |
| Phase 6 实盘候选与部署治理 | `python3 scripts/smoke_phase6.py --offline --tmp-dir /tmp/freqtrade-ai-phase6-smoke` |
| Phase 7 工程化与只读运行可见性 | `python3 scripts/smoke_phase7.py --offline --tmp-dir /tmp/freqtrade-ai-phase7-smoke` |
| Phase 8 Local Strategy Lab / DB-API-UI 对账 | `python3 scripts/smoke_phase8.py --offline --tmp-dir /tmp/freqtrade-ai-phase8-smoke` |
| Phase 9 DeepSeek 单次 E2E 安全默认路径 | `python3 scripts/phase9_deepseek_single_e2e.py --json` |

Phase 8 / Phase 9 和当前 refactor/runtime PR 必须记录 API、UI、数据库、artifact、
source marker、fail-closed 和安全边界证据。真实 DeepSeek 调用只允许在本地 operator
明确授权后运行 `python3 scripts/phase9_deepseek_single_e2e.py --allow-real-call --json`；
默认 PR 验证不得泄露或持久化真实 key。

## PR 验收清单

- [ ] backend pytest：如涉及 backend、schema、repository、service、API 或 smoke，运行
  `(cd backend && . .venv/bin/activate && pytest)`。
- [ ] compileall：如涉及 Python 代码、脚本或 smoke，运行
  `python3 -m compileall backend/app backend/tests scripts`。
- [ ] frontend build：如涉及 frontend、API 类型、页面或 fallback 展示，运行
  `(cd frontend && npm run build)`。
- [ ] relevant smoke command：按阶段或功能选择对应 offline smoke，并记录结果。
- [ ] secret scan：运行 `python3 scripts/scan_secrets.py`，确认没有提交真实 API key、secret、
  token、passphrase 或 secret-shaped fixture。
- [ ] git diff check：运行 `git diff --check`，确认没有 whitespace error。
- [ ] API endpoint check：如涉及 API，记录 endpoint、方法、fixture / fallback 来源、状态码、
  response shape 和 read-only / fail-closed 结果。
- [ ] frontend manual check：如涉及 UI，记录页面路径、关键状态、fallback / mock 来源是否可见，
  以及是否没有展示 live trading、真实交易所连接或生产就绪误导。
- [ ] DB/UI reconciliation check：如涉及数据库、seed、API DTO 或页面展示，记录数据库查询、
  API response 和 UI 页面是否一致；不一致必须 `BLOCKED` 或创建缺陷 Issue。
- [ ] artifact / manifest check：如涉及运行证据，确认 artifact / manifest 存在、字段完整、
  来源可追溯、脱敏生效，并且不会把 fixture 当成真实运行。
- [ ] fail-closed check：验证缺少前置条件、缺少数据、缺少 ENV、风险超限或命令失败时返回
  `BLOCKED` / `FAILED`，不得伪造成功。
- [ ] fallback/mock source visibility check：确认 API、前端、文档或 smoke 输出清楚标记
  fixture、fake runner、fallback、mock data 或 local-only 来源。
- [ ] scope creep check：确认 PR 只覆盖已批准 Issue / Feature Intake 的范围，没有顺带新增阶段、
  页面、API、数据库写入、交易控制、部署能力或队列基础设施。
- [ ] docs / link check：如修改 README、roadmap、docs 或 Issue 模板，确认 Markdown 文件存在，
  相对链接有效，Issue template front matter 格式有效。

## 安全边界检查

每个 PR 都必须确认没有引入以下能力，除非已有单独阶段、单独 Issue 和人工审批明确授权：

- 不执行真实下单；
- 不启动 live trading；
- 不连接真实交易所；
- 不下载真实 K 线；
- 不提交真实 API key、secret、passphrase；
- 不修改 Freqtrade 源码；
- 不实现 Redis、Celery、Kafka、RabbitMQ 或 worker pool；
- 不实现 deployment executor；
- 不实现 start / stop / deploy live bot 控制；
- 不把 fixture / fallback 数据展示成真实运行数据。

## Docs-only PR 说明格式

Docs-only PR 至少在描述中写明：

- 修改范围仅限文档、Issue 模板或链接；
- 未运行 backend pytest / compileall / frontend build / smoke 的原因；
- 已运行的检查，例如 `git diff --check`、Issue template front matter 检查、文档链接检查；
- 本 PR 不修改 Freqtrade 源码、不新增 live trading、真实下单、生产部署或生产交易控制能力。
