import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalMetricMatrix,
  canonicalScorePercent,
  canonicalWindowEvidenceState,
} from "../src/pages/canonicalV13/canonicalV13Results.ts";

test("score visualization preserves the API value and never creates a qualification", () => {
  assert.equal(canonicalScorePercent("81.00000000"), 81);
  assert.equal(canonicalScorePercent("0"), 0);
  assert.equal(canonicalScorePercent("100"), 100);
  assert.equal(canonicalScorePercent(null), null);
  assert.equal(canonicalScorePercent("101"), null);
  assert.equal(canonicalScorePercent("not-a-score"), null);
});

test("metric matrix preserves API metrics without filling missing windows", () => {
  const windows = [
    { window_key: "oos-a", result: { metrics_json: { drawdown: -0.12, trades: 20 } } },
    { window_key: "oos-b", result: { metrics_json: { trades: 0, note: "API" } } },
    { window_key: "oos-c", result: null },
  ];
  assert.deepEqual(canonicalMetricMatrix(windows), {
    metricKeys: ["drawdown", "note", "trades"],
    rows: [
      { values: { drawdown: "-0.12", trades: "20" }, windowKey: "oos-a" },
      { values: { note: "API", trades: "0" }, windowKey: "oos-b" },
      { values: {}, windowKey: "oos-c" },
    ],
  });
});

test("window evidence states come only from explicit API result and gate evidence", () => {
  assert.equal(canonicalWindowEvidenceState({ result: null, qualification_evidence: null }), "missing-result");
  assert.equal(canonicalWindowEvidenceState({ result: {}, qualification_evidence: null }), "result-only");
  assert.equal(canonicalWindowEvidenceState({ result: {}, qualification_evidence: { hard_gate_passed: false } }), "gate-failed");
  assert.equal(canonicalWindowEvidenceState({ result: {}, qualification_evidence: { hard_gate_passed: true } }), "gate-passed");
});
