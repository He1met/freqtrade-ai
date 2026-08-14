# Strategy Platform V1.3 canonical genesis 与独立 API 接入

本 runbook 只定义未来获批后的生产接入顺序。本地审计不得据此连接共享/生产数据库，
也不得把离线 SQL、SQLite 测试或 CI fixture 声称为真实 PostgreSQL 验收。

## 1. 不可变边界

- 目标必须是新建、专用、空的 PostgreSQL database；`public` 或其他用户 schema 中存在
  table/view/materialized view/sequence/function/custom type 时均为
  `BLOCKED_NON_EMPTY_CANONICAL_DATABASE`。
- 旧 `freqtrade_ai`、`freqtrade_ai_design_lab`、v47 marker `20260813_47` 只允许作为
  外部只读历史来源，禁止成为 canonical database、连接 fallback 或 migration bootstrap。
- `backend/app/main.py` 继续承载 legacy API，不 import/mount canonical，不执行 canonical
  genesis。canonical API 使用独立进程和独立连接角色。
- genesis 只创建 46-table canonical manifest 与一条 identity；业务行必须为 0，七类 P0
  配置、target/count/per-target cap、market snapshot、activation 均保持 `UNSET/BLOCKED`。
- genesis、owner/ACL、activation、research、runtime、order 是不同授权动作。完成前一项不
  授权后一项。

## 2. 变更窗口前置门

在任何真实连接前，由数据库管理员分别确认并留存：

1. 精确 database name/host、空库创建 receipt 和不指向 legacy 的双人复核；
2. 可恢复备份/快照、恢复演练位置与回滚负责人；
3. `canonical_schema_owner` 为 `NOLOGIN` owner；manifest 中 reader/writer roles 已存在，
   没有 membership 继承或组合超权角色；
4. provisioning identity 有受控 DDL/GRANT/owner-transfer 权限，但不是 application runtime；
5. 维护窗口、审计 actor/request id 和失败后禁止服务接入的 fence。

任一证据缺失即 `BLOCKED`，不得用放宽 ACL、复用 legacy owner 或预置业务 fixture 绕过。

## 3. 只读 dry-run（不连接数据库）

在已安装 `backend/requirements.txt` 的隔离环境、仓库根目录执行以下离线渲染。它只编译
SQLAlchemy PostgreSQL dialect，不创建 engine 或连接：

```bash
cd backend
python - <<'PY'
from app.canonical_v13.genesis import (
    assert_postgresql_acl_sql,
    render_postgresql_acl_sql,
    render_postgresql_genesis_ddl,
    render_postgresql_owner_sql,
)

ddl = render_postgresql_genesis_ddl()
acl = render_postgresql_acl_sql()
owner = render_postgresql_owner_sql()
assert_postgresql_acl_sql(acl)
print({"ddl_bytes": len(ddl), "acl_bytes": len(acl), "owner_bytes": len(owner)})
PY
```

`render_postgresql_genesis_ddl()` 是评审证据，不是可独立执行的 installer：它没有动态
installer identity 行。不得把“SQL 可编译”记录为真实 migration/ACL 成功。

## 4. 获批后的单事务安装顺序

只有新的空 database、备份和角色前置门全部通过后，管理员才能在一个 PostgreSQL DDL
事务中按以下唯一顺序执行：

1. `BEGIN`；
2. 调用 `install_canonical_genesis(connection, installer_identity=<reviewed actor>)`；
   installer 会先做 database-wide user-object preflight，再只使用独立
   `CanonicalBase.metadata` 创建 schema/tables/indexes，插入 identity，并确认业务行 0；
3. 应用 `render_postgresql_acl_sql()` 的 exact per-table revoke/grant；
4. 最后应用 `render_postgresql_owner_sql()`，先逐表转给 `canonical_schema_owner`，最后转移
   schema owner；
5. 在同一事务内以 provisioning identity 再运行 `verify_canonical_genesis(...,
   require_zero_business_rows=True)`；
6. 所有结果完全匹配才 `COMMIT`，否则 `ROLLBACK`。

禁止使用 legacy `app.db.migrate`、legacy `Base.metadata.create_all()`、v47 migration registry
或 application startup 代替上述入口。重复调用只允许 exact identity/schema/database isolation
成立时返回 no-op；不得自动修复 partial/drift schema。

如果在 identity 写入后 ACL/owner 步骤失败，同一事务必须整体回滚。若因外部操作导致无法
证明整体回滚，database 状态为 `UNKNOWN/BLOCKED`，禁止启动 API，交由管理员恢复快照或
销毁该专用新库后重建；不得删除旧数据库或历史证据。

## 5. 独立 API composition root

canonical API 只能由 `app.canonical_v13.production:create_app` factory 启动。它要求两个
显式环境变量，且两个 PostgreSQL role 必须不同并指向同一 dedicated database：

- `FREQTRADE_AI_CANONICAL_V13_READER_DATABASE_URL`
- `FREQTRADE_AI_CANONICAL_V13_CONTROL_DATABASE_URL`

它明确忽略 legacy `DATABASE_URL`，不会安装 genesis、修复 schema、activation、启动 research
或 runtime。GET projection 只使用 reader；POST control command 只使用 control writer。
每个请求先验证 canonical database identity；不匹配返回 fail-closed 503/409，不 fallback。

开发环境 Vite 将 `/api/canonical-v13` 单独转发到 `127.0.0.1:8001`，其他 `/api` 仍指向
legacy `127.0.0.1:8000`。生产 reverse proxy 必须做同样的 longest-prefix 分流；禁止把
canonical DSN 配给 legacy process，或把 legacy router mount 到 canonical process。

## 6. 独立验收与仍然 BLOCKED 的门

提交审查前可接受的本地证据仅包括：46-table manifest、离线 DDL/ACL、source-layout import、
独立 OpenAPI routes、SQLite domain/API tests 和 mocked browser tests。

真实接入仍须分别取得：

- PostgreSQL 16 空库安装/rollback/repeat-noop、owner 与每个 reader/writer ACL 实证；
- reader/control 两个最小角色的 route-level smoke，证明 writer 不因 identity guard 越权读表；
- backup/restore receipt、database-wide non-legacy inventory 和 reverse-proxy routing evidence；
- 后续 market、首次真实 backtest、qualification、approval、runtime/交易链各自的显式授权。

这些门通过前，production activation/runtime/trading 必须保持 `BLOCKED`。
