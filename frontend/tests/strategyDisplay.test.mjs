import assert from "node:assert/strict";
import test from "node:test";

import {
  formatDiffLabel,
  formatDiffValue,
  formatSourceTrace,
  formatTraceRecord,
  isStrategyProblemStatus,
  strategyAvailability,
} from "../src/pages/strategyDisplay.ts";

function strategy(overrides = {}) {
  return {
    id: "strategy-7",
    name: "稳健趋势策略",
    status: "active",
    timeframe: "15m",
    source: "ai_generated",
    description: "测试策略",
    tags: ["trend"],
    currentVersionId: "version-8",
    currentVersion: {
      id: "version-8",
      versionNumber: 3,
      filePath: "user_data/strategies/generated/StableTrend.py",
      validationStatus: "passed",
      validationErrors: [],
      dataSource: undefined,
    },
    dataSource: {
      sourceType: "database",
      sourceDetail: "Persisted strategy row.",
      coreData: true,
      databaseIds: { strategy_id: 7 },
      artifactRefs: { strategy_file_path: "user_data/strategies/generated/StableTrend.py" },
      freshness: null,
      blockedReason: null,
    },
    ...overrides,
  };
}

test("availability prioritizes missing versions and validation failures", () => {
  assert.deepEqual(strategyAvailability(strategy({ currentVersion: null })), {
    status: "MISSING",
    reason: "尚无当前版本，不能确认策略文件是否可用。",
    isProblem: true,
  });

  const failed = strategy({
    currentVersion: {
      ...strategy().currentVersion,
      validationStatus: "failed",
      validationErrors: [{ field: "strategy", message: "策略文件校验失败。", code: "invalid" }],
    },
  });
  assert.deepEqual(strategyAvailability(failed), {
    status: "failed",
    reason: "策略文件校验失败。",
    isProblem: true,
  });
});

test("source blockers stay visible instead of being promoted to usable", () => {
  const blocked = strategy({
    dataSource: {
      ...strategy().dataSource,
      coreData: false,
      blockedReason: "策略文件不存在。",
    },
  });

  assert.deepEqual(strategyAvailability(blocked), {
    status: "BLOCKED",
    reason: "策略文件不存在。",
    isProblem: true,
  });
  assert.equal(isStrategyProblemStatus("UNAVAILABLE"), true);
  assert.equal(isStrategyProblemStatus("passed"), false);
});

test("trace, ids and diff values remain complete for expandable and copyable views", () => {
  const source = strategy().dataSource;
  assert.equal(formatTraceRecord(source.databaseIds), "strategy_id: 7");
  assert.match(formatSourceTrace(source), /来源类型（source_type）：database/);
  assert.match(formatSourceTrace(source), /strategy_file_path/);
  assert.equal(formatDiffLabel("validation_errors"), "校验错误");
  assert.equal(formatDiffValue({ after: ["a", "b"] }), '{\n  "after": [\n    "a",\n    "b"\n  ]\n}');
  assert.equal(formatDiffValue([]), "暂无");
});
