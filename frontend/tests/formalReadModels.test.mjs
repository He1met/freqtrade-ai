import assert from "node:assert/strict";
import test from "node:test";

import { normalizeOkxDemoRuntimeActivity } from "../src/api/okxDemoRuntimeActivityModel.ts";

const safeRuntime = {
  schema_version: "okx-demo-runtime-activity-v1",
  as_of: "2026-08-09T00:00:00Z",
  source_type: "database",
  core_data: true,
  execution_target: "OKX_DEMO",
  allow_real_funds: false,
  real_orders: false,
  active_deployments: [],
  recent_signal_evaluations: [],
  signal_window: { returned_count: 0, limit: 20, has_more: false },
};

test("runtime activity accepts only the explicit Demo-only safety contract", () => {
  assert.equal(normalizeOkxDemoRuntimeActivity(safeRuntime).execution_target, "OKX_DEMO");
  assert.throws(() => normalizeOkxDemoRuntimeActivity({ ...safeRuntime, execution_target: "LIVE" }), /safety contract/);
  assert.throws(() => normalizeOkxDemoRuntimeActivity({ ...safeRuntime, allow_real_funds: true }), /safety contract/);
  assert.throws(() => normalizeOkxDemoRuntimeActivity({ ...safeRuntime, real_orders: true }), /safety contract/);
});
