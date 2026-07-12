import assert from "node:assert/strict";
import test from "node:test";

import {
  actionStatusClassName,
  createActionEvidence,
  recordActionEvidence,
} from "../src/pages/localStrategyLab/actionEvidence.ts";

test("action evidence keeps only concrete database IDs and artifact paths", () => {
  const evidence = createActionEvidence({
    action: "导入回测结果并计算评分",
    artifactPaths: ["", null, "user_data/backtest_results/run-7.json"],
    databaseIds: { backtest_task_id: 4, strategy_score_id: null },
    message: "回测结果和评分已写入数据库。",
    nextAction: "刷新 API/DB 证据进行对账。",
    recommendBug: false,
    status: "SUCCESS",
    updatedAt: "2026-07-12T00:00:00Z",
  });

  assert.deepEqual(evidence.artifactPaths, ["user_data/backtest_results/run-7.json"]);
  assert.deepEqual(evidence.databaseIds, { backtest_task_id: "4" });
});

test("blocked and failed actions stay visible without being promoted to success", () => {
  const blocked = createActionEvidence({
    action: "触发本地回测",
    message: "没有可用的核心 strategy version。",
    nextAction: "先生成并验证策略版本。",
    recommendBug: false,
    status: "BLOCKED",
    updatedAt: "2026-07-12T00:00:00Z",
  });
  const failed = createActionEvidence({
    action: "触发本地回测",
    message: "后端返回失败。",
    nextAction: "检查持久任务和错误原因。",
    recommendBug: true,
    status: "FAILED",
    updatedAt: "2026-07-12T00:01:00Z",
  });

  assert.deepEqual(recordActionEvidence([blocked], failed), [failed]);
  assert.equal(actionStatusClassName(blocked.status), "status-blocked");
  assert.equal(actionStatusClassName(failed.status), "status-failed");
});
