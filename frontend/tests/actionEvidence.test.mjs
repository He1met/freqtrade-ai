import assert from "node:assert/strict";
import test from "node:test";

import {
  actionStatusClassName,
  createActionEvidence,
  latestActionEnvironmentScope,
  parseStoredActionEvidence,
  recordActionEvidence,
  resolveLatestActionFeedback,
} from "../src/pages/localStrategyLab/actionEvidence.ts";

function evidence(overrides = {}) {
  const eventId = overrides.eventId ?? "event-1";
  const lifecycleId = overrides.lifecycleId ?? eventId;
  return createActionEvidence({
    action: "触发本地回测",
    eventId,
    lifecycleId,
    message: "已处理。",
    nextAction: "刷新 API/DB 证据进行对账。",
    recommendBug: false,
    status: "SUCCESS",
    updatedAt: "2026-07-12T00:00:00Z",
    databaseIds: { backtest_run_id: 7 },
    ...overrides,
  });
}

test("v2 action evidence records phase event and only concrete associated entity IDs", () => {
  const entry = evidence({
    artifactPaths: ["", null, "user_data/backtest_results/run-7.json"],
    databaseIds: { backtest_run_id: 7, strategy_score_id: null },
  });

  assert.equal(entry.schemaVersion, 2);
  assert.equal(entry.eventId, "event-1");
  assert.equal(entry.environmentScope, "current");
  assert.equal(entry.phase, "backtest");
  assert.deepEqual(entry.artifactPaths, ["user_data/backtest_results/run-7.json"]);
  assert.deepEqual(entry.entityIds, { backtest_run_id: "7" });
  assert.deepEqual(entry.databaseIds, entry.entityIds);
});

test("business SUCCESS without an associated persistent ID fails closed as API_GAP", () => {
  const entry = evidence({
    action: "导入回测结果并计算评分",
    databaseIds: {},
    status: "SUCCESS",
  });

  assert.equal(entry.phase, "score");
  assert.equal(entry.status, "API_GAP");
});

test("terminal feedback replaces its matching RUNNING event while preserving correlation", () => {
  const running = evidence({ eventId: "running-event", lifecycleId: "shared-lifecycle", status: "RUNNING" });
  const complete = evidence({ eventId: "terminal-event", lifecycleId: "shared-lifecycle", status: "SUCCESS", updatedAt: "2026-07-12T00:01:00Z" });

  const history = recordActionEvidence([running], complete);
  assert.equal(history.length, 1);
  assert.equal(history[0].eventId, "running-event");
  assert.equal(history[0].status, "SUCCESS");
});

test("repeated refresh feedback folds without deleting failures or blockers", () => {
  const failed = evidence({
    action: "生成策略",
    databaseIds: {},
    eventId: "failed",
    status: "FAILED",
  });
  const refreshOne = evidence({
    action: "刷新数据",
    databaseIds: {},
    eventId: "refresh-1",
    status: "SUCCESS",
    updatedAt: "2026-07-12T00:01:00Z",
  });
  const refreshTwo = evidence({
    action: "刷新数据",
    databaseIds: {},
    eventId: "refresh-2",
    status: "SUCCESS",
    updatedAt: "2026-07-12T00:02:00Z",
  });

  const history = recordActionEvidence(
    recordActionEvidence([failed], refreshOne),
    refreshTwo,
  );
  assert.equal(history.length, 2);
  assert.equal(history[0].eventId, "refresh-2");
  assert.equal(history[0].repeatCount, 2);
  assert.equal(history[1].status, "FAILED");
});

