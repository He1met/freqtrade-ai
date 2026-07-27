import { fetchJson } from "./http.ts";

export type OkxDemoReadinessStatus =
  | "READY"
  | "BLOCKED"
  | "FAILED"
  | "DRIFTED"
  | "STALE"
  | "UNKNOWN";

export type OkxDemoReadinessCheck = {
  key: "credentials" | "instrument" | "market" | "risk" | "writer" | "reconciliation";
  label: string;
  status: OkxDemoReadinessStatus;
  summary: string;
  action: string | null;
  observedAt: string | null;
};

export type OkxDemoRiskDecision = {
  databaseId: number;
  tradeIntentDatabaseId: number;
  decision: string;
  policyVersion: string;
  createdAt: string;
  reason: string | null;
};

export type OkxDemoIntent = {
  databaseId: number;
  intentId: string | null;
  clientOrderId: string;
  strategyVersionId: number | null;
  instrumentId: string | null;
  side: string | null;
  positionSide: string | null;
  orderType: string | null;
  quantity: string | null;
  limitPrice: string | null;
  leverage: string | null;
  marginMode: string | null;
  reduceOnly: boolean | null;
  status: string;
  expiresAt: string | null;
  createdAt: string;
};

export type OkxDemoFill = {
  databaseId: number;
  exchangeFillId: string;
  price: string;
  quantity: string;
  fee: string | null;
  createdAt: string;
};

export type OkxDemoOrder = {
  databaseId: number;
  tradeIntentDatabaseId: number;
  clientOrderId: string;
  exchangeOrderId: string | null;
  authoritativeSnapshotDatabaseId: number | null;
  authoritativeEventDatabaseId: number | null;
  fullChainDatabaseId: number | null;
  instrumentId: string | null;
  side: string | null;
  orderType: string | null;
  quantity: string | null;
  status: string;
  authoritativeStatus: string | null;
  filledQuantity: string | null;
  averagePrice: string | null;
  reduceOnly: boolean | null;
  authoritativeObservedAt: string | null;
  createdAt: string;
  updatedAt: string;
  completionState: "COMPLETE" | "INCOMPLETE";
  completionReason: string;
  riskDecision: OkxDemoRiskDecision | null;
  fills: OkxDemoFill[];
};

export type OkxDemoPosition = {
  databaseId: number;
  instrumentId: string;
  positionSide: string;
  quantity: string;
  averagePrice: string | null;
  observedAt: string;
  eventDatabaseId: number;
};

export type OkxDemoReconciliation = {
  databaseId: number;
  stateDatabaseId: number;
  status: string;
  openingFrozen: boolean;
  startedAt: string;
  completedAt: string | null;
  authoritativeObservedAt: string | null;
  artifactStatus: string;
  sourceType: string;
  coreData: boolean;
  reason: string | null;
};

export type OkxDemoLineage = {
  fullChainDatabaseId: number | null;
  strategyGenerationRunDatabaseId: number | null;
  strategyDatabaseId: number | null;
  strategyVersionDatabaseId: number | null;
  backtestRunDatabaseId: number | null;
  backtestTaskDatabaseId: number | null;
  backtestResultDatabaseId: number | null;
  strategyScoreDatabaseId: number | null;
  candidateApprovalDatabaseId: number | null;
  signalSnapshotDatabaseId: number | null;
  tradeIntentDatabaseId: number;
  riskDecisionDatabaseId: number | null;
  approvedExecutionDatabaseId: number | null;
  orderDatabaseId: number | null;
  fillDatabaseId: number | null;
  exchangeOrderId: string | null;
  authoritativeOrderSnapshotDatabaseId: number | null;
  authoritativeEventDatabaseId: number | null;
  reconciliationDatabaseId: number | null;
  reconciliationStateDatabaseId: number | null;
};

