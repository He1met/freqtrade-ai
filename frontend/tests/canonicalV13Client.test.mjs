import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  activateCanonicalResearchBundle,
  CANONICAL_V13_API_ROOT,
  CanonicalV13ApiError,
  createCanonicalConfigurationDraft,
  fetchCanonicalConfigurations,
  fetchCanonicalMarketInventory,
  fetchCanonicalMarketSnapshot,
  fetchCanonicalOptimizations,
  fetchCanonicalResearchReadiness,
  fetchCanonicalResearchGates,
  fetchCanonicalResearchChain,
  fetchCanonicalResearchPlans,
  fetchCanonicalResearchResults,
  fetchCanonicalRuntimeReadiness,
  fetchCanonicalStrategies,
  fetchCanonicalStrategy,
  previewCanonicalResearchBundle,
  submitCanonicalStrategy,
  validateCanonicalConfiguration,
} from "../src/api/canonicalV13Client.ts";

const ID = "123e4567-e89b-42d3-a456-426614174000";
const DIGEST = "a".repeat(64);

test("the client exposes the canonical projection routes with one fetch each", async (context) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async (input, init) => {
    calls.push({ input: String(input), method: init?.method ?? "GET" });
    return Response.json({
      artifact_count: 0,
      bundle_digest: DIGEST,
      catalog_status: "DRAFT",
      configuration_activation_id: ID,
      configuration_bundle_id: ID,
      configuration_bundle_digest: DIGEST,
      configuration_kind: "TARGET",
      configured_kinds: [],
      created_bundle: false,
      deployment_id: null,
      display_name: "Strategy",
      execution_authorized: false,
      execution_side_effects: 0,
      idempotency_receipt_id: ID,
      idempotent_replay: false,
      intake_status: "INTAKE_ACCEPTED",
      items: [],
      lifecycle_status: "DRAFT",
      members: [],
      market_snapshot_id: ID,
      market_snapshot_digest: DIGEST,
      profile_count: 0,
      profile_id: ID,
      profiles: [],
      prospective_bundle_id: null,
      qualification_status: "NOT_EVALUATED",
      reason_codes: [],
      receipt_digest: DIGEST,
      repeat_noop: false,
      runtime_instance_id: null,
      snapshot_digest: DIGEST,
      snapshot_id: ID,
      snapshot_digests: {},
      snapshot_ids: {},
      snapshots: [],
      status: "EMPTY",
      strategy_id: ID,
      strategy_version_id: ID,
      submission_id: ID,
      unset_kinds: [],
      validation_status: "UNVALIDATED",
      validation_plan_id: ID,
      validation_plan_digest: DIGEST,
      research_target_id: ID,
      target_key: "btc-5m",
      plan_status: "READY",
      validation_attempt_id: null,
      attempt_status: null,
      attempt_receipt_digest: null,
      target_score_id: null,
      overall_score: null,
      score_digest: null,
      qualification_decision_id: null,
      qualification_reason_code: null,
      qualification_decision_digest: null,
      attempt: null,
      windows: [],
      score: null,
      qualification: null,
      version_id: ID,
    });
  };
  const draft = {
    actor_identity: "canonical-p0-operator",
    adapter_digest: DIGEST,
    adapter_identity: "adapter",
    dependencies: [],
    idempotency_key: "draft-target-v1",
    payload_json: {},
    profile_key: "profile",
    schema_json: {},
    scope_key: "scope",
    workflow_key: "workflow",
  };
  const preview = { market_snapshot_id: null, scope_key: "scope", snapshot_ids: {}, workflow_key: "workflow" };
  await submitCanonicalStrategy({
    archive_snapshot_digest: DIGEST,
    caller_identity: "caller",
    current_version_id: "version-1",
    display_name: "Strategy",
    idempotency_key: "key",
    source_entry_key: "archive/a.py",
    source_strategy_key: "source",
    versions: [{ artifact_base64: "eA==", source_strategy_key: "source", version_id: "version-1", version_number: 1 }],
  });
  await fetchCanonicalStrategies();
  await fetchCanonicalStrategy(ID);
  await fetchCanonicalConfigurations();
  await createCanonicalConfigurationDraft("TARGET", draft);
  await validateCanonicalConfiguration("TARGET", ID, {
    actor_identity: "canonical-p0-operator",
    adapter_manifest_digest: DIGEST,
    idempotency_key: "validate-target-v1",
  });
  await previewCanonicalResearchBundle(preview);
  await activateCanonicalResearchBundle(ID, { ...preview, actor_identity: "actor", expected_bundle_digest: DIGEST });
  await fetchCanonicalMarketInventory();
  await fetchCanonicalMarketSnapshot(ID);
  await fetchCanonicalResearchReadiness("scope", "workflow");
  await fetchCanonicalRuntimeReadiness();
  await fetchCanonicalOptimizations();
  await fetchCanonicalResearchChain(ID);
  await fetchCanonicalResearchPlans();
  await fetchCanonicalResearchResults(ID);
  await fetchCanonicalResearchGates();

  assert.equal(calls.length, 17);
  assert.deepEqual(calls, [
    { input: `${CANONICAL_V13_API_ROOT}/submissions`, method: "POST" },
    { input: `${CANONICAL_V13_API_ROOT}/strategies`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/strategies/${ID}`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/configurations`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/configurations/TARGET/drafts`, method: "POST" },
    { input: `${CANONICAL_V13_API_ROOT}/configurations/TARGET/${ID}/validate`, method: "POST" },
    { input: `${CANONICAL_V13_API_ROOT}/research-bundles/preview`, method: "POST" },
    { input: `${CANONICAL_V13_API_ROOT}/research-bundles/${ID}/activate`, method: "POST" },
    { input: `${CANONICAL_V13_API_ROOT}/market-data`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/market-data/snapshots/${ID}`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/readiness/research?scope_key=scope&workflow_key=workflow`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/readiness/runtime`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/optimizations`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/research/validation-plans/${ID}`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/research/validation-plans`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/research/validation-plans/${ID}/results`, method: "GET" },
    { input: `${CANONICAL_V13_API_ROOT}/research/gates`, method: "GET" },
  ]);
});

test("incomplete readiness scope is blocked before fetch", (context) => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => { calls += 1; return new Response("{}"); };
  assert.throws(() => fetchCanonicalResearchReadiness("scope", null), /RESEARCH_SCOPE_INCOMPLETE/);
  assert.equal(calls, 0);
});

test("canonical error envelope is preserved without a retry", async (context) => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => {
    calls += 1;
    return new Response(JSON.stringify({ status: "BLOCKED", error: { code: "RESEARCH_BUNDLE_UNSET", detail: "unset" } }), {
      headers: { "Content-Type": "application/json" },
      status: 409,
    });
  };
  await assert.rejects(fetchCanonicalRuntimeReadiness(), (error) => {
    assert.ok(error instanceof CanonicalV13ApiError);
    assert.equal(error.code, "RESEARCH_BUNDLE_UNSET");
    return true;
  });
  assert.equal(calls, 1);
});

test("malformed 2xx payload is blocked at the runtime DTO boundary", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => Response.json({ status: "AVAILABLE", items: null });
  await assert.rejects(fetchCanonicalStrategies(), /INVALID_SUCCESS_DTO/);
});

test("client source contains no legacy fallback surface", async () => {
  const source = await readFile(new URL("../src/api/canonicalV13Client.ts", import.meta.url), "utf8");
  for (const forbidden of ["/api/v1", "fetchList", "fetchValue", "strategy-research", "okx-demo", "mvpApi"]) {
    assert.equal(source.includes(forbidden), false, forbidden);
  }
});