test("only equivalent refresh feedback folds; changed failures and artifacts stay separate", () => {
  const firstFailure = evidence({
    action: "触发本地回测",
    artifactPaths: ["results/first.json"],
    eventId: "failure-1",
    lifecycleId: "failure-cycle-1",
    message: "首次失败原因",
    status: "FAILED",
  });
  const secondFailure = evidence({
    action: "触发本地回测",
    artifactPaths: ["results/second.json"],
    eventId: "failure-2",
    lifecycleId: "failure-cycle-2",
    message: "另一个失败原因",
    status: "FAILED",
  });
  const failures = recordActionEvidence([firstFailure], secondFailure);
  assert.equal(failures.length, 2);

  const changedRefresh = evidence({
    action: "刷新数据",
    artifactPaths: ["snapshot/new.json"],
    databaseIds: {},
    eventId: "refresh-changed",
    lifecycleId: "refresh-cycle-changed",
    message: "刷新结果已变化",
    status: "SUCCESS",
  });
  const refreshes = recordActionEvidence(
    [evidence({
      action: "刷新数据",
      artifactPaths: ["snapshot/old.json"],
      databaseIds: {},
      eventId: "refresh-old",
      lifecycleId: "refresh-cycle-old",
      message: "刷新完成",
      status: "SUCCESS",
    })],
    changedRefresh,
  );
  assert.equal(refreshes.length, 2);
});

test("repeated RUNNING to terminal refresh cycles fold into one timeline entry", () => {
  const refresh = (eventId, lifecycleId, status, updatedAt) => evidence({
    action: "刷新数据",
    databaseIds: {},
    eventId,
    lifecycleId,
    status,
    updatedAt,
  });
  let history = [];
  history = recordActionEvidence(history, refresh("refresh-1", "cycle-1", "RUNNING", "2026-07-12T00:00:00Z"));
  history = recordActionEvidence(history, refresh("complete-1", "cycle-1", "SUCCESS", "2026-07-12T00:00:01Z"));
  history = recordActionEvidence(history, refresh("refresh-2", "cycle-2", "RUNNING", "2026-07-12T00:01:00Z"));
  history = recordActionEvidence(history, refresh("complete-2", "cycle-2", "SUCCESS", "2026-07-12T00:01:01Z"));

  assert.equal(history.length, 1);
  assert.equal(history[0].eventId, "refresh-2");
  assert.equal(history[0].repeatCount, 2);
});

test("generation backtest and ingest terminal events enrich IDs on their shared lifecycle", () => {
  const cases = [
    {
      action: "生成策略",
      phase: "generation",
      runningIds: {},
      terminalIds: { strategy_generation_run_id: "101" },
    },
    {
      action: "触发本地回测",
      phase: "backtest",
      runningIds: { strategy_version_id: "201" },
      terminalIds: { strategy_version_id: "201", backtest_run_id: "202" },
    },
    {
      action: "导入回测结果并计算评分",
      phase: "score",
      runningIds: { backtest_task_id: "301" },
      terminalIds: {
        backtest_task_id: "301",
        backtest_result_id: "302",
        strategy_score_id: "303",
      },
    },
  ];

  for (const [index, item] of cases.entries()) {
    const lifecycleId = `lifecycle-${index}`;
    const running = evidence({
      action: item.action,
      databaseIds: item.runningIds,
      eventId: `running-${index}`,
      lifecycleId,
      phase: item.phase,
      status: "RUNNING",
    });
    const terminal = evidence({
      action: item.action,
      databaseIds: item.terminalIds,
      eventId: `terminal-${index}`,
      lifecycleId,
      phase: item.phase,
      status: "SUCCESS",
      updatedAt: "2026-07-12T00:01:00Z",
    });
    const history = recordActionEvidence([running], terminal);

    assert.equal(history.length, 1);
    assert.equal(history[0].eventId, `running-${index}`);
    assert.equal(history[0].lifecycleId, lifecycleId);
    assert.deepEqual(history[0].entityIds, item.terminalIds);
  }
});

