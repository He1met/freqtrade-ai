import assert from "node:assert/strict";
import test from "node:test";

import { mockMvpData } from "../src/data/mock.ts";
import { deriveLabWorkflow } from "../src/pages/localStrategyLab/workflowState.ts";

function evidenceStage(key, state, canAccept) {
  return {
    key,
    label: key,
    state,
    canAccept,
    reason: `${key}:${state}`,
    nextAction: `next:${key}`,
    observedCount: canAccept ? 1 : 0,
    coreCount: canAccept ? 1 : 0,
    records: [],
  };
}

function dataWithStages(stages, dryRun = {}, manifest = {}) {
  const data = structuredClone(mockMvpData);
  data.localStrategyLabEvidence = {
    state: "NOT_RUN",
    canAccept: false,
    reason: "test",
    nextAction: "test",
    stages,
  };
  Object.assign(data.dryRun.snapshot, dryRun);
  Object.assign(data.dryRun.manifest, manifest);
  return data;
}

const completedResearchStages = [
  evidenceStage("generation", "ACCEPTABLE", true),
  evidenceStage("strategy_file", "ACCEPTABLE", true),
  evidenceStage("backtest", "ACCEPTABLE", true),
  evidenceStage("score", "ACCEPTABLE", true),
];

test("empty API evidence keeps generation current and later phases locked", () => {
  const data = dataWithStages([]);
  const model = deriveLabWorkflow(data);

  assert.equal(model.currentPhase, "generation");
  assert.deepEqual(
    model.stages.map(({ id, progress }) => [id, progress]),
    [
      ["generation", "current"],
      ["backtest", "locked"],
      ["score", "locked"],
      ["dry-run", "locked"],
    ],
  );
  assert.equal(model.stages[0].state, "NOT_RUN");
});

test("only core acceptable generation and strategy-file evidence completes generation", () => {
  const data = dataWithStages([
    evidenceStage("generation", "ACCEPTABLE", true),
    evidenceStage("strategy_file", "ACCEPTABLE", true),
    evidenceStage("backtest", "BLOCKED", false),
    evidenceStage("score", "NOT_RUN", false),
  ]);
  const model = deriveLabWorkflow(data);

  assert.equal(model.currentPhase, "backtest");
  assert.equal(model.stages[0].progress, "completed");
  assert.equal(model.stages[1].progress, "current");
  assert.equal(model.stages[1].state, "BLOCKED");
  assert.equal(model.stages[2].progress, "locked");
});

test("FAILED API_GAP and NOT_ACCEPTABLE never render as completed", () => {
  for (const state of ["FAILED", "API_GAP", "NOT_ACCEPTABLE"]) {
    const data = dataWithStages([
      evidenceStage("generation", state, false),
      evidenceStage("strategy_file", "ACCEPTABLE", true),
    ]);
    const model = deriveLabWorkflow(data);

    assert.equal(model.currentPhase, "generation");
    assert.equal(model.stages[0].progress, "current");
    assert.equal(model.stages[0].state, state);
  }
});

test("persisted dry_run=true RUNNING evidence selects the final phase without implying live trading", () => {
  const data = dataWithStages(
    completedResearchStages,
    {
      artifactManifestPath: "/tmp/dry-run-manifest.json",
      dryRun: true,
      status: "RUNNING",
      strategyVersionId: 123,
    },
    {
      manifestPath: "/tmp/dry-run-manifest.json",
      status: "SUCCESS",
      strategyVersionId: 123,
    },
  );
  const model = deriveLabWorkflow(data, { dryRunSource: "api" });

  assert.equal(model.currentPhase, "dry-run");
  assert.deepEqual(model.stages.slice(0, 3).map((stage) => stage.progress), [
    "completed",
    "completed",
    "completed",
  ]);
  assert.equal(model.stages[3].progress, "current");
  assert.match(model.stages[3].nextAction, /禁止 live trading/);
});

test("fixture or failed Dry-run source never completes the final phase", () => {
  const data = dataWithStages(
    completedResearchStages,
    {
      artifactManifestPath: "/tmp/dry-run-manifest.json",
      dryRun: true,
      status: "RUNNING",
      strategyVersionId: 123,
    },
    {
      manifestPath: "/tmp/dry-run-manifest.json",
      status: "SUCCESS",
      strategyVersionId: 123,
    },
  );

  const fixture = deriveLabWorkflow(data, { dryRunSource: "fixture" });
  assert.equal(fixture.stages[3].state, "NOT_ACCEPTABLE");
  assert.equal(fixture.stages[3].progress, "current");

  const failed = deriveLabWorkflow(data, { dryRunSource: "failed" });
  assert.equal(failed.stages[3].state, "API_GAP");
  assert.equal(failed.stages[3].progress, "current");
});

test("RUNNING Dry-run with missing or mismatched persistent IDs fails closed", () => {
  const missingIdentity = dataWithStages(completedResearchStages, {
    artifactManifestPath: null,
    dryRun: true,
    status: "RUNNING",
    strategyVersionId: null,
  });
  const missing = deriveLabWorkflow(missingIdentity, { dryRunSource: "api" });
  assert.equal(missing.stages[3].state, "API_GAP");

  const mismatchedIdentity = dataWithStages(
    completedResearchStages,
    {
      artifactManifestPath: "/tmp/snapshot.json",
      dryRun: true,
      status: "RUNNING",
      strategyVersionId: 123,
    },
    {
      manifestPath: "/tmp/manifest.json",
      status: "SUCCESS",
      strategyVersionId: 456,
    },
  );
  const mismatched = deriveLabWorkflow(mismatchedIdentity, { dryRunSource: "api" });
  assert.equal(mismatched.stages[3].state, "API_GAP");

  const failedManifest = dataWithStages(
    completedResearchStages,
    {
      artifactManifestPath: "/tmp/dry-run-manifest.json",
      dryRun: true,
      status: "RUNNING",
      strategyVersionId: 123,
    },
    {
      manifestPath: "/tmp/dry-run-manifest.json",
      status: "FAILED",
      strategyVersionId: 123,
    },
  );
  const failed = deriveLabWorkflow(failedManifest, { dryRunSource: "api" });
  assert.equal(failed.stages[3].state, "API_GAP");
});

test("loading and API failure override cached-looking data fail-closed", () => {
  const data = dataWithStages([
    evidenceStage("generation", "ACCEPTABLE", true),
    evidenceStage("strategy_file", "ACCEPTABLE", true),
  ]);

  assert.equal(deriveLabWorkflow(data, { isLoading: true }).currentPhase, "generation");
  const failed = deriveLabWorkflow(data, { error: "Backend API unavailable" });
  assert.equal(failed.currentPhase, "generation");
  assert.equal(failed.stages[0].state, "API_GAP");
  assert.equal(failed.stages[0].reason, "Backend API unavailable");
});
