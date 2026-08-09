import { fetchJson } from "./http";
import { normalizeOkxDemoRuntimeActivity } from "./okxDemoRuntimeActivityModel";

export { normalizeOkxDemoRuntimeActivity } from "./okxDemoRuntimeActivityModel";

export type OkxDemoRuntimeActivity = {
  schema_version: "okx-demo-runtime-activity-v1";
  as_of: string;
  source_type: "database";
  core_data: true;
  execution_target: "OKX_DEMO";
  allow_real_funds: false;
  real_orders: false;
  active_deployments: Array<{
    deployment_id: number;
    status: "ACTIVE";
    active_slot: number;
    instrument_id: string;
    timeframe: string;
    strategy_id: number;
    strategy_name: string;
    strategy_version_id: number;
    strategy_version_number: number;
    candidate_approval_id: number;
    candidate_approval_status: string;
    created_at: string;
  }>;
  recent_signal_evaluations: Array<{
    evaluation_id: number;
    deployment_id: number;
    instrument_id: string;
    timeframe: string;
    closed_candle_at: string;
    status: "PENDING" | "LEASED" | "NO_ACTION" | "ACTIONABLE" | "BLOCKED" | "FAILED";
    completed_at: string | null;
    error_code: string | null;
    created_at: string;
  }>;
  signal_window: { returned_count: number; limit: number; has_more: boolean };
};

export function fetchOkxDemoRuntimeActivity(signal?: AbortSignal) {
  return fetchJson<unknown>("/okx-demo/runtime-activity?signal_limit=20", signal)
    .then(normalizeOkxDemoRuntimeActivity);
}