test("BLOCKED FAILED UNAUTHORIZED and API_GAP retain distinct timeline semantics", () => {
  const statuses = ["BLOCKED", "FAILED", "UNAUTHORIZED", "API_GAP"];
  const history = statuses.reduce(
    (current, status, index) => recordActionEvidence(current, evidence({
      action: `事件 ${status}`,
      databaseIds: {},
      eventId: `event-${index}`,
      status,
      updatedAt: `2026-07-12T00:0${index}:00Z`,
    })),
    [],
  );

  assert.deepEqual(history.map((entry) => entry.status), [...statuses].reverse());
  assert.equal(actionStatusClassName("BLOCKED"), "status-blocked");
  assert.equal(actionStatusClassName("FAILED"), "status-failed");
  assert.equal(actionStatusClassName("UNAUTHORIZED"), "status-blocked");
  assert.equal(actionStatusClassName("API_GAP"), "status-blocked");
});

test("v1 browser history migrates to v2 without becoming API/DB business state", () => {
  const legacy = JSON.stringify([{
    action: "检查 Dry-run readiness",
    artifactPaths: ["/tmp/readiness.json"],
    databaseIds: { strategy_version_id: "42" },
    message: "readiness report 已返回。",
    nextAction: "核对持久证据。",
    recommendBug: false,
    status: "SUCCESS",
    updatedAt: "2026-07-12T00:00:00Z",
  }]);

  const parsed = parseStoredActionEvidence(null, legacy);
  assert.equal(parsed.state, "migrated-v1");
  assert.equal(parsed.history[0].schemaVersion, 2);
  assert.equal(parsed.history[0].phase, "dry-run");
  assert.equal(parsed.history[0].environmentScope, "unknown");
  assert.deepEqual(parsed.history[0].entityIds, { strategy_version_id: "42" });
  assert.match(parsed.history[0].eventId, /^legacy-/);
});

test("refresh semantics distinguish empty, invalid and restored browser history", () => {
  assert.deepEqual(parseStoredActionEvidence(null, null), {
    history: [],
    state: "empty",
  });
  assert.deepEqual(parseStoredActionEvidence("{broken", null), {
    history: [],
    state: "invalid",
  });

  const stored = evidence({ eventId: "persisted-event" });
  const restored = parseStoredActionEvidence(JSON.stringify([stored]), null);
  assert.equal(restored.state, "restored-v2");
  assert.equal(restored.history[0].eventId, "persisted-event");
});

test("edited v2 storage cannot restore business SUCCESS without entity IDs", () => {
  const stored = evidence({ eventId: "edited-event" });
  stored.entityIds = {};
  stored.databaseIds = {};

  const restored = parseStoredActionEvidence(JSON.stringify([stored]), null);
  assert.equal(restored.state, "restored-v2");
  assert.equal(restored.history[0].status, "API_GAP");
});

test("creation and restore redact HTML and secrets and enforce field limits", () => {
  const entry = createActionEvidence({
    action: "<b>生成策略</b>",
    artifactPaths: Array.from(
      { length: 12 },
      (_, index) => `/tmp/${"x".repeat(600)}-${index}?token=visible-secret`,
    ),
    databaseIds: {
      strategy_generation_run_id: "9".repeat(200),
      unexpected_token: "must-drop",
    },
    eventId: "event-safe",
    lifecycleId: "lifecycle-safe",
    message: "<script>alert(1)</script> Authorization: Bearer top-secret token=plain-secret",
    nextAction: `检查 api_key=plain-secret ${"n".repeat(800)}`,
    recommendBug: true,
    status: "FAILED",
    updatedAt: "2026-07-12T00:00:00Z",
  });

  assert.equal(entry.action, "生成策略");
  assert.doesNotMatch(JSON.stringify(entry), /top-secret|plain-secret|<script>|must-drop/);
  assert.equal(entry.artifactPaths.length, 8);
  assert.ok(entry.artifactPaths.every((path) => path.length <= 512));
  assert.equal(entry.entityIds.strategy_generation_run_id.length, 128);
  assert.equal(entry.message.length <= 1_000, true);
  assert.equal(entry.nextAction.length <= 600, true);

  const edited = {
    ...entry,
    message: "<img src=x onerror=alert(1)> token=restored-secret",
    extraField: "Bearer restored-bearer",
  };
  const restored = parseStoredActionEvidence(JSON.stringify([edited]), null);
  assert.doesNotMatch(JSON.stringify(restored.history), /restored-secret|restored-bearer|<img/);
});

