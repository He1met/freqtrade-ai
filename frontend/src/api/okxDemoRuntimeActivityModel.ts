import type { OkxDemoRuntimeActivity } from "./okxDemoRuntimeActivityApi";

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("runtime activity response is not an object");
  return value as Record<string, unknown>;
}

export function normalizeOkxDemoRuntimeActivity(value: unknown): OkxDemoRuntimeActivity {
  const raw = asRecord(value);
  if (raw.schema_version !== "okx-demo-runtime-activity-v1"
    || raw.execution_target !== "OKX_DEMO"
    || raw.allow_real_funds !== false
    || raw.real_orders !== false
    || raw.source_type !== "database"
    || raw.core_data !== true
    || !Array.isArray(raw.active_deployments)
    || !Array.isArray(raw.recent_signal_evaluations)) {
    throw new Error("runtime activity safety contract mismatch");
  }
  for (const item of raw.active_deployments) {
    const deployment = asRecord(item);
    if (deployment.status !== "ACTIVE" || typeof deployment.deployment_id !== "number") {
      throw new Error("runtime activity deployment contract mismatch");
    }
  }
  for (const item of raw.recent_signal_evaluations) {
    const evaluation = asRecord(item);
    if (typeof evaluation.evaluation_id !== "number" || typeof evaluation.status !== "string") {
      throw new Error("runtime activity signal contract mismatch");
    }
  }
  return raw as OkxDemoRuntimeActivity;
}
