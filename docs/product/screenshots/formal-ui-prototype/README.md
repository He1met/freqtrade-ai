# 正式工作台桌面截图清单

评审基线：`1280×720`、真实前端路由、真实只读 API、2026-08-09。截图过程未点击“手动运行一轮研究（10 条）”，未触发运行时、部署、订单或数据库写入。

现有产品仅提供浅色主题，因此深色主题在本轮标记为 N/A，不以未实现的主题切换作为页面验收条件。

当前本地正式数据库仍为 schema v36，尚无 v37 的 append-only research receipt 表。研究 workspace 采用 section 级降级：attempt 与分钟质量 receipt 标为“未知”，已有真实批次继续展示；没有用 fixture、mock 或历史批次推断 bridge/部署成功。OKX_DEMO 部署、信号、订单和对账区块来自可用的真实只读 API。

| 文件 | 路由 | 覆盖条目 | 评审重点 |
| --- | --- | --- | --- |
| `overview-desktop-v38.png` | `/` | `UI-D-01`–`UI-D-05`、`DATA-D-01`–`DATA-D-02` | 当前 worktree + 正式数据库只读 GET；11 个目录策略、10 个本批候选、0 个合格、2 个 OKX_DEMO ACTIVE，bridge 缺失保持未知 |
| `strategy-factory-desktop-v38.png` | `/strategies` | `UI-S-01`–`UI-S-08`、`DATA-S-01`–`DATA-S-04` | 正式研究因所有权证据不足显示“已阻塞”，按钮禁用；真实批次 10/10/10/10/0/10；未触发 POST |
| `okx-demo-desktop-v38.png` | `/okx-demo` | `UI-O-01`–`UI-O-04`、`DATA-O-01`–`DATA-O-03` | Demo-only 安全字段、2 个 ACTIVE、最近信号已阻塞、3 个订单/0 个成交及 RECOVERED 对账；不可验收结论未被美化 |
| `overview-desktop-v1.jpg` | `/` | `UI-D-01`–`UI-D-05`、`DATA-D-01`–`DATA-D-02` | 首屏结论、四个关键数字、研究未知不计为 0、ACTIVE 部署与 Demo 证据独立展示 |
| `strategy-factory-desktop-v1.jpg` | `/strategies` | `UI-S-01`–`UI-S-08`、`DATA-S-01`–`DATA-S-04` | 唯一研究入口、真实批次 10/10/10/10/0/10、receipt 缺失保持未知、QUALIFIED 不推断部署 |
| `okx-demo-desktop-v1.jpg` | `/okx-demo` | `UI-O-01`–`UI-O-04`、`DATA-O-01`–`DATA-O-03` | Demo-only 安全字段、严格验收结论、ACTIVE 部署、最近信号、订单/成交/对账证据 |

`v38` 是当前页面与 bridge 契约批次的截图标识，不表示正式数据库已迁移到 schema v38。截图时正式数据库仍为 `20260804_36`；新 receipt/bridge 表缺失所造成的 section 降级被原样保留为“未知”。

移动端、平板和窄屏响应式已从当前产品范围与验收门禁移除；既有多视口自动化仅保留在提交历史中，不再进入默认 CI。
