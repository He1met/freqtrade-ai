import assert from "node:assert/strict";
import test from "node:test";

import {
  generationRunDisplayTime,
  generationRunOutcome,
  generationRunTimeLabel,
} from "../src/pages/generationRunDisplay.ts";

function run(overrides = {}) {
  return {
    id: "run-1",
    status: "succeeded",
    provider: "deepseek",
    model: "deepseek-chat",
    requestedCount: 1,
    generatedCount: 1,
    acceptedCount: 1,
    failedCount: 0,
    errorMessage: null,
    ...overrides,
  };
}

test("succeeded with zero generated records is not presented as normal success", () => {
  const outcome = generationRunOutcome(
    run({ generatedCount: 0, acceptedCount: 0 }),
  );

  assert.equal(outcome.label, "完成但无产出");
  assert.equal(outcome.tone, "warning");
  assert.match(outcome.conclusion, /不能视为有效生成成功/);
});

test("generated records with zero accepted records remain a warning", () => {
  const outcome = generationRunOutcome(
    run({ generatedCount: 2, acceptedCount: 0, failedCount: 2 }),
  );

  assert.equal(outcome.label, "无可用策略");
  assert.equal(outcome.tone, "warning");
});

test("partial and complete output have distinct conclusions", () => {
  assert.equal(
    generationRunOutcome(run({ generatedCount: 2, acceptedCount: 1, failedCount: 1 })).label,
    "部分产出",
  );
  assert.deepEqual(generationRunOutcome(run()), {
    label: "生成完成",
    tone: "success",
    conclusion: "已生成并接受 1 个策略。",
  });
});

test("failed and blocked runs keep their recorded reason", () => {
  assert.deepEqual(
    generationRunOutcome(run({ status: "failed", errorMessage: "Provider timeout" })),
    {
      label: "生成失败",
      tone: "danger",
      conclusion: "Provider timeout",
    },
  );
  assert.match(
    generationRunOutcome(run({ status: "BLOCKED", errorMessage: null })).conclusion,
    /前置条件/,
  );
});

test("time display prefers completion, then start, then creation", () => {
  const complete = run({
    completedAt: "2026-07-24T03:00:00Z",
    startedAt: "2026-07-24T02:00:00Z",
    createdAt: "2026-07-24T01:00:00Z",
  });

  assert.equal(generationRunDisplayTime(complete), "2026-07-24T03:00:00Z");
  assert.equal(generationRunTimeLabel(complete), "完成时间");
  assert.equal(
    generationRunDisplayTime(run({ startedAt: null, completedAt: null, createdAt: null })),
    null,
  );
});
