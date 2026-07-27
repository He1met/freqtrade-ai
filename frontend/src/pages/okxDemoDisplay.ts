import type { OkxDemoObservability, OkxDemoOrder } from "../api/okxDemoApi";

const COMPLETE_RECONCILIATION = new Set(["RECONCILED", "RECOVERED"]);

export function orderCanDisplayComplete(
  order: OkxDemoOrder,
  data: Pick<OkxDemoObservability, "latestReconciliation">,
): boolean {
  return Boolean(
    order.databaseId > 0
    && order.fullChainDatabaseId
    && order.fills.length > 0
    && order.exchangeOrderId
    && order.authoritativeSnapshotDatabaseId
    && order.authoritativeEventDatabaseId
    && order.tradeIntentDatabaseId > 0
    && order.riskDecision?.databaseId
    && order.riskDecision.decision === "APPROVED"
    && data.latestReconciliation?.databaseId
    && data.latestReconciliation.stateDatabaseId
    && data.latestReconciliation.completedAt
    && data.latestReconciliation.authoritativeObservedAt
    && data.latestReconciliation.artifactStatus === "READY"
    && data.latestReconciliation.sourceType === "api_aggregate"
    && data.latestReconciliation.coreData
    && !data.latestReconciliation.openingFrozen
    && COMPLETE_RECONCILIATION.has(data.latestReconciliation.status)
    && order.completionState === "COMPLETE",
  );
}

export function okxDemoAcceptanceIsTruthful(data: OkxDemoObservability): boolean {
  const readinessKeys = new Set(data.readiness.map((check) => check.key));
  const accountReady = Boolean(
    data.account.status === "READY"
    && data.account.databaseId
    && data.account.eventDatabaseId
    && data.account.equity !== null
    && data.account.availableBalance !== null
    && data.account.marginBalance !== null
    && data.account.observedAt,
  );
  return (
    data.sourceType === "api_aggregate"
    && data.coreData
    && data.orders.length > 0
    && data.orders.every((order) => orderCanDisplayComplete(order, data))
    && accountReady
    && data.readiness.length === 6
    && readinessKeys.size === 6
    && ["credentials", "instrument", "market", "risk", "writer", "reconciliation"]
      .every((key) => readinessKeys.has(key as OkxDemoObservability["readiness"][number]["key"]))
    && data.readiness.every((check) => check.status === "READY")
    && data.acceptanceState === "ACCEPTABLE"
  );
}

export function statusTone(status: string): "success" | "danger" | "warning" | "info" | "neutral" {
  const normalized = status.toUpperCase();
  if (["READY", "APPROVED", "COMPLETE", "RECONCILED", "RECOVERED", "ACCEPTABLE"].includes(normalized)) {
    return "success";
  }
  if (["FAILED", "REJECTED", "DRIFTED"].includes(normalized)) return "danger";
  if (["BLOCKED", "STALE", "EXPIRED", "INCOMPLETE", "NOT_ACCEPTABLE"].includes(normalized)) {
    return "warning";
  }
  if (normalized === "UNKNOWN") return "info";
  return "neutral";
}