export type OkxDemoObservability = {
  generatedAt: string;
  sourceType: "api_aggregate";
  coreData: true;
  target: {
    targetId: "OKX_DEMO";
    label: "OKX_DEMO / 模拟盘";
    exchange: "okx";
    productType: "SWAP";
    marginMode: "isolated";
    accountMode: "demo";
    simulatedTrading: true;
    allowRealFunds: false;
  };
  readiness: OkxDemoReadinessCheck[];
  intents: OkxDemoIntent[];
  orders: OkxDemoOrder[];
  positions: OkxDemoPosition[];
  account: {
    status: "READY" | "STALE" | "NOT_AVAILABLE";
    reason: string;
    databaseId: number | null;
    eventDatabaseId: number | null;
    equity: string | null;
    availableBalance: string | null;
    marginBalance: string | null;
    observedAt: string | null;
  };
  latestReconciliation: OkxDemoReconciliation | null;
  lineage: OkxDemoLineage[];
  acceptanceState: "ACCEPTABLE" | "NOT_ACCEPTABLE";
  acceptanceReason: string;
};

type RawRecord = Record<string, unknown>;

function record(value: unknown): RawRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as RawRecord
    : {};
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function numericId(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function requiredText(value: unknown, field: string): string {
  const result = text(value);
  if (!result) throw new Error(`OKX Demo API 缺少 ${field}`);
  return result;
}

function requiredId(value: unknown, field: string): number {
  const result = numericId(value);
  if (!result) throw new Error(`OKX Demo API 缺少 ${field}`);
  return result;
}

function requiredBoolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") throw new Error(`OKX Demo API 缺少 ${field}`);
  return value;
}

function decimal(value: unknown): string | null {
  return typeof value === "number" || typeof value === "string" ? String(value) : null;
}

function normalizeDecision(value: unknown): OkxDemoRiskDecision {
  const item = record(value);
  return {
    databaseId: requiredId(item.database_id, "风控数据库 ID"),
    tradeIntentDatabaseId: requiredId(item.trade_intent_database_id, "TradeIntent 数据库 ID"),
    decision: requiredText(item.decision, "风控决定"),
    policyVersion: requiredText(item.policy_version, "风控策略版本"),
    createdAt: requiredText(item.created_at, "风控创建时间"),
    reason: text(item.reason),
  };
}

function normalizeOrder(value: unknown): OkxDemoOrder {
  const item = record(value);
  const riskDecision = record(item.risk_decision);
  const completionState = requiredText(item.completion_state, "完成状态");
  if (completionState !== "COMPLETE" && completionState !== "INCOMPLETE") {
    throw new Error("OKX Demo API 返回未知完成状态");
  }
  return {
    databaseId: requiredId(item.database_id, "订单数据库 ID"),
    tradeIntentDatabaseId: requiredId(item.trade_intent_database_id, "TradeIntent 数据库 ID"),
    clientOrderId: requiredText(item.client_order_id, "客户端订单 ID"),
    exchangeOrderId: text(item.exchange_order_id),
    authoritativeSnapshotDatabaseId: numericId(item.authoritative_snapshot_database_id),
    authoritativeEventDatabaseId: numericId(item.authoritative_event_database_id),
    fullChainDatabaseId: numericId(item.full_chain_database_id),
    instrumentId: text(item.instrument_id),
    side: text(item.side),
    orderType: text(item.order_type),
    quantity: decimal(item.quantity),
    status: requiredText(item.status, "订单状态"),
    authoritativeStatus: text(item.authoritative_status),
    filledQuantity: decimal(item.filled_quantity),
    averagePrice: decimal(item.average_price),
    reduceOnly: typeof item.reduce_only === "boolean" ? item.reduce_only : null,
    authoritativeObservedAt: text(item.authoritative_observed_at),
    createdAt: requiredText(item.created_at, "订单创建时间"),
    updatedAt: requiredText(item.updated_at, "订单更新时间"),
    completionState,
    completionReason: requiredText(item.completion_reason, "完成判定原因"),
    riskDecision: Object.keys(riskDecision).length ? normalizeDecision(riskDecision) : null,
    fills: list(item.fills).map((fill) => {
      const row = record(fill);
      return {
        databaseId: requiredId(row.database_id, "成交数据库 ID"),
        exchangeFillId: requiredText(row.exchange_fill_id, "交易所成交 ID"),
        price: requiredText(decimal(row.price), "成交价格"),
        quantity: requiredText(decimal(row.quantity), "成交数量"),
        fee: decimal(row.fee),
        createdAt: requiredText(row.created_at, "成交时间"),
      };
    }),
  };
}

