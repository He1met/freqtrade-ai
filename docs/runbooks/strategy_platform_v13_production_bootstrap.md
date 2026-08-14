# Strategy Platform V1.3 production bootstrap（no-trade）

本文档接续 canonical genesis，只允许对新的 `freqtrade_ai_v13` 执行。旧数据库不是 fallback、
迁移源或验收目标；任何命令都不得连接 `freqtrade_ai` 或 `freqtrade_ai_design_lab`。

## 1. 唯一角色映射

- `canonical_*` 是代码和 manifest 中稳定的逻辑 capability。
- `CanonicalRoleMapping` 是唯一物理映射入口；正式本机 contract 为
  `CanonicalRoleMapping.from_prefix("freqtrade_ai_v13_")`。
- installer、ACL renderer 和 verifier 必须接收同一个 mapping。缺项、额外项、重复物理角色、
  非 PostgreSQL-safe identifier 或 digest drift 均 fail closed。
- 15 个 capability roles 保持 `NOLOGIN/NOINHERIT`。schema owner/provisioner 永不作为服务
  LOGIN。

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

## 2. 最小 API principals 与 Keychain

只建立两个 LOGIN principals：

| LOGIN principal | 唯一 membership | Keychain service |
| --- | --- | --- |
| `freqtrade_ai_v13_api_login` | `freqtrade_ai_v13_api_reader` | `freqtrade-ai/v13/api-reader-password` |
| `freqtrade_ai_v13_control_login` | `freqtrade_ai_v13_control_writer` | `freqtrade-ai/v13/control-password` |

`scripts/canonical_v13_api_service.py provision` 仅在两个 LOGIN 和两个 Keychain item 全部不存在、
capability roles 完整且当前连接确为本机 `freqtrade_ai_v13` superuser 时执行。随机值只存在内存、
PostgreSQL password verifier 和 macOS Keychain；不会进入 argv、stdout/stderr、repo、Issue、plist
或 dotenv。任一已存在/半完成状态均 `BLOCKED`，绝不覆盖。

worker、research/backtest、runtime、order、fill、ledger principals 本阶段禁止创建。

## 3. Backup 与独立 restore acceptance

backup 输出目录必须位于 operator-controlled 非仓库位置，权限 `0700`；文件使用 custom format，
并单独记录 SHA-256。不得把 dump 或 digest receipt 提交到 Git。

```bash
umask 077
pg_dump --format=custom --file="$BACKUP_PATH" freqtrade_ai_v13
shasum -a 256 "$BACKUP_PATH"
```

恢复目标必须是一个此前不存在的唯一数据库名，由 `freqtrade_ai_v13_schema_owner` 拥有。恢复后
运行同一个 bootstrap verifier，要求 46 表、identity/digest、ACL/owner 和业务行 0 全部匹配。
验证库保留，除非取得独立删除授权。不得用旧库或现有测试库作为恢复目标。

## 4. 独立 loopback API

仅从 canonical main checkout 安装：

```bash
python scripts/canonical_v13_api_service.py install --port 8011
python scripts/canonical_v13_api_service.py status --port 8011
```

LaunchAgent plist 只含 Python/script/port 与非 secret 环境；服务子进程自行按固定 service/account
读取 Keychain，在内存构造两个 DSN，并调用
`app.canonical_v13.production:create_app`。绑定固定为 `127.0.0.1`，access log 关闭；它不 mount
legacy API、不执行 genesis、不激活配置、不启动 research/runtime/trading。

验收必须覆盖：

- `/healthz` = `HEALTHY` 且 `TRADING_DISABLED`；
- `/readyz` = `READY`，reader/control `current_user` 不同；
- strategies `EMPTY`、七类 P0 `UNSET`、market `MARKET_SNAPSHOT_UNSET`；
- research/runtime `BLOCKED`、optimization `PENDING_FIRST_BACKTEST`；
- control preview `BLOCKED` 且不写 bundle；
- 重启后结果一致、只有一个 LaunchAgent/process、所有业务域仍为 0。

UI/reverse proxy、公网访问、策略 intake、market、backtest、qualification、activation、runtime、
OKX 和交易链均属于后续独立授权门。
