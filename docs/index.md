# Freqtrade AI 文档导航

这是仓库唯一的 current 文档导航入口。任何会变化的进度结论以开放 GitHub issues、
当前 checkout 和新鲜 runtime/receipt 证据为准；README、roadmap、已合并 PR、历史报告和
页面快照都不能单独证明当前完成或可运行。

## Runtime truth

- [本地 runtime / worker](phase9_db_backed_worker.md)：受管服务、`status`、`verify` 与恢复边界。
- [ExecutionTarget](execution_target.md)：执行目标隔离和 Demo-only 约束。
- [OKX Demo E2E](okx_demo_e2e.md)：端到端验收与 fail-closed 判定。
- [OKX Demo risk chain](okx_demo_risk_chain.md)：自然信号、风控和 owner-mediated 写入边界。
- [OKX Demo read adapter](okx_demo_read_adapter.md)：只读交易所事实与数据来源。
- [OKX Demo soak](okx_demo_soak.md)：持续观察和对账证据要求。

运行状态必须现场执行仓库支持的 verification 命令后判断。`VERIFIED` 仅说明该次核对的
基线一致；它不替代尚未完成 issue 的验收，也不授权研究、部署、重启或下单。

## Current plan

Current plan 不在静态 roadmap 中维护：

- [开放 issues](https://github.com/He1met/freqtrade-ai/issues)
- [`roadmap/current`](https://github.com/He1met/freqtrade-ai/issues?q=is%3Aissue+is%3Aopen+label%3Aroadmap%2Fcurrent)
- [`roadmap/next`](https://github.com/He1met/freqtrade-ai/issues?q=is%3Aissue+is%3Aopen+label%3Aroadmap%2Fnext)
- [`roadmap/long-term`](https://github.com/He1met/freqtrade-ai/issues?q=is%3Aissue+is%3Aopen+label%3Aroadmap%2Flong-term)

若 issue、PR 和 runtime 证据不一致，保持未知或阻断，并在 issue 中收敛事实；不得修改静态
文档来制造完成状态。

## Research contract

- [60-candidate 正式研究契约](formal_research_contract.md)：当前唯一候选数量、矩阵、状态、
  质量门和交接定义。
- [Strategy research lifecycle](strategy_research_lifecycle.md)：历史流程说明；涉及候选数量时
  以上述契约为准。
- [独立 OOS / Walk-forward 验证矩阵](strategy_validation_matrix.md)：正式策略版本的独立验证边界。
- [多资产 canonical handoff](multi_asset_research_canonical_handoff.md)：历史合并交接与 owner 边界。
- [正式网页信息架构 PRD](product/formal_web_information_architecture_prd.md)：产品/页面证据模型；
  其中旧的固定 10 条文案已 superseded。

## Runbooks and governance

- [Feature intake](feature_intake.md) 与 [Acceptance checklist](acceptance_checklist.md)
- [凭据边界](okx_demo_credentials.md)
- [Runtime 安全边界](phase9_security_boundary_review.md)
- [ExecutionTarget lineage](execution_target_lineage.md)
- [受控 canary](okx_demo_canary.md)
- [ADR-0010：OKX Demo single writer](adr/0010-okx-demo-order-writer-compatibility.md)

这些文档不授权 `OKX_LIVE`、真实资金、真实订单、credentials 读取/记录、扩大 DB/ACL、
runtime 接管或绕过唯一 writer。

## Historical evidence

- [历史 roadmap](roadmap.md)：Phase 规划与背景，已 superseded 为 current plan 来源。
- `phase*_acceptance.md`、`phase*_plan.md`：阶段验收和设计快照。
- `reports/`：带时间、commit、环境和 receipt 的运行/研究证据；报告事实只在其声明的证据范围内成立。
- `docs/adr/`：架构决策记录，不因 current 入口收口而删除或重写。

仓库当前尚无统一的 reports manifest。预留 canonical 路径为 `reports/index.md`；在该文件由
独立任务实际建立并合并前，本导航不提供虚假的可点击入口，也不把目录内容描述为完整清单。

## 静态文档验收

文档修改至少执行：

```bash
python3 scripts/scan_secrets.py
git diff --check
```

还应检查所有相对 Markdown 链接实际存在；外部 GitHub 链接只作为 current plan 的动态入口，
不把查询结果复制为静态状态表。
