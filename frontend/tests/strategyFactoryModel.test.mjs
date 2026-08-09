import assert from "node:assert/strict";
import test from "node:test";

import {
  canStartFormalResearch,
  deploymentHandoffText,
  validatedCandidateCount,
} from "../src/pages/strategyFactoryModel.ts";

function batch(statuses, qualifiedCount = 0) {
  return {
    persisted_count: statuses.length,
    qualified_count: qualifiedCount,
    candidates: statuses.map((status) => ({ status })),
  };
}

test("factory counts only completed validation and queues only complete qualified evidence", () => {
  assert.equal(validatedCandidateCount(batch(["QUALIFIED", "REJECTED", "VALIDATION_FAILED"])), 2);
  assert.match(deploymentHandoffText(batch(["QUALIFIED", "REJECTED"], 1)), /自动部署评审队列/);
  assert.match(deploymentHandoffText(batch(["QUALIFIED", "VALIDATION_FAILED"], 1)), /未进入/);
});

test("manual entry is enabled only for an inactive READY formal run", () => {
  assert.equal(canStartFormalResearch({ status: "READY", active: false }, false), true);
  assert.equal(canStartFormalResearch({ status: "RUNNING", active: true }, false), false);
  assert.equal(canStartFormalResearch({ status: "BLOCKED", active: false }, false), false);
  assert.equal(canStartFormalResearch({ status: "READY", active: false }, true), false);
});
