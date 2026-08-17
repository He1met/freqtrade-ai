import assert from "node:assert/strict";
import test from "node:test";

import { canonicalResearchWorkflow } from "../src/pages/canonicalV13/canonicalV13Workflow.ts";

const STRATEGY_ID = "123e4567-e89b-42d3-a456-426614174000";
const VERSION_ID = "123e4567-e89b-42d3-a456-426614174001";
const PLAN_ID = "123e4567-e89b-42d3-a456-426614174002";
const TARGET_ID = "123e4567-e89b-42d3-a456-426614174003";
const DIGEST = "a".repeat(64);

const STRATEGY = {
  strategy_id: STRATEGY_ID,
  display_name: "Alpha",
  catalog_status: "DRAFT",
  intake_status: "INTAKE_ACCEPTED",
  current_version_id: VERSION_ID,
  version_number: 1,
  artifact_id: STRATEGY_ID,
  artifact_digest: DIGEST,
  validation_status: "VALIDATED",
  qualification_status: "NOT_EVALUATED",
  execution_authorized: false,
  created_at: "2026-08-14T00:00:00Z",
};

const CHAIN = {
  validation_plan_id: PLAN_ID,
  validation_plan_digest: DIGEST,
  strategy_version_id: VERSION_ID,
  research_target_id: TARGET_ID,
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

const LINKS = {
  researchHref: `/v13/research?strategy=${STRATEGY_ID}`,
  strategyHref: `/v13/strategies?strategy=${STRATEGY_ID}`,
};
const SELECTION = { planId: PLAN_ID, strategyId: STRATEGY_ID, targetId: null };

test("exact API chain projects completed current not-started and blocked stages", () => {
  const result = canonicalResearchWorkflow({
    chain: {
      ...CHAIN,
      plan_status: "COMPLETE",
      validation_attempt_id: PLAN_ID,
      attempt_status: "SUCCEEDED",
      attempt_receipt_digest: DIGEST,
      target_score_id: TARGET_ID,
      overall_score: "0.81",
      score_digest: DIGEST,
      qualification_decision_id: TARGET_ID,
      qualification_status: "REJECTED",
      qualification_reason_code: "REQUIRED_WINDOW_GATE_FAILED",
      qualification_decision_digest: DIGEST,
    },
    links: LINKS,
    selection: SELECTION,
    strategy: { ...STRATEGY, qualification_status: "REJECTED" },
  });
  assert.deepEqual(result.steps.map((step) => step.state), ["complete", "complete", "complete", "complete", "blocked"]);
  assert.deepEqual(result.steps.map((step) => step.apiStatus), ["INTAKE_ACCEPTED", "VALIDATED", "COMPLETE", "SUCCEEDED", "REJECTED"]);
  assert.equal(result.currentStepId, "qualification");
  assert.deepEqual(result.nextAction, { label: "查看资格阻断", to: LINKS.researchHref });
});

test("missing exact plan context never lights later stages from a strategy summary", () => {
  const result = canonicalResearchWorkflow({
    chain: null,
    links: LINKS,
    selection: { ...SELECTION, planId: null },
    strategy: { ...STRATEGY, qualification_status: "QUALIFIED" },
  });
  assert.deepEqual(result.steps.map((step) => step.state), ["complete", "complete", "unknown", "unknown", "unknown"]);
  assert.equal(result.steps[4].apiStatus, "QUALIFIED");
  assert.deepEqual(result.steps[2].reasonCodes, ["RESEARCH_CONTEXT_UNSELECTED"]);
  assert.equal(result.researchLink.to, LINKS.researchHref);
});

test("explicit null attempt and qualification fields remain not-started", () => {
  const result = canonicalResearchWorkflow({ chain: CHAIN, links: LINKS, selection: SELECTION, strategy: STRATEGY });
  assert.deepEqual(result.steps.map((step) => step.state), ["complete", "complete", "complete", "not-started", "not-started"]);
  assert.equal(result.steps[3].apiStatus, null);
  assert.equal(result.steps[4].apiStatus, null);
});

test("a succeeded exact attempt makes a missing qualification decision the current step", () => {
  const result = canonicalResearchWorkflow({
    chain: {
      ...CHAIN,
      plan_status: "COMPLETE",
      validation_attempt_id: PLAN_ID,
      attempt_status: "SUCCEEDED",
      attempt_receipt_digest: DIGEST,
    },
    links: LINKS,
    selection: SELECTION,
    strategy: STRATEGY,
  });
  assert.deepEqual(result.steps.map((step) => step.state), ["complete", "complete", "complete", "complete", "current"]);
  assert.equal(result.currentStepId, "qualification");
});

test("unknown strategy enum blocks its stage and never promotes downstream stages", () => {
  const result = canonicalResearchWorkflow({
    chain: { ...CHAIN, plan_status: "COMPLETE", validation_attempt_id: PLAN_ID, attempt_status: "SUCCEEDED" },
    links: LINKS,
    selection: SELECTION,
    strategy: { ...STRATEGY, validation_status: "FUTURE_VALID" },
  });
  assert.equal(result.steps[1].state, "unknown");
  assert.deepEqual(result.steps.slice(2).map((step) => step.state), ["unknown", "unknown", "unknown"]);
  assert.ok(result.steps.slice(1).every((step) => step.state !== "complete"));
});

test("mismatched strategy lineage is explicit and keeps research stages unknown", () => {
  const result = canonicalResearchWorkflow({
    chain: { ...CHAIN, strategy_version_id: TARGET_ID },
    links: LINKS,
    selection: SELECTION,
    strategy: STRATEGY,
  });
  assert.deepEqual(result.steps.slice(2).map((step) => step.state), ["unknown", "unknown", "unknown"]);
  assert.ok(result.steps.slice(2).every((step) => step.reasonCodes.includes("RESEARCH_STRATEGY_LINEAGE_MISMATCH")));
});

test("conflicting chain order fails closed instead of inferring completion", () => {
  const result = canonicalResearchWorkflow({
    chain: { ...CHAIN, plan_status: "BLOCKED", validation_attempt_id: PLAN_ID, attempt_status: "SUCCEEDED" },
    links: LINKS,
    selection: SELECTION,
    strategy: STRATEGY,
  });
  assert.deepEqual(result.steps.slice(2).map((step) => step.state), ["unknown", "unknown", "unknown"]);
  assert.ok(result.steps.slice(2).every((step) => step.reasonCodes.includes("RESEARCH_CONTEXT_CONFLICT")));
});

test("missing persisted identities for explicit attempt and qualification statuses fail closed", () => {
  const result = canonicalResearchWorkflow({
    chain: {
      ...CHAIN,
      plan_status: "COMPLETE",
      attempt_status: "SUCCEEDED",
      qualification_status: "QUALIFIED",
    },
    links: LINKS,
    selection: SELECTION,
    strategy: STRATEGY,
  });
  assert.deepEqual(result.steps.slice(2).map((step) => step.state), ["unknown", "unknown", "unknown"]);
  assert.ok(result.steps.slice(2).every((step) => step.reasonCodes.includes("RESEARCH_CONTEXT_CONFLICT")));
});

test("a response for another committed plan or target keeps research stages unknown", () => {
  const result = canonicalResearchWorkflow({
    chain: CHAIN,
    links: LINKS,
    selection: { ...SELECTION, planId: TARGET_ID },
    strategy: STRATEGY,
  });
  assert.deepEqual(result.steps.slice(2).map((step) => step.state), ["unknown", "unknown", "unknown"]);
  assert.ok(result.steps.slice(2).every((step) => step.reasonCodes.includes("RESEARCH_CONTEXT_CONFLICT")));
});
