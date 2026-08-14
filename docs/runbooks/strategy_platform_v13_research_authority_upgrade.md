# Canonical V1.3 research authority upgrade（no-trade）

本 runbook 只升级 46-table canonical database 的 PostgreSQL role、ACL 和 manifest digest。
它不改表结构、不删除或改写历史业务数据、不执行策略、回测、评分、qualification、optimization、
runtime 或交易。旧库、shared/legacy database、Keychain 和 OKX 都不是本步骤的输入。

## 1. 精确边界

- previous manifest digest：
  `282a29277220c1626800356e37f121ed6e3800d72c49d0bb60573a9fb006f9e6`；
- current manifest digest：由当前 release 的 `CANONICAL_MANIFEST_DIGEST` 唯一给出；
- 表必须仍精确为 46 张；validation/scoring/qualification/optimization 共 9 张表必须全部为 0 行；
- 原 `canonical_research_writer` 必须是无 membership 的 `NOLOGIN/NOINHERIT` role；
- current writers 固定为 `canonical_validation_writer`、`canonical_scoring_writer`、
  `canonical_qualification_writer`、`canonical_optimization_writer`；每表恰好一个 writer；
- 任何缺 role、额外 ACL、research 行、identity drift 或 receipt history 不平衡均 `BLOCKED`。

原 broad role 在升级后不删除，只撤销全部 schema/table privilege，作为可回滚 anchor。回滚也不
删除四个新 role，只撤销其 privilege；upgrade/rollback 都向 `audit_events` 追加 typed receipt，
不得删除旧 receipt。

## 2. 离线评审（不连接数据库）

从待发布的 clean checkout 执行：

```bash
cd backend
python scripts/canonical_v13_bootstrap.py authority-plan
```

输出必须包含 previous/current digest、role mapping digest、9 张 research 表、upgrade/rollback ACL
digest、`requires_zero_research_rows=true` 和 `destructive_table_operations=[]`。此结果不是生产升级
完成证据。

## 3. 生产变更前置门

1. release pin 已固定到包含本 contract 的 exact `main` SHA；
2. 已保存可恢复 backup、SHA-256、恢复演练位置和 rollback owner；
3. API/control service 进入维护窗口，尚未启动任何 research worker；
4. provisioner DSN 只通过
   `FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL` 注入，数据库名必须精确为
   `freqtrade_ai_v13`；actor 只通过 `FREQTRADE_AI_CANONICAL_V13_UPGRADE_ACTOR` 注入；
5. read-only preflight：

```bash
cd backend
python scripts/canonical_v13_bootstrap.py authority-verify
```

只允许 `PREVIOUS_READY` 或已完成状态 `CURRENT`。`PREVIOUS_READY` 必须显示 research row count 0；
不得为通过门而删除、truncate 或移动研究数据。

## 4. 单事务 apply 与验证

在同一受控环境中执行：

```bash
cd backend
python scripts/canonical_v13_bootstrap.py authority-apply
python scripts/canonical_v13_bootstrap.py authority-verify
python scripts/canonical_v13_bootstrap.py verify-current
```

apply 先锁定唯一 `schema_metadata` identity，再创建缺失的四个 `NOLOGIN/NOINHERIT` capability
roles、撤销旧 broad role、应用 exact current ACL、guarded 更新 manifest digest、追加 upgrade receipt，
最后在提交前 post-verify。任一步失败由调用方事务整体 rollback。成功状态为 `UPGRADED/CURRENT`；
exact replay 为 `NO_OP_ALREADY_CURRENT`，不得盲目重试未知状态。

升级后需独立证明：旧 broad role无 privilege/membership；scorer 只有 `target_scores` DML；qualifier
只有 qualification 两表 DML；validation/optimization 各自只写所属表；46 表和已有非 research
业务行保持不变。

## 5. rollback

只有 release pin 也将回退到 previous-manifest 代码、9 张 research 表仍全部 0 行、且恰好存在一个
未回滚 upgrade receipt 时，才允许：

```bash
cd backend
python scripts/canonical_v13_bootstrap.py authority-rollback
```

rollback 恢复 reviewed previous ACL/digest并追加 rollback receipt，不删除任何表、role、receipt 或
历史业务行。若 research 已产生任何行，rollback 固定 `BLOCKED`；必须走新的显式迁移设计，不能
回授 broad writer。

## 6. 后续 production provisioning

ACL upgrade 本身不创建 LOGIN、Keychain item 或 worker。后续恢复任务必须分别 provision 四个
LOGIN，并证明每个 LOGIN 只继承一个 exact capability；四个 DSN 分别通过：

- `FREQTRADE_AI_CANONICAL_V13_VALIDATION_DATABASE_URL`
- `FREQTRADE_AI_CANONICAL_V13_SCORING_DATABASE_URL`
- `FREQTRADE_AI_CANONICAL_V13_QUALIFICATION_DATABASE_URL`
- `FREQTRADE_AI_CANONICAL_V13_OPTIMIZATION_DATABASE_URL`

所有 locator 必须指向同一 canonical database，username 必须匹配 role mapping。完成这些步骤仍不
授权首回测；market/bundle/plan、one-shot `PRODUCTION_RESEARCH` authorization、sandbox image 与
resource limits、worker execution 仍是后续独立门。

四个 LOGIN 和 membership 完成后，用 `canonical_v13_bootstrap.py verify-research-provisioned`
验收 6 个 LOGIN 的 exact membership 与所有 capability ACL；此时不再使用要求 research roles
零 membership 的 upgrade preflight verifier。