test("restored and recorded history never exceeds 24 entries", () => {
  const many = Array.from({ length: 40 }, (_, index) => evidence({
    action: `刷新数据 ${index}`,
    eventId: `event-${index}`,
    lifecycleId: `lifecycle-${index}`,
    status: "FAILED",
    updatedAt: `2026-07-12T00:${String(index).padStart(2, "0")}:00Z`,
  }));
  const restored = parseStoredActionEvidence(JSON.stringify(many), null);
  assert.equal(restored.history.length, 24);

  const recorded = many.reduce(
    (history, entry) => recordActionEvidence(history, entry, 100),
    [],
  );
  assert.equal(recorded.length, 24);
});

test("latest feedback rejects candidate mismatches and historical environments", () => {
  const history = [evidence({
    databaseIds: { strategy_version_id: "201", backtest_run_id: "501" },
    status: "SUCCESS",
  })];
  assert.equal(resolveLatestActionFeedback({
    environmentScope: "current",
    expectedEntityIds: { strategy_version_id: "999" },
    history,
    phase: "backtest",
  }).applicability, "mismatch");
  assert.equal(resolveLatestActionFeedback({
    environmentScope: "historical",
    expectedEntityIds: { strategy_version_id: "201" },
    history,
    phase: "backtest",
  }).applicability, "historical");
  assert.equal(resolveLatestActionFeedback({
    environmentScope: "current",
    expectedEntityIds: { strategy_version_id: "201" },
    history: [{ ...history[0], environmentScope: "historical" }],
    phase: "backtest",
  }).applicability, "historical");
  assert.equal(resolveLatestActionFeedback({
    environmentScope: "current",
    expectedEntityIds: { strategy_version_id: undefined },
    history,
    phase: "backtest",
  }).applicability, "unknown");
});

test("missing candidate context demotes old success while stop failures retain their status", () => {
  const oldSuccess = evidence({
    action: "检查 Dry-run readiness",
    databaseIds: { strategy_version_id: "201" },
    phase: "dry-run",
    status: "SUCCESS",
  });
  for (const environmentScope of ["historical", "unknown"]) {
    const result = resolveLatestActionFeedback({
      actions: ["检查 Dry-run readiness"],
      environmentScope,
      expectedEntityIds: { strategy_version_id: undefined },
      history: [oldSuccess],
      phase: "dry-run",
    });
    assert.notEqual(result.applicability, "current");
  }

  for (const status of ["API_GAP", "FAILED", "BLOCKED"]) {
    const stop = evidence({
      action: "停止 controlled dry-run",
      databaseIds: {},
      phase: "dry-run",
      status,
    });
    const result = resolveLatestActionFeedback({
      actions: ["停止 controlled dry-run"],
      environmentScope: latestActionEnvironmentScope({
        actions: ["停止 controlled dry-run"],
        history: [stop],
        phase: "dry-run",
      }),
      history: [stop],
      phase: "dry-run",
    });
    assert.equal(result.applicability, "current");
    assert.equal(result.entry.status, status);
  }

  const historicalStop = evidence({
    action: "停止 controlled dry-run",
    databaseIds: {},
    environmentScope: "historical",
    phase: "dry-run",
    status: "BLOCKED",
  });
  const historicalScope = latestActionEnvironmentScope({
    actions: ["停止 controlled dry-run"],
    history: [historicalStop],
    phase: "dry-run",
  });
  assert.equal(historicalScope, "historical");
  assert.equal(resolveLatestActionFeedback({
    actions: ["停止 controlled dry-run"],
    environmentScope: historicalScope,
    history: [historicalStop],
    phase: "dry-run",
  }).applicability, "historical");
});
