import assert from "node:assert/strict";
import test from "node:test";

import { displayDuration, filterAndSortQueueItems, projectResearchQueue, researchGenerationStatusLabel, researchQueueActionAdvice, researchQueueStatusLabel, researchQueueStatusTone, safeEvidenceHref, terminalGroups } from "../src/pages/researchQueueModel.ts";

function item(overrides = {}) {
  return { candidate_id: "candidate-1", candidate_name: "MomentumOne", pair: "BTC/USDT:USDT", timeframe: "5m", generated_at: "2026-08-11T01:00:00Z", queue_position: 1, status: "PENDING", current_step: "等待领取", completed_steps: ["候选已持久化"], next_step: "领取 lease", progress_percent: null, started_at: null, completed_at: null, elapsed_seconds: null, preceding_count: 0, attempt: 0, reason_code: "AWAITING_VALIDATION_LEASE", reason_message: "等待串行 worker 领取", evidence: [], actions: { cancel_available: false, retry_available: false, reason_code: "READ_ONLY" }, ...overrides };
}

test("deployment lifecycle is distinct while FAILED and REJECTED keep different advice", () => {
  assert.equal(researchQueueStatusLabel("QUALIFIED_PENDING_DEPLOYMENT"), "合格待部署");
  assert.equal(researchQueueStatusTone("DEPLOYED"), "success");
  assert.equal(researchQueueStatusTone("FAILED"), "danger");
  assert.match(researchQueueActionAdvice("FAILED"), /重试/);
  assert.match(researchQueueActionAdvice("REJECTED"), /不要直接重试/);
  assert.equal(researchGenerationStatusLabel("NOT_GENERATED"), "未生成");
});

test("available projection preserves one active candidate and waiting order", () => {
  const queue = { schema_version: "formal-candidate-validation-queue-read-v1", as_of: "2026-08-11T02:00:00Z", availability: "AVAILABLE", serial_execution: true, batch: { run_id: "run", expected_count: 60, generation_status: "GENERATED", generated_count: 60, enqueued_count: 60, active_count: 1, waiting_count: 2, completed_count: 57, remaining_count: 3 }, health: { status: "HEALTHY", reason_code: "LEASE_ACTIVE", lease_owner_present: true, lease_expires_at: "2026-08-11T02:01:00Z" }, active_candidate: item({ candidate_id: "active", status: "RUNNING" }), waiting_candidates: [item({ candidate_id: "third", queue_position: 3 }), item({ candidate_id: "second", queue_position: 2 })], completed_candidates: [] };
  const projection = projectResearchQueue(queue, null, null);
  assert.equal(projection.active.candidate_id, "active");
  assert.deepEqual(projection.waiting.map((row) => row.candidate_id), ["second", "third"]);
});

test("missing queue API never fabricates active, waiting, lease or progress", () => {
  const workspace = { schema_version: "formal-strategy-research-workspace-v2", as_of: "2026-08-11T02:00:00Z", source_type: "database", core_data: true, evidence_status: "COMPLETE", sections: {}, attempts: [], latest_quality_receipt: null, handoff_status: "UNKNOWN", latest_batch: { id: 1, run_id: "legacy", status: "VALIDATED", requested_count: 60, generated_count: 60, persisted_count: 60, qualified_count: 0, rejected_count: 60, failure_reason: null, report_path: "report.json", report_digest: "digest", repository_commit: "commit", completed_at: "2026-08-11T01:00:00Z", created_at: "2026-08-11T00:00:00Z", selection_policy: {}, candidates: [{ id: 7, batch_id: 1, candidate_name: "Legacy", status: "REJECTED", source_path: "candidate.py", code_digest: "digest", loadable: true, static_check: "PASSED", lookahead_status: "PASSED", score: 40, validation_passed: false, deployable_candidate: false, rejection_reasons: [{ code: "SCORE_LOW", message: "得分不足", evidence: {} }], evidence_snapshot: {}, quality_contract: {} }] } };
  const projection = projectResearchQueue(null, workspace, "404 Not Found");
  assert.equal(projection.available, false);
  assert.equal(projection.active, null);
  assert.deepEqual(projection.waiting, []);
  assert.equal(projection.completed[0].current_step, "历史批次终态（非队列投影）");
  assert.equal(projection.completed[0].progress_percent, null);
});

test("filters cover status, pair, timeframe and batch without hiding defaults", () => {
  const rows = [item({ candidate_id: "b", candidate_name: "Beta", pair: "ETH/USDT:USDT", queue_position: 2, status: "REJECTED" }), item({ candidate_id: "a", candidate_name: "Alpha", queue_position: 1, status: "VALIDATED" })];
  const all = { query: "", status: "ALL", pair: "", timeframe: "", batch: "" };
  assert.equal(filterAndSortQueueItems(rows, all, "queue", "run").length, 2);
  assert.deepEqual(filterAndSortQueueItems(rows, { ...all, pair: "ETH/USDT:USDT" }, "queue", "run").map((row) => row.candidate_id), ["b"]);
  assert.deepEqual(filterAndSortQueueItems(rows, { ...all, status: "VALIDATED", batch: "run" }, "name", "run").map((row) => row.candidate_id), ["a"]);
  assert.deepEqual(terminalGroups(rows).map((group) => [group.status, group.items.length]), [["VALIDATED", 1], ["REJECTED", 1], ["FAILED", 0], ["DEPLOYED", 0]]);
});

test("unknown duration is explicit and unsafe evidence links are rejected", () => {
  assert.equal(displayDuration(null), "数据暂不可用");
  assert.equal(displayDuration(3661), "1 小时 1 分");
  assert.equal(safeEvidenceHref("javascript:alert(1)"), null);
  assert.equal(safeEvidenceHref("/api/evidence/1"), "/api/evidence/1");
  assert.equal(safeEvidenceHref("https://example.com/report"), "https://example.com/report");
});
