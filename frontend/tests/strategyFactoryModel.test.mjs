import assert from "node:assert/strict";
import test from "node:test";

import {
  candidateLifecycleDisplay,
  candidateLifecycleFor,
  canStartFormalResearch,
  deploymentHandoffText,
  hasOfficialAggressiveContract,
  hasOfficialSafetyContract,
  lifecycleSummaryText,
  researchQualityContractText,
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

const demoSafety = {
  execution_target: "OKX_DEMO",
  allow_real_funds: false,
  real_orders: false,
  credentials_collected: false,
  dry_run_trading_authorized: false,
  grant_authorized: false,
  manual_order_authorized: false,
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
  assert.match(deploymentHandoffText({ deployment_handoff_status: "CANONICAL_LINK_UNAVAILABLE" }), /衔接证据尚不可用/);
  assert.match(deploymentHandoffText({ deployment_handoff_status: "NOT_QUEUED_NO_QUALIFIED" }), /未交接/);
});

test("manual entry is enabled only for an inactive READY formal run", () => {
  const ready = { status: "READY", active: false, quality_contract: aggressiveContract, safety: demoSafety };
  assert.equal(canStartFormalResearch(ready, false), true);
  assert.equal(canStartFormalResearch({ ...ready, status: "RUNNING", active: true }, false), false);
  assert.equal(canStartFormalResearch({ ...ready, status: "BLOCKED" }, false), false);
  assert.equal(canStartFormalResearch(ready, true), false);
});

test("manual entry fails closed unless the API exposes the exact aggressive contract", () => {
  const ready = { status: "READY", active: false, quality_contract: aggressiveContract, safety: demoSafety };
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

test("quality contract copy preserves historical batch thresholds", () => {
  const legacy = {
    quality_contract: {
      max_drawdown_per_validation_window: 0.10,
      validation_requires_positive_net_profit: true,
    },
  };
  assert.match(researchQualityContractText(legacy), /历史批次契约/);
  assert.match(researchQualityContractText(legacy), /最大回撤 10%/);
  assert.match(researchQualityContractText(legacy), /不得自动部署/);
  assert.match(
    researchQualityContractText({ quality_contract: aggressiveContract }),
    /匹配当前 official contract/,
  );
});

test("manual entry rejects unsafe or incomplete execution target evidence", () => {
  const ready = { status: "READY", active: false, quality_contract: aggressiveContract, safety: demoSafety };
  assert.equal(hasOfficialSafetyContract(ready), true);
  assert.equal(canStartFormalResearch({ ...ready, safety: undefined }, false), false);
  assert.equal(canStartFormalResearch({ ...ready, safety: { ...demoSafety, execution_target: "LIVE" } }, false), false);
  assert.equal(canStartFormalResearch({ ...ready, safety: { ...demoSafety, allow_real_funds: true } }, false), false);
  assert.equal(canStartFormalResearch({ ...ready, safety: { ...demoSafety, real_orders: true } }, false), false);
});

test("candidate lifecycle display recognizes only explicit authoritative states", () => {
  assert.equal(candidateLifecycleDisplay("UNBRIDGED_REVALIDATION_REQUIRED").label, "需补充 Blueprint v2 证据");
  assert.equal(candidateLifecycleDisplay("BRIDGED_PENDING_CANONICAL_VALIDATION").label, "已桥接，待 canonical 验证");
  assert.equal(candidateLifecycleDisplay("BRIDGED_PENDING_APPROVAL").label, "已桥接，待批准");
  assert.equal(candidateLifecycleDisplay("APPROVED_NOT_DEPLOYED").label, "已批准，未部署");
  assert.equal(candidateLifecycleDisplay("DEPLOYED_ACTIVE_DEMO").label, "Demo 运行中");
  assert.equal(candidateLifecycleDisplay("made-up-state").status, "UNKNOWN");
  assert.deepEqual(candidateLifecycleDisplay(undefined).steps, ["UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN"]);
});

test("candidate lifecycle lookup fails closed when the bridge section is absent or unknown", () => {
  const lifecycle = { candidate_id: 41, lifecycle_status: "BRIDGED_PENDING_CANONICAL_VALIDATION" };
  const base = { candidate_lifecycles: [lifecycle] };
  assert.equal(candidateLifecycleFor(base, 41), null);
  assert.equal(candidateLifecycleFor({ ...base, sections: { bridge: { status: "UNKNOWN" } } }, 41), null);
  assert.equal(
    candidateLifecycleFor({ ...base, sections: { bridge: { status: "AVAILABLE" } } }, 41),
    lifecycle,
  );
  assert.equal(candidateLifecycleFor({ ...base, sections: { bridge: { status: "AVAILABLE" } } }, 99), null);
});

test("batch handoff copy comes only from the authoritative lifecycle summary", () => {
  const summary = {
    status: "APPROVED_NOT_DEPLOYED",
    qualified_count: 2,
    unbridged_count: 0,
    pending_canonical_validation_count: 0,
    pending_approval_count: 0,
    approved_not_deployed_count: 2,
    active_demo_count: 0,
    unknown_count: 0,
    reason_code: "APPROVED_NOT_DEPLOYED",
  };
  assert.match(lifecycleSummaryText(summary, true), /已批准未部署：2/);
  assert.match(lifecycleSummaryText(summary, false), /生命周期未知/);
  assert.match(lifecycleSummaryText(null, true), /生命周期未知/);
});
