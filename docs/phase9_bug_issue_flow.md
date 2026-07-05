# Phase 9 Bug Issue Flow

Phase 9 的 Bug Issue 用于把页面、API、数据库、Provider、回测、数据来源和安全边界中发现的问题，转成单一、可复现、可验收的 GitHub Issue。Bug 不应停留在聊天记录、测试报告、QA 截图或 PR 评论里。

本流程只定义 Bug 创建规则、字段模板和触发清单；不创建真实 Bug Issue，不修复具体 Bug，不授权 live trading、真实下单、生产部署或修改 Freqtrade 源码。

## 创建规则

- 一个 Bug Issue 只描述一个可复现问题；如果同一次 QA 发现多个页面、多个接口或多个根因，必须拆成多个 Bug。
- Feature、Task、Docs、Config、Test 或 Security 工作不得顺手修无关 Bug。发现无关 Bug 时，先按本文档创建独立 Bug，再由单独 PR 处理。
- Bug 必须写清数据来源：`database`、`api_aggregate`、`fixture`、`fallback`、`mock` 或 `unknown`。
- fixture、fallback、mock、unknown 数据不得被描述成真实数据库、真实 Provider、真实回测或真实运行成功。
- 涉及缺少 API key、本地行情数据、Freqtrade binary、策略文件、配置或权限时，Bug 必须标注 `BLOCKED` 状态和解除条件。
- Issue、PR、日志、页面、截图和附件不得包含真实 API key、secret、passphrase、token、cookie 或账户敏感信息。

## Bug Issue 模板字段

复制以下模板创建 Phase 9 Bug Issue。未能填写必填字段时，Issue 不应进入 `Ready`。

```markdown
## 复现步骤

1. 进入页面、调用 API、运行命令或执行 QA 脚本。
2. 使用的配置、环境、数据集、策略文件或前置条件。
3. 触发问题的最小步骤。

## 当前表现

实际看到的页面状态、API 响应、数据库记录、报告内容、日志或阻断状态。

## 期望表现

按 Phase 9 设计应展示、返回、写入、阻断或提示的行为。

## 页面 / API / 数据库影响

- 页面：
- API：
- 数据库：

## 数据来源

- 来源类型：`database` / `api_aggregate` / `fixture` / `fallback` / `mock` / `unknown`
- 来源路径、表名、接口、artifact 或报告：
- 是否可作为真实数据验收通过：`yes` / `no`
- 如果不是真实数据库数据，缺少的前置条件：

## 安全影响

- 是否涉及真实密钥、交易所凭据、账户信息或敏感日志：`yes` / `no`
- 是否已脱敏：
- 是否触及 live trading、真实下单、生产部署或 Freqtrade 源码修改：`yes` / `no`

## 证据

- 页面截图或录屏：
- API 请求和响应摘要：
- 数据库查询摘要：
- 日志或测试输出：
- 关联报告、artifact 或 commit：

## 验收标准

- [ ] Bug 可按复现步骤稳定复现，或明确标注 `BLOCKED` 和解除条件。
- [ ] 修复后页面、API、数据库证据一致。
- [ ] 数据来源标注正确，fixture / fallback / mock / unknown 不被冒充为真实数据。
- [ ] 未泄露真实密钥或敏感信息。
- [ ] 未把无关 Feature、Config、Docs、Test 或 Security 工作混入同一修复 PR。
```

## 15 类 Bug 触发条件