export function normalizeOkxDemoObservability(value: unknown): OkxDemoObservability {
  const payload = record(value);
  const target = record(payload.target);
  if (
    target.target_id !== "OKX_DEMO"
    || target.exchange !== "okx"
    || target.product_type !== "SWAP"
    || target.margin_mode !== "isolated"
    || target.account_mode !== "demo"
    || target.simulated_trading !== true
    || target.allow_real_funds !== false
    || payload.source_type !== "api_aggregate"
    || payload.core_data !== true
  ) {
    throw new Error("OKX Demo API 目标或数据来源不符合安全契约");
  }
  const acceptanceState = payload.acceptance_state;
  if (acceptanceState !== "ACCEPTABLE" && acceptanceState !== "NOT_ACCEPTABLE") {
    throw new Error("OKX Demo API 缺少严格验收状态");
  }
  return {
    generatedAt: requiredText(payload.generated_at, "生成时间"),
    sourceType: "api_aggregate",
    coreData: true,
    target: {
      targetId: "OKX_DEMO",
      label: "OKX_DEMO / 模拟盘",
      exchange: "okx",
      productType: "SWAP",
      marginMode: "isolated",
      accountMode: "demo",
      simulatedTrading: true,
      allowRealFunds: false,
    },
    readiness: list(payload.readiness).map((value) => {
      const item = record(value);
      const key = requiredText(item.key, "readiness key");
      const status = requiredText(item.status, "readiness 状态");
      if (!["credentials", "instrument", "market", "risk", "writer", "reconciliation"].includes(key)) {
        throw new Error("OKX Demo API 返回未知 readiness key");
      }
      if (!["READY", "BLOCKED", "FAILED", "DRIFTED", "STALE", "UNKNOWN"].includes(status)) {
        throw new Error("OKX Demo API 返回未知 readiness 状态");
      }
      return {
        key: key as OkxDemoReadinessCheck["key"],
        label: requiredText(item.label, "readiness 名称"),
        status: status as OkxDemoReadinessStatus,
        summary: requiredText(item.summary, "readiness 摘要"),
        action: text(item.action),
        observedAt: text(item.observed_at),
      };
    }),
    intents: list(payload.intents).map((value) => {
      const item = record(value);
      return {
        databaseId: requiredId(item.database_id, "TradeIntent 数据库 ID"),
        intentId: text(item.intent_id),
        clientOrderId: requiredText(item.client_order_id, "客户端订单 ID"),
        strategyVersionId: numericId(item.strategy_version_id),
        instrumentId: text(item.instrument_id),
        side: text(item.side),
        positionSide: text(item.position_side),
        orderType: text(item.order_type),
        quantity: decimal(item.quantity),
        limitPrice: decimal(item.limit_price),
        leverage: decimal(item.leverage),
        marginMode: text(item.margin_mode),
        reduceOnly: typeof item.reduce_only === "boolean" ? item.reduce_only : null,
        status: requiredText(item.status, "TradeIntent 状态"),
        expiresAt: text(item.expires_at),
        createdAt: requiredText(item.created_at, "TradeIntent 创建时间"),
      };
    }),
    orders: list(payload.orders).map(normalizeOrder),
    positions: list(payload.positions).map((value) => {
      const item = record(value);
      return {
        databaseId: requiredId(item.database_id, "仓位数据库 ID"),
        instrumentId: requiredText(item.instrument_id, "仓位合约"),
        positionSide: requiredText(item.position_side, "仓位方向"),
        quantity: requiredText(decimal(item.quantity), "仓位数量"),
        averagePrice: decimal(item.average_price),
        observedAt: requiredText(item.observed_at, "仓位观测时间"),
        eventDatabaseId: requiredId(item.event_database_id, "仓位事件数据库 ID"),
      };
    }),
    account: (() => {
      const item = record(payload.account);
      const status = requiredText(item.status, "账户状态");
      if (!["READY", "STALE", "NOT_AVAILABLE"].includes(status)) {
        throw new Error("OKX Demo API 返回未知账户状态");
      }
      return {
        status: status as OkxDemoObservability["account"]["status"],
        reason: requiredText(item.reason, "账户快照原因"),
        databaseId: numericId(item.database_id),
        eventDatabaseId: numericId(item.event_database_id),
        equity: decimal(item.equity),
        availableBalance: decimal(item.available_balance),
        marginBalance: decimal(item.margin_balance),
        observedAt: text(item.observed_at),
      };
    })(),
    latestReconciliation: (() => {
      const item = record(payload.latest_reconciliation);
      if (!Object.keys(item).length) return null;
      return {
        databaseId: requiredId(item.database_id, "对账数据库 ID"),
        stateDatabaseId: requiredId(item.state_database_id, "对账状态数据库 ID"),
        status: requiredText(item.status, "对账状态"),
        openingFrozen: requiredBoolean(item.opening_frozen, "对账冻结状态"),
        startedAt: requiredText(item.started_at, "对账开始时间"),
        completedAt: text(item.completed_at),
        authoritativeObservedAt: text(item.authoritative_observed_at),
        artifactStatus: requiredText(item.artifact_status, "对账 artifact 状态"),
        sourceType: requiredText(item.source_type, "对账来源"),
        coreData: item.core_data === true,
        reason: text(item.reason),
      };
    })(),
    lineage: list(payload.lineage).map((value) => {
      const item = record(value);
      return {
        fullChainDatabaseId: numericId(item.full_chain_database_id),
        strategyGenerationRunDatabaseId: numericId(
          item.strategy_generation_run_database_id,
        ),
        strategyDatabaseId: numericId(item.strategy_database_id),
        strategyVersionDatabaseId: numericId(item.strategy_version_database_id),
        backtestRunDatabaseId: numericId(item.backtest_run_database_id),
        backtestTaskDatabaseId: numericId(item.backtest_task_database_id),
        backtestResultDatabaseId: numericId(item.backtest_result_database_id),
        strategyScoreDatabaseId: numericId(item.strategy_score_database_id),
        candidateApprovalDatabaseId: numericId(item.candidate_approval_database_id),
        signalSnapshotDatabaseId: numericId(item.signal_snapshot_database_id),
        tradeIntentDatabaseId: requiredId(item.trade_intent_database_id, "lineage TradeIntent ID"),
        riskDecisionDatabaseId: numericId(item.risk_decision_database_id),
        approvedExecutionDatabaseId: numericId(item.approved_execution_database_id),
        orderDatabaseId: numericId(item.order_database_id),
        fillDatabaseId: numericId(item.fill_database_id),
        exchangeOrderId: text(item.exchange_order_id),
        authoritativeOrderSnapshotDatabaseId: numericId(
          item.authoritative_order_snapshot_database_id,
        ),
        authoritativeEventDatabaseId: numericId(item.authoritative_event_database_id),
        reconciliationDatabaseId: numericId(item.reconciliation_database_id),
        reconciliationStateDatabaseId: numericId(item.reconciliation_state_database_id),
      };
    }),
    acceptanceState,
    acceptanceReason: requiredText(payload.acceptance_reason, "验收原因"),
  };
}

export async function fetchOkxDemoObservability(signal?: AbortSignal): Promise<OkxDemoObservability> {
  return normalizeOkxDemoObservability(
    await fetchJson<unknown>("/okx-demo/observability", signal),
  );
}
