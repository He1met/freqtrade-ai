# Strategy Platform V1.3 production bootstrap（no-trade）

本文档接续 canonical genesis，只允许对新的 `freqtrade_ai_v13` 执行。旧数据库不是 fallback、
迁移源或验收目标；任何命令都不得连接 `freqtrade_ai` 或 `freqtrade_ai_design_lab`。

## 1. 唯一角色映射

- `canonical_*` 是代码和 manifest 中稳定的逻辑 capability。
- `CanonicalRoleMapping` 是唯一物理映射入口；正式本机 contract 为
  `CanonicalRoleMapping.from_prefix("freqtrade_ai_v13_")`。
- installer、ACL renderer 和 verifier 必须接收同一个 mapping。缺项、额外项、重复物理角色、
  非 PostgreSQL-safe identifier 或 digest drift 均 fail closed。
- 18 个 current capability roles 保持 `NOLOGIN/NOINHERIT`。schema owner/provisioner 永不作为服务
  LOGIN。
- 原 `freqtrade_ai_v13_research_writer` 只作为可审计 rollback anchor 保留：authority upgrade
  后必须无 schema/table ACL、无 membership，不能作为 production service identity。

无连接的计划渲染：

```bash
cd backend
python scripts/canonical_v13_bootstrap.py render
```

验收只从指定环境变量读取 provisioner DSN，不接受命令行 DSN，且不会输出 DSN：

```bash
cd backend
FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL='postgresql+psycopg:///freqtrade_ai_v13' \
  python scripts/canonical_v13_bootstrap.py verify
```

## 2. API/control/research principals 与 Keychain

API 启动前必须建立六个彼此不同的 LOGIN principals：

| LOGIN principal | 唯一 membership | Keychain service |
| --- | --- | --- |
| `freqtrade_ai_v13_api_login` | `freqtrade_ai_v13_api_reader` | `freqtrade-ai/v13/api-reader-password` |
| `freqtrade_ai_v13_control_login` | `freqtrade_ai_v13_control_writer` | `freqtrade-ai/v13/control-password` |
| `freqtrade_ai_v13_validation_login` | `freqtrade_ai_v13_validation_writer` | `freqtrade-ai/v13/research-validation-password` |
| `freqtrade_ai_v13_scoring_login` | `freqtrade_ai_v13_scoring_writer` | `freqtrade-ai/v13/research-scoring-password` |
| `freqtrade_ai_v13_qualification_login` | `freqtrade_ai_v13_qualification_writer` | `freqtrade-ai/v13/research-qualification-password` |
| `freqtrade_ai_v13_optimization_login` | `freqtrade_ai_v13_optimization_writer` | `freqtrade-ai/v13/research-optimization-password` |

`scripts/canonical_v13_api_service.py provision` 仅在两个 LOGIN 和两个 Keychain item 全部不存在、
capability roles 完整且当前连接确为本机 `freqtrade_ai_v13` superuser 时执行。随机值只存在内存、
PostgreSQL password verifier 和 macOS Keychain；不会进入 argv、stdout/stderr、repo、Issue、plist
或 dotenv。任一已存在/半完成状态均 `BLOCKED`，绝不覆盖。

runtime、order、fill、ledger principals 本阶段禁止创建。

production research control surface 另需四个互不相同的 LOGIN，每个只继承一个 capability：
`validation_writer`、`scoring_writer`、`qualification_writer`、`optimization_writer`。它们不由
原有 `provision` 自动创建；恢复任务必须先完成并验证 authority upgrade，再在四个 LOGIN/Keychain
项均不存在时显式执行 `python scripts/canonical_v13_api_service.py provision-research`。该操作不修改
reader/control principal，任一半存在状态都 fail closed 且不覆盖。精确升级和回滚顺序见
[`strategy_platform_v13_research_authority_upgrade.md`](strategy_platform_v13_research_authority_upgrade.md)。

恢复任务的固定次序是：`authority-apply` → `authority-verify`（此时 split roles 仍为零 membership）→
`provision-research` → `verify-research-provisioned`。最后一步要求六个 LOGIN 均为非 superuser、
`INHERIT`、每个只有 exact membership，且 canonical ACL/owner/manifest 无漂移。若要 rollback，必须
另行取得删除 principal/Keychain 的授权，先移除四个 research memberships，再执行 authority rollback；
不得让 verifier 为迁就已 provision 的 membership 而放宽。

`provision-research` 必须同时把 canonical database 的 `CONNECT` 只授予四个 split capability
roles；`verify-research-provisioned` 必须验证六个 LOGIN 均具有 effective database `CONNECT`。
若旧版本已原子创建四个 LOGIN/Keychain/membership，但四个 capability roles 的 `CONNECT` 精确
全部缺失，且 verifier 的唯一问题是 `missing service database CONNECT count=4`，可在 maintenance
window 内执行一次：

```bash
python scripts/canonical_v13_api_service.py repair-research-connect
```

