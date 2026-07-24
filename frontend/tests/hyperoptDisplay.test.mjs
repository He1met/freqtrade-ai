import assert from "node:assert/strict";
import test from "node:test";

import {
  countHyperoptStatuses,
  effectiveHyperoptStatus,
  firstUsableHyperoptBestRun,
  formatHyperoptParamsJson,
  hasUsableHyperoptBestResult,
  hyperoptParamsPreview,
} from "../src/pages/hyperoptDisplay.ts";

function run(overrides = {}) {
  return {
    id: "run-1",
    strategyName: "StableTrend",
    status: "SUCCESS",
    profileName: "local-hyperopt",
    spaces: ["buy", "sell"],
    bestParams: { buy_rsi: 28, sell_rsi: 72, nested: { enabled: true } },
    bestLoss: -1.42,
    score: 84.5,
    epoch: 18,
    artifactManifest: {
      manifestVersion: 1,
      status: "SUCCESS",
      configPath: "reports/hyperopt/config.json",
      strategyName: "StableTrend",
      resultPath: "reports/hyperopt/result.json",
      manifestPath: "reports/hyperopt/manifest.json",
      commandArgs: [],
      returnCode: 0,
      stdout: "",
      stderr: "",
      datadir: "user_data/data",
      strategyPath: "user_data/strategies",
      userdir: "user_data",
      spaces: ["buy", "sell"],
      epochs: 20,
      hyperoptLoss: "SharpeHyperOptLoss",
      blockedReason: null,
      failedReason: null,
    },
    resultPath: "reports/hyperopt/result.json",
    manifestPath: "reports/hyperopt/manifest.json",
    blockedReason: null,
    failedReason: null,
    comparison: {
      parentVersionId: "version-1",
      optimizedVersionId: "version-2",
      status: "SUCCESS",
      metrics: [],
      warnings: [],
      blockedReason: null,
      failedReason: null,
    },
    ...overrides,
  };
}

test("only successful runs with params, loss and result artifact expose a usable best result", () => {
  const success = run();
  assert.equal(effectiveHyperoptStatus(success), "SUCCESS");
  assert.equal(hasUsableHyperoptBestResult(success), true);
  assert.equal(firstUsableHyperoptBestRun([success]), success);

  assert.equal(hasUsableHyperoptBestResult(run({ bestParams: {} })), false);
  assert.equal(hasUsableHyperoptBestResult(run({ bestLoss: null })), false);
  assert.equal(hasUsableHyperoptBestResult(run({ resultPath: null, artifactManifest: null })), false);
  assert.equal(
    hasUsableHyperoptBestResult(run({ comparison: { ...run().comparison, status: "UNKNOWN" } })),
    false,
  );
});

test("FAILED and BLOCKED records never claim best even when numeric values exist", () => {
  const failed = run({ status: "FAILED", failedReason: "Hyperopt process exited with code 2." });
  const blocked = run({
    artifactManifest: {
      ...run().artifactManifest,
      status: "BLOCKED",
      blockedReason: "Local market data is missing.",
    },
  });

  assert.equal(effectiveHyperoptStatus(failed), "FAILED");
  assert.equal(effectiveHyperoptStatus(blocked), "BLOCKED");
  assert.equal(hasUsableHyperoptBestResult(failed), false);
  assert.equal(hasUsableHyperoptBestResult(blocked), false);
  assert.equal(firstUsableHyperoptBestRun([failed, blocked]), null);
  assert.deepEqual(countHyperoptStatuses([failed, blocked]), { FAILED: 1, BLOCKED: 1 });
});

test("parameter previews stay compact while full JSON remains complete", () => {
  const params = run().bestParams;
  assert.deepEqual(hyperoptParamsPreview(params, 2), [
    ["buy_rsi", "28"],
    ["sell_rsi", "72"],
  ]);
  assert.equal(
    formatHyperoptParamsJson(params),
    '{\n  "buy_rsi": 28,\n  "sell_rsi": 72,\n  "nested": {\n    "enabled": true\n  }\n}',
  );
});
