# API Design

## Phase 0

Phase 0 只提供基础健康检查：

```text
GET /health
```

响应字段：

- `status`
- `app`
- `env`
- `database_enabled`
- `allow_live_trading`

## Phase 1 草案

后续 API 按资源分组：

- `/strategies`
- `/strategy-versions`
- `/generation-runs`
- `/backtest-runs`
- `/backtest-tasks`
- `/ranking`
- `/freq-ui`

## 安全边界

API 不返回真实密钥。交易、部署、生产写入、Dry-run 和 Live 相关接口默认不在 Phase 1 提供。