| # | 触发条件 | 必须提供的证据 | 默认分类 |
| --- | --- | --- | --- |
| 1 | Bug 报告缺少可执行复现步骤，或步骤依赖未说明的本地状态。 | 最小复现步骤、前置条件、阻断条件。 | Bug / Test |
| 2 | 页面显示成功、完成、真实运行或验收通过，但 API 或数据库没有对应证据。 | 页面截图、API 响应摘要、数据库查询摘要。 | Bug / Frontend |
| 3 | API 返回成功、非空数据或聚合结果，但数据库记录缺失、状态不一致或来源不明。 | API 请求响应、表名、主键或查询条件。 | Bug / Backend / Database |
| 4 | 数据库记录存在，但页面或 API 未展示、展示错字段、状态映射错误或时间排序错误。 | 数据库查询、页面截图、API 摘要。 | Bug / Frontend / API |
| 5 | 数据来源被误标：fixture、fallback、mock 或 unknown 被展示成 database、Provider 或真实回测结果。 | payload 中的 source 字段、页面来源标识、报告片段。 | Bug / Data Source |
| 6 | 缺少 API key、行情数据、Freqtrade binary、策略文件、配置或权限时没有返回 `BLOCKED` 和解除条件。 | 错误输出、页面提示、配置检查结果。 | Bug / Config |
| 7 | Provider 调用、策略生成、回测或报告使用 fake / fallback 结果，但文案或状态暗示真实 Provider 成功。 | provider 状态、artifact、日志摘要。 | Bug / Provider |
| 8 | 回测 artifact、指标、manifest 或报告缺失、不可解析、路径错误，或与数据库 run/task 记录不一致。 | artifact 路径、manifest 摘要、数据库记录。 | Bug / Backtest |
| 9 | 页面、API 或报告缺少错误状态、空状态、失败原因、重试条件或用户下一步操作。 | 失败场景截图、API 错误体、日志摘要。 | Bug / UX / API |
| 10 | QA 报告、smoke、grep 或 markdown sanity 发现阶段边界、验收标准或证据链缺口。 | QA 命令、输出摘要、关联文档段落。 | Bug / Test |
| 11 | GitHub Project、labels、milestone、Issue type 或状态无法区分 Bug、Feature、Test、Config、Docs、Security。 | Project 字段截图或 `gh issue view` 摘要。 | Bug / GitHub Project |
| 12 | Feature PR、Task PR 或 Docs PR 顺手修无关 Bug，或 Bug 修复混入未批准 Feature。 | PR diff 摘要、关联 Issue、变更文件列表。 | Bug / Governance |
| 13 | 多个 Bug 被写进同一个 Issue，导致复现步骤、证据或验收标准无法一一对应。 | Issue 内容、拆分建议、每个子问题的独立触发点。 | Bug / Triage |
| 14 | 日志、Issue、PR、页面、报告或 artifact 暴露真实密钥、token、cookie、账户信息或可逆敏感值。 | 只记录路径、行号、字段名和规则 id；不得复制真实值。 | Security |
| 15 | 页面、API、脚本或文档暗示允许 live trading、真实下单、生产部署、自动实盘调度或修改 Freqtrade 源码。 | 文案、配置、命令或代码路径摘要。 | Security / Config |

## 分类与 Project 路由

创建 Bug 后，至少按以下维度路由：

- `Type`: Bug。若本质是新能力请求，改走 Feature Intake，不创建 Bug。
- `Area`: backend、frontend、database、freqtrade-adapter、config、docs、testing、github-project、frequi、security 或 deployment。
- `Priority`: 根据是否阻塞 Phase 9 真实运行证据链选择 P0、P1、P2 或 P3。
- `Status`: 缺少复现、证据、权限、配置或本地数据时使用 `Blocked`，不要把无法验证的 Bug 放入 `Ready`。
- `Security`: 涉及密钥、敏感日志、交易权限、live trading、生产部署或 Freqtrade 源码边界时，按 Security 单独路由，不混入普通 Feature。

Project 必须能区分 Bug、Feature、Test、Config、Docs 和 Security。无法分类时，先补充证据或拆分 Issue，不要用一个 Issue 承载多个类型。

## 不混 Bug 与 Feature

以下情况必须拆开：

- 开发 Feature 时发现已有页面字段错乱：当前 PR 继续 Feature，另建 Bug 跟踪字段错乱。
- 修 Bug 时发现需要新配置页、新队列、新执行器或新 Provider 能力：Bug PR 只修复已批准问题，新能力走 Feature Intake。
- 文档更新时发现 smoke 失败：文档 PR 不修 smoke，另建 Bug 或 Test Issue。
- Security 问题和普通功能缺陷同时出现：Security 单独 Issue、单独 PR、单独验收。

## QA 示例场景

QA 至少用以下三个场景检查模板覆盖度：

- 页面场景：页面显示真实运行成功，但 API payload 的数据来源为 `fallback`。应创建单一 Bug，证据包含页面截图、API 摘要和数据来源字段。
- API 场景：接口返回 `200`，但缺少数据库对应记录或 `source=unknown`。应创建单一 Bug，证据包含请求摘要、响应摘要和数据库查询条件。
- 数据库场景：数据库已有 run/task 记录和 artifact 路径，但页面不展示或报告无法解析。应创建单一 Bug，证据包含表名、记录标识、artifact 路径和页面/API 摘要。

这些场景只验证流程可用性，不要求创建真实 Bug Issue。
