import assert from "node:assert/strict";
import test from "node:test";

import {
  CANONICAL_URL_KEYS,
  canonicalStatusPresentation,
  parseCanonicalUrlState,
  serializeCanonicalUrlState,
  withCanonicalUrlValue,
} from "../src/pages/canonicalV13/canonicalV13Model.ts";

const ID = "123e4567-e89b-42d3-a456-426614174000";

test("canonical URL key matrix is exact and stable", () => {
  assert.deepEqual(CANONICAL_URL_KEYS, {
    submission: [],
    strategies: ["strategy"],
    configuration: ["scope", "workflow", "profile", "version"],
    "market-data": ["profile", "snapshot", "target"],
    research: ["scope", "workflow", "target", "strategy"],
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
