import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalSelectionState,
  configurationContextOptions,
  filterCanonicalSelectorOptions,
  marketProfileSelectorOptions,
  marketSnapshotSelectorOptions,
  marketTargetSelectorOptions,
  researchPlanSelectorOptions,
  researchTargetSelectorOptions,
  strategySelectorOptions,
} from "../src/pages/canonicalV13/canonicalV13Selectors.ts";

const ID_A = "123e4567-e89b-42d3-a456-426614174000";
const ID_B = "123e4567-e89b-42d3-a456-426614174001";
const ID_C = "123e4567-e89b-42d3-a456-426614174002";
const ID_D = "123e4567-e89b-42d3-a456-426614174003";
const DIGEST = "a".repeat(64);

const VERSION = {
  version_id: ID_B,
  version_number: 1,
  lifecycle_status: "VALIDATED",
  schema_json: {},
  payload_json: {},
  schema_digest: DIGEST,
  payload_digest: DIGEST,
  adapter_identity: "adapter",
  adapter_digest: DIGEST,
  snapshot_id: ID_C,
  snapshot_digest: DIGEST,
  created_at: "2026-08-14T00:00:00Z",
  validated_at: "2026-08-14T01:00:00Z",
};

const STRATEGY = {
  strategy_id: ID_A,
  display_name: "Alpha",
  catalog_status: "DRAFT",
  intake_status: "INTAKE_ACCEPTED",
  current_version_id: ID_B,
  version_number: 1,
  artifact_id: ID_D,
  artifact_digest: DIGEST,
  validation_status: "VALIDATED",
  qualification_status: "NOT_EVALUATED",
  execution_authorized: false,
  created_at: "2026-08-14T00:00:00Z",
};

const PLAN = {
  validation_plan_id: ID_C,
  validation_plan_digest: DIGEST,
  strategy_version_id: ID_B,
  research_target_id: ID_D,
  target_key: "btc-5m",
  plan_status: "READY",
  validation_attempt_id: null,
  attempt_status: null,
  attempt_receipt_digest: null,
  target_score_id: null,
  overall_score: null,
  score_digest: null,
  qualification_decision_id: null,
  qualification_status: null,
  qualification_reason_code: null,
  qualification_decision_digest: null,
};

test("configuration contexts are deduplicated API pairs without invented defaults", () => {
  const options = configurationContextOptions({
    status: "AVAILABLE",
    configured_kinds: ["TARGET", "WINDOW"],
    unset_kinds: [],
    items: [
      { profile_id: ID_A, profile_key: "target-main", configuration_kind: "TARGET", scope_key: "scope-a", workflow_key: "research", versions: [VERSION] },
      { profile_id: ID_B, profile_key: "window-main", configuration_kind: "WINDOW", scope_key: "scope-a", workflow_key: "research", versions: [VERSION] },
    ],
  });
  assert.equal(options.length, 1);
  assert.equal(options[0].label, "scope-a / research");
  assert.equal(options[0].scopeKey, "scope-a");
  assert.equal(options[0].workflowKey, "research");
  assert.match(options[0].description, /2 个配置 Profile/);
});

test("strategy plan and target options retain API IDs only as submitted values", () => {
  const strategies = strategySelectorOptions({ status: "AVAILABLE", items: [STRATEGY] });
  const targets = researchTargetSelectorOptions({ status: "AVAILABLE", items: [PLAN] }, STRATEGY);
  const plans = researchPlanSelectorOptions({ status: "AVAILABLE", items: [PLAN] }, STRATEGY, ID_D);
  assert.equal(strategies[0].value, ID_A);
  assert.equal(targets[0].value, ID_D);
  assert.equal(plans[0].value, ID_C);
  for (const option of [...strategies, ...targets, ...plans]) {
    assert.equal(option.label.includes(option.value), false);
    assert.equal(option.description.includes(option.value), false);
  }
  assert.equal(plans[0].status, "READY");
  assert.equal(targets[0].label, "btc-5m");
});

test("research options never cross the selected strategy version or target lineage", () => {
  const anotherPlan = { ...PLAN, validation_plan_id: ID_D, strategy_version_id: ID_A, research_target_id: ID_C, target_key: "eth-5m" };
  const catalog = { status: "AVAILABLE", items: [PLAN, anotherPlan] };
  assert.deepEqual(researchTargetSelectorOptions(catalog, STRATEGY).map((item) => item.value), [ID_D]);
  assert.deepEqual(researchPlanSelectorOptions(catalog, STRATEGY, ID_D).map((item) => item.value), [ID_C]);
  assert.deepEqual(researchPlanSelectorOptions(catalog, STRATEGY, ID_C), []);
});

test("market profile snapshot and target options remain contextual", () => {
  const inventory = {
    status: "AVAILABLE",
    profile_count: 1,
    validated_profile_count: 1,
    artifact_count: 1,
    accepted_receipt_count: 1,
    profiles: [{
      market_profile_id: ID_A,
      profile_key: "okx-btc-5m",
      scope_key: "scope-a",
      version_id: ID_B,
      version_number: 2,
      lifecycle_status: "VALIDATED",
      payload_digest: DIGEST,
      created_at: "2026-08-14T00:00:00Z",
      validated_at: "2026-08-14T01:00:00Z",
    }],
    snapshots: [{ snapshot_id: ID_C, snapshot_digest: DIGEST, market_profile_version_id: ID_B, member_count: 1, created_at: "2026-08-14T02:00:00Z" }],
  };
  const snapshot = {
    snapshot_id: ID_C,
    snapshot_digest: DIGEST,
    market_profile_version_id: ID_B,
    status: "ACCEPTED",
    reason_codes: [],
    members: [{
      market_artifact_id: ID_A,
      artifact_digest: DIGEST,
      market_receipt_id: ID_B,
      receipt_digest: DIGEST,
      receipt_status: "ACCEPTED",
      research_target_id: ID_D,
      target_key: "btc-5m",
      coverage_start: "2026-08-13T00:00:00Z",
      coverage_end: "2026-08-14T00:00:00Z",
      coverage_digest: DIGEST,
    }],
    created_at: "2026-08-14T02:00:00Z",
  };
  assert.deepEqual(marketProfileSelectorOptions(inventory).map((item) => item.value), [ID_B]);
  assert.deepEqual(marketSnapshotSelectorOptions(inventory, ID_B).map((item) => item.value), [ID_C]);
  assert.deepEqual(marketTargetSelectorOptions(snapshot).map((item) => item.value), [ID_D]);
  assert.equal(marketProfileSelectorOptions(inventory)[0].label, "okx-btc-5m · 版本 2");
});

test("search uses display context while stale committed IDs remain explicit", () => {
  const options = strategySelectorOptions({ status: "AVAILABLE", items: [STRATEGY] });
  assert.equal(filterCanonicalSelectorOptions(options, "alpha").length, 1);
  assert.equal(filterCanonicalSelectorOptions(options, "已验证").length, 1);
  assert.equal(filterCanonicalSelectorOptions(options, ID_A).length, 0);
  assert.equal(canonicalSelectionState(options, ""), "unselected");
  assert.equal(canonicalSelectionState(options, ID_A), "selected");
  assert.equal(canonicalSelectionState(options, ID_D), "stale");
});

test("empty API projections yield no selector fallback", () => {
  assert.deepEqual(strategySelectorOptions({ status: "EMPTY", items: [] }), []);
  assert.deepEqual(researchTargetSelectorOptions({ status: "EMPTY", items: [] }, STRATEGY), []);
  assert.deepEqual(researchPlanSelectorOptions({ status: "EMPTY", items: [] }, STRATEGY, null), []);
});