该命令只增加四条 capability database `CONNECT`，不读取 secret value，也不输出、覆盖、轮换或重建 Keychain
material；任何部分完成状态、其他 verifier 问题或 Keychain 缺项都 `BLOCKED`。完成后仍必须重新
运行 `verify-research-provisioned` 和六 identity 实际连接验收。

### 2.1 API reader credential 安全轮换

API reader material 发生疑似暴露时，只允许在 clean/exact-main release、API 已 READY、Phase 9
runtime 与 order writer LaunchAgent 均停止的安全点执行正式轮换。首次 `provision` 不得被当作覆盖或
轮换入口。轮换只修改 `freqtrade_ai_v13_api_login` 的 PostgreSQL SCRAM verifier 和固定 API reader
Keychain item；control/research/Phase 9/runtime/交易凭据都不读取、不修改。

```bash
backend/.venv/bin/python scripts/canonical_v13_api_service.py rotate-api-reader \
  --actor-identity 'operator:<owner>' \
  --idempotency-key '<redacted-incident-key>' \
  --port 8011
```

该命令在单个数据库事务内锁定 incident key、验证 LOGIN 属性与唯一 reader membership、写入新的
SCRAM verifier、原地替换固定 Keychain item，并向 immutable `audit_events` 写入仅含 generation、
release SHA 和 verifier/keychain locator digest 的脱敏 receipt。事务或 audit 写入失败时自动恢复旧
Keychain item；成功提交后必须证明旧 credential 不能新建连接、新 credential 只有 reader 权限，
再对 API 执行恰好一次 `kickstart -k` 并等待 `HEALTHY/READY`。material 不进入 argv、stdout/stderr、
receipt、repo、plist 或 dotenv。相同 actor/key/release 的 exact replay 返回
`NO_OP_ALREADY_ROTATED`，不得产生第二次轮换或第二次 restart；key reuse/drift 必须 BLOCKED。

轮换事务开始前还必须从 `pg_hba_file_rules` 证明 IPv4/IPv6 loopback 的前两条 `host` 规则精确绑定
canonical database 与 API reader LOGIN，且认证方法均为 `scram-sha-256`。任何更早规则、`trust`、
宽泛 database/user、地址或 netmask 漂移都必须在 `ALTER ROLE`、Keychain 和 audit 写入之前返回
`BLOCKED_READER_ROTATION_HBA_UNSAFE`；不得把 post-commit 旧 credential 连接测试当作 HBA 配置器。

轮换后还必须分别执行 UI status、`verify-research-provisioned`、Phase 9 provision verifier、backup/
isolated restore verifier、execution-domain zero-count 与 secret scan。旧 credential 被拒绝、新
credential read-only 和 `trading_credentials_modified=false` 是 release handoff 的必需证据。

## 3. Backup 与独立 restore acceptance

backup 输出目录必须位于 operator-controlled 非仓库位置，权限 `0700`；文件使用 custom format，
并单独记录 SHA-256。不得把 dump 或 digest receipt 提交到 Git。

```bash
umask 077
pg_dump --format=custom --file="$BACKUP_PATH" freqtrade_ai_v13
shasum -a 256 "$BACKUP_PATH"
```

恢复目标必须是一个此前不存在的唯一数据库名，由 `freqtrade_ai_v13_schema_owner` 拥有。恢复后
目标名必须严格匹配 `freqtrade_ai_v13_restore_<lowercase_identity>`。使用显式 restore-only
verifier；普通 production verifier 仍只接受精确 `freqtrade_ai_v13`，不得用 restore override
放宽生产目标校验：

```bash
cd backend
FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL=\
'postgresql+psycopg:///freqtrade_ai_v13_restore_<lowercase_identity>' \
FREQTRADE_AI_CANONICAL_V13_RESTORE_DATABASE_NAME=\
'freqtrade_ai_v13_restore_<lowercase_identity>' \
  python scripts/canonical_v13_bootstrap.py authority-verify-restore
```

结果必须为 `ACCEPTED`、`verification_scope=INDEPENDENT_RESTORE`、
`state=PREVIOUS_READY`（升级前 backup）或 exact compatible current state，并证明
authority identity/digest、ACL/owner 和十一张 research 表零行均匹配。表总数必须另以
canonical schema inventory 复核为 48；backup 文件完整性由前一步记录的 SHA-256 绑定。
验证库保留，除非取得独立删除授权。不得用旧库或现有测试库作为恢复目标。

## 4. 独立 loopback API

仅从 clean release checkout 安装：该 checkout 可以是独立 detached worktree，但其 `HEAD` 必须
精确等于 `origin/main`，worktree 必须 clean，且不得位于 Codex 临时 worktree 路径。不要为了
部署清理、reset、stash 或覆盖已有 dirty main checkout；应另建专用 clean release worktree。

```bash
python scripts/canonical_v13_api_service.py install --port 8011
python scripts/canonical_v13_api_service.py status --port 8011
```

