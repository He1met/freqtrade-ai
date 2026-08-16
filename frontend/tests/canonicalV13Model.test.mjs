import assert from "node:assert/strict";
import test from "node:test";

import {
  CANONICAL_URL_KEYS,
  canonicalHomeDecision,
  canonicalStatusPresentation,
  parseCanonicalUrlState,
  serializeCanonicalUrlState,
  withCanonicalUrlValue,
} from "../src/pages/canonicalV13/canonicalV13Model.ts";

const ID = "123e4567-e89b-42d3-a456-426614174000";

const HOME_EVIDENCE = {
  configurations: { status: "AVAILABLE", configured_kinds: [], unset_kinds: [], items: [] },
  market: {
    status: "AVAILABLE",
    profile_count: 1,
    validated_profile_count: 1,
    artifact_count: 1,
    accepted_receipt_count: 1,
    snapshots: [],
  },
  optimization: { status: "PENDING_FIRST_BACKTEST", items: [] },
  research: {
    status: "READY",
    reason_codes: [],
    scope_key: "production",
    workflow_key: "research",
    configuration_bundle_id: ID,
    bundle_digest: "a".repeat(64),
    market_snapshot_id: ID,
    target_count: 1,
    total_candidate_count: 1,
    deployment_id: null,
    runtime_instance_id: null,
  },
  runtime: {
    status: "BLOCKED",
    reason_codes: ["TRADING_DISABLED"],
    scope_key: null,
    workflow_key: null,
    configuration_bundle_id: null,
    bundle_digest: null,
    market_snapshot_id: null,
    target_count: null,
    total_candidate_count: null,
    deployment_id: null,
    runtime_instance_id: null,
  },
  strategies: { status: "AVAILABLE", items: [] },
};

test("canonical URL key matrix is exact and stable", () => {
  assert.deepEqual(CANONICAL_URL_KEYS, {
    submission: [],
    strategies: ["strategy"],
    configuration: ["scope", "workflow", "profile", "version"],
    "market-data": ["profile", "snapshot", "target"],
    research: ["scope", "workflow", "target", "strategy", "plan"],
    optimization: ["strategy", "target"],
  });
  assert.equal(
    serializeCanonicalUrlState("configuration", { version: ID, profile: ID, workflow: "research", scope: "prod" }),
    `scope=prod&workflow=research&profile=${ID}&version=${ID}`,
  );
});

test("scope and workflow are a required pair without a hidden workflow default", () => {
  assert.deepEqual(parseCanonicalUrlState("configuration", "?scope=prod").problems, ["INCOMPLETE_SCOPE_WORKFLOW"]);
  assert.deepEqual(parseCanonicalUrlState("research", "?workflow=research").problems, ["INCOMPLETE_SCOPE_WORKFLOW"]);
  assert.equal(parseCanonicalUrlState("research", "?scope=prod&workflow=research").valid, true);
  assert.equal(parseCanonicalUrlState("research", "").valid, true);
});

test("unknown duplicate invalid and non-UUID identity values fail closed", () => {
  assert.deepEqual(parseCanonicalUrlState("strategies", "?legacy=1").problems, ["UNKNOWN_URL_KEY:legacy"]);
  assert.deepEqual(parseCanonicalUrlState("strategies", `?strategy=${ID}&strategy=${ID}`).problems, ["DUPLICATE_URL_KEY:strategy"]);
  assert.deepEqual(parseCanonicalUrlState("strategies", "?strategy=not-a-uuid").problems, ["INVALID_URL_VALUE:strategy"]);
  assert.deepEqual(parseCanonicalUrlState("market-data", "?profile=not-a-uuid").problems, ["INVALID_URL_VALUE:profile"]);
  assert.equal(parseCanonicalUrlState("market-data", "?target=BTC%2FUSDT%3AUSDT").valid, true);
});

test("selection updates never insert an unrequested first-item default", () => {
  assert.equal(withCanonicalUrlValue("strategies", {}, "strategy", ID), `strategy=${ID}`);
  assert.equal(withCanonicalUrlValue("strategies", { strategy: ID }, "strategy", null), "");
  assert.throws(() => withCanonicalUrlValue("strategies", {}, "profile", ID), /UNKNOWN_URL_KEY/);
});

test("unknown API enum is an explicit contract failure, never a success tone", () => {
  assert.deepEqual(canonicalStatusPresentation("BLOCKED"), {
    known: true,
    label: "已阻塞",
    raw: "BLOCKED",
    tone: "warning",
  });
  assert.deepEqual(canonicalStatusPresentation("FUTURE_GREEN"), {
    known: false,
    label: "未知合同状态",
    raw: "FUTURE_GREEN",
    tone: "danger",
  });
});

test("persisted gate PASSED is a known success contract", () => {
  assert.deepEqual(canonicalStatusPresentation("PASSED"), {
    known: true,
    label: "已通过",
    raw: "PASSED",
    tone: "success",
  });
});

test("home follows the explicit canonical journey without promoting later readiness", () => {
  assert.deepEqual(canonicalHomeDecision({
    ...HOME_EVIDENCE,
    strategies: { status: "EMPTY", items: [] },
  }), {
    kind: "blocked",
    title: "尚无 canonical 策略",
    summary: "策略目录由 API 明确返回 EMPTY；这不代表加载失败，也不会从 Legacy 补齐。",
    rawStatus: "EMPTY",
    reasonCodes: [],
    nextAction: { label: "提交第一个策略", to: "/v13/submission" },
  });

  assert.deepEqual(canonicalHomeDecision({
    ...HOME_EVIDENCE,
    research: { ...HOME_EVIDENCE.research, status: "BLOCKED", reason_codes: ["RESEARCH_BUNDLE_UNSET"] },
  }), {
    kind: "blocked",
    title: "研究流程被阻断",
    summary: "研究 readiness 由 Canonical API 明确返回 BLOCKED。",
    rawStatus: "BLOCKED",
    reasonCodes: ["RESEARCH_BUNDLE_UNSET"],
    nextAction: { label: "查看研究阻断", to: "/v13/research" },
  });
});

test("home fails closed when any required canonical projection is unavailable", () => {
  const decision = canonicalHomeDecision({ ...HOME_EVIDENCE, market: null });
  assert.equal(decision.kind, "unknown");
  assert.equal(decision.title, "项目状态未知");
  assert.equal(decision.rawStatus, "CANONICAL_API_UNAVAILABLE");
  assert.doesNotMatch(decision.title, /就绪|成功/);
});
