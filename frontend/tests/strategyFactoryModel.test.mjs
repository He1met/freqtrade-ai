import assert from "node:assert/strict";
import test from "node:test";

import {
  canStartFormalResearch,
  deploymentHandoffText,
  hasOfficialAggressiveContract,
  validatedCandidateCount,
} from "../src/pages/strategyFactoryModel.ts";

const aggressiveContract = {
  contract_version: "formal-strategy-research-aggressive-v1",
  risk_profile: "AGGRESSIVE",
  profile_label: "进攻型：最大回撤 15%",
  max_drawdown_per_validation_window: 0.15,
  validation_requires_positive_net_profit: true,
  lookahead_analysis_required: true,
  fee_per_side: 0.0005,
  slippage_per_side: 0.0002,
};

function batch(statuses, qualifiedCount = 0) {
  return {
    persisted_count: statuses.length,
    qualified_count: qualifiedCount,
    candidates: statuses.map((status) => ({ status })),
  };
}

test("factory counts only completed validation and never infers handoff from candidate counts", () => {
  assert.equal(validatedCandidateCount(batch(["QUALIFIED", "REJECTED", "VALIDATION_FAILED"])), 2);
  assert.match(deploymentHandoffText(null), /未知/);
  assert.match(deploymentHandoffText({ deployment_handoff_status: "NOT_EVALUATED" }), /未知/);
  assert.match(deploymentHandoffText({ deployment_handoff_status: "QUEUED_FOR_EXISTING_AUTOMATION" }), /已交接/);
  assert.match(deploymentHandoffText({ deployment_handoff_status: "NOT_QUEUED_NO_QUALIFIED" }), /未交接/);
});

test("manual entry is enabled only for an inactive READY formal run", () => {
  const ready = { status: "READY", active: false, quality_contract: aggressiveContract };
  assert.equal(canStartFormalResearch(ready, false), true);
  assert.equal(canStartFormalResearch({ ...ready, status: "RUNNING", active: true }, false), false);
  assert.equal(canStartFormalResearch({ ...ready, status: "BLOCKED" }, false), false);
  assert.equal(canStartFormalResearch(ready, true), false);
});

test("manual entry fails closed unless the API exposes the exact aggressive contract", () => {
  const ready = { status: "READY", active: false, quality_contract: aggressiveContract };
  assert.equal(hasOfficialAggressiveContract(ready), true);
  assert.equal(canStartFormalResearch({ ...ready, quality_contract: undefined }, false), false);
  assert.equal(canStartFormalResearch({
    ...ready,
    quality_contract: { ...aggressiveContract, max_drawdown_per_validation_window: 0.10 },
  }, false), false);
  assert.equal(canStartFormalResearch({
    ...ready,
    quality_contract: { ...aggressiveContract, max_drawdown_per_validation_window: 0.16 },
  }, false), false);
});