LaunchAgent plist 只含 Python/script/port 与非 secret 环境；服务子进程自行按固定 service/account
读取 Keychain，在内存构造六个 DSN，并调用
`app.canonical_v13.production:create_app`。四个 research DSN 必须使用上表 exact LOGIN principal，
不能使用 NOLOGIN capability role；六个 username 必须不同且 locator 必须是同一个 canonical
database。绑定固定为
`127.0.0.1`，access log 关闭；它不 mount
legacy API、不执行 genesis、不激活配置、不启动 research/runtime/trading。

验收必须覆盖：

- `/healthz` = `HEALTHY` 且 `TRADING_DISABLED`；
- `/readyz` = `READY`，reader/control/validation/scoring/qualification/optimization 六个
  `current_user` 全部不同；
- strategies `EMPTY`、七类 P0 `UNSET`、market `MARKET_SNAPSHOT_UNSET`；
- research/runtime `BLOCKED`、optimization `PENDING_FIRST_BACKTEST`；
- control preview `BLOCKED` 且不写 bundle；
- 重启后结果一致、只有一个 LaunchAgent/process、所有业务域仍为 0。

UI/reverse proxy、公网访问、策略 intake、market、backtest、qualification、activation、runtime、
OKX 和交易链均属于后续独立授权门。

production research 的 control-plane 与 one-shot worker 操作见
[`strategy_platform_v13_production_research.md`](strategy_platform_v13_production_research.md)。

## 5. 独立 canonical UI gateway（no-trade）

UI 只能从与 `origin/main` 精确一致的 clean release checkout 构建和安装。先在
`frontend/` 执行 lockfile 固定的 `npm ci --ignore-scripts` 与 `npm run build`，再安装独立
LaunchAgent：

```bash
python scripts/canonical_v13_ui_service.py install --port 8012
python scripts/canonical_v13_ui_service.py status --port 8012
```

`com.he1met.freqtrade-ai.v13-canonical-ui` 只监听 `127.0.0.1:8012`，静态提供 build artifact，
并且只把 `/api/canonical-v13` 转发至 `127.0.0.1:8011`。任何其他 `/api/*` 都返回
`BLOCKED_LEGACY_API_DISABLED`；没有 legacy backend fallback、DSN、Keychain material 或公网
listener。API 与 UI 使用两个独立 label/process，UI gateway 无数据库连接能力。

真实浏览器验收至少覆盖 canonical submission、strategies、configuration、market-data、
research、optimization 六个路由的 deep-link/refresh/URL state，以及未知 enum、空态和 API
错误 fail-closed。验收不授权策略 intake、market acquisition、research/backtest 或交易。

## 6. P0 configuration rollout 边界

PostgreSQL referential-integrity triggers execute parent-row locking checks under
the canonical table owner. The exact ACL reset must therefore restore the seven
standard table privileges for the NOLOGIN `canonical_schema_owner` on each of the
48 canonical tables. This does not create a second application writer: no service
principal may inherit the schema-owner role, and application DML remains governed
by the per-table writer allowlist.

For the historical ACL state containing only the schema-owner's exact
`SELECT, INSERT` privileges on `schema_metadata`, review the offline plan and run
the one-shot audited repair during maintenance:

```bash
cd backend
python scripts/canonical_v13_bootstrap.py owner-table-acl-plan
FREQTRADE_AI_CANONICAL_V13_PROVISIONER_DATABASE_URL=\
'postgresql+psycopg:///freqtrade_ai_v13' \
FREQTRADE_AI_CANONICAL_V13_UPGRADE_ACTOR=\
'<accepted-release-manifest-identity>' \
  python scripts/canonical_v13_bootstrap.py owner-table-acl-repair
python scripts/canonical_v13_bootstrap.py verify-research-provisioned
```

The repair accepts only that exact two-privilege legacy state, requires all eleven
research tables to remain empty, uses 48 explicit table statements (never `ON ALL
TABLES`), writes one immutable audit receipt, and fails closed on partial ACLs.
Its internal authority gate accepts only the four exact, already-provisioned
research LOGIN memberships; the public pre-provision `authority-verify` command
retains its zero-membership contract.
Re-run the exact repair command only to prove `NO_OP_ALREADY_CURRENT` before
leaving maintenance.

七类 P0 必须逐一通过 `/api/canonical-v13/configurations/{kind}/drafts` 和
`/{kind}/{version_id}/validate`。每个 command 都必须携带唯一 `actor_identity` 与
`idempotency_key`；成功事务同时写入 immutable version/snapshot、`idempotency_receipts` 和
`audit_events`，相同请求 replay 返回同一 receipt，key reuse 或 receipt/audit drift 均 fail
closed。禁止直接 INSERT/UPDATE 配置业务表。

`RESEARCH_AGGREGATE` 只依赖前六类 frozen snapshots。bundle activation 仍必须通过 fresh
market snapshot gate；缺少 market 时只允许 preview=`BLOCKED/MARKET_SNAPSHOT_UNSET`，不得为
满足 rollout 文案而伪造 activation 或第二套 active pointer。此时 research/runtime 保持
`BLOCKED`，optimization 保持 `PENDING_FIRST_BACKTEST`，trading 保持 `TRADING_DISABLED`。
