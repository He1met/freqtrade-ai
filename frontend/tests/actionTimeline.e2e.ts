import { expect, test } from "@playwright/test";

import { captureBrowserProblems, expectNoPageOverflow } from "./helpers/desktopGate";

test("shows contextual auxiliary feedback and restores it without claiming backend state", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "Issue #432 desktop acceptance uses 1280x720.");
  const browserProblems = captureBrowserProblems(page);

  await page.goto("/local-strategy-lab");

  const timeline = page.getByRole("region", { name: "本浏览器操作反馈" });
  await expect(timeline).toBeVisible();
  await expect(timeline).toContainText("本浏览器辅助历史为空或已丢失");
  await expect(page.getByRole("region", { name: "核心操作反馈记录" })).toHaveCount(0);

  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(timeline).toContainText("API/DB");

  await page.reload();
  await expect(timeline).toContainText("已恢复本浏览器辅助历史");
  await timeline.getByText(/操作历史/).click();
  await expect(timeline.getByText("刷新数据", { exact: true }).first()).toBeVisible();
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("shows backtest score and dry-run feedback next to each operation area", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "Issue #432 desktop acceptance uses 1280x720.");
  const source = (key: string, id: number) => ({
    source_type: "database",
    source_detail: "isolated E2E database record",
    core_data: true,
    database_ids: { [key]: id },
    artifact_refs: {},
    environment: {
      scope: "current",
      runnable: true,
      migration_verified: false,
      reason: "isolated current E2E environment",
    },
  });
  const entries = [
    {
      schemaVersion: 2,
      eventId: "stop-event",
      lifecycleId: "stop-lifecycle",
      environmentScope: "current",
      phase: "dry-run",
      action: "停止 controlled dry-run",
      artifactPaths: [],
      entityIds: {},
      databaseIds: {},
      message: "停止接口未返回持久 ID。",
      nextAction: "核对 backend control response。",
      recommendBug: true,
      repeatCount: 1,
      status: "API_GAP",
      updatedAt: "2026-07-12T00:04:00Z",
    },
    {
      schemaVersion: 2,
      eventId: "backtest-event",
      lifecycleId: "backtest-lifecycle",
      environmentScope: "current",
      phase: "backtest",
      action: "触发本地回测",
      artifactPaths: [],
      entityIds: { strategy_version_id: "999", backtest_run_id: "501" },
      databaseIds: { strategy_version_id: "999", backtest_run_id: "501" },
      message: "回测已创建。",
      nextAction: "核对 backtest run。",
      recommendBug: false,
      repeatCount: 1,
      status: "SUCCESS",
      updatedAt: "2026-07-12T00:03:00Z",
    },
    {
      schemaVersion: 2,
      eventId: "score-event",
      lifecycleId: "score-lifecycle",
      environmentScope: "current",
      phase: "score",
      action: "导入回测结果并计算评分",
      artifactPaths: ["/tmp/result.json"],
      entityIds: { backtest_task_id: "601", backtest_result_id: "401", strategy_score_id: "701" },
      databaseIds: { backtest_task_id: "601", backtest_result_id: "401", strategy_score_id: "701" },
      message: "评分已持久化。",
      nextAction: "核对 StrategyScore。",
      recommendBug: false,
      repeatCount: 1,
      status: "SUCCESS",
      updatedAt: "2026-07-12T00:02:00Z",
    },
    {
      schemaVersion: 2,
      eventId: "dry-event",
      lifecycleId: "dry-lifecycle",
      environmentScope: "current",
      phase: "dry-run",
      action: "检查 Dry-run readiness",
      artifactPaths: [],
      entityIds: { strategy_version_id: "201" },
      databaseIds: { strategy_version_id: "201" },
      message: "readiness 已返回。",
      nextAction: "只进行受控 Dry-run。",
      recommendBug: false,
      repeatCount: 1,
      status: "SUCCESS",
      updatedAt: "2026-07-12T00:01:00Z",
    },
  ];
  await page.addInitScript((history) => {
    window.localStorage.setItem(
      "freqtrade-ai.local-strategy-lab.action-evidence.v2",
      JSON.stringify(history),
    );
  }, entries);

  await page.route("**/api/strategy-generation-runs", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 101,
      status: "succeeded",
      provider: "deepseek",
      model: "deepseek-chat",
      requested_count: 1,
      generated_count: 1,
      accepted_count: 1,
      failed_count: 0,
      data_source: source("strategy_generation_run_id", 101),
    }]),
  }));
  await page.route("**/api/strategy-versions", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 201,
      strategy_id: 301,
      generation_run_id: 101,
      version_number: 1,
      file_path: "user_data/strategies/generated/E2E.py",
      validation_status: "valid",
      file_state: {
        status: "SUCCESS",
        path: "user_data/strategies/generated/E2E.py",
        exists: true,
        is_file: true,
      },
      data_source: source("strategy_version_id", 201),
    }]),
  }));
  await page.route("**/api/strategies", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 301,
      name: "E2E",
      slug: "e2e",
      status: "validated",
      source: "deepseek",
      current_version_id: 201,
      data_source: source("strategy_id", 301),
    }]),
  }));
  await page.route("**/api/backtest-results", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 401,
      backtest_run_id: 501,
      backtest_task_id: 601,
      result_path: "/tmp/result.json",
      data_source: source("backtest_result_id", 401),
    }]),
  }));
  await page.route("**/api/ranking", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      rank: 1,
      score_id: 701,
      strategy_id: 301,
      strategy_version_id: 201,
      backtest_result_id: 401,
      strategy_name: "E2E",
      version_number: 1,
      file_path: "user_data/strategies/generated/E2E.py",
      total_score: 80,
      data_source: source("strategy_score_id", 701),
    }]),
  }));

  await page.goto("/local-strategy-lab");
  const workflow = page.getByTestId("lab-workflow");
  await expect(workflow).toHaveAttribute("data-current-stage", "dry-run", { timeout: 20_000 });

  await workflow.getByRole("button", { name: /回测验证/ }).click();
  const backtestFeedback = page.getByRole("region", { name: "回测验证最近操作反馈" });
  await expect(backtestFeedback).toContainText("不适用于当前对象");
  await expect(backtestFeedback).not.toContainText("SUCCESS");

  await page.addInitScript(() => {
    const key = "freqtrade-ai.local-strategy-lab.action-evidence.v2";
    const history = JSON.parse(window.localStorage.getItem(key) ?? "[]");
    const backtest = history.find((entry: { action?: string }) => entry.action === "触发本地回测");
    if (backtest) {
      backtest.environmentScope = "historical";
      window.localStorage.setItem(key, JSON.stringify(history));
    }
  });
  await page.reload();
  await workflow.getByRole("button", { name: /回测验证/ }).click();
  await expect(backtestFeedback).toContainText("历史辅助反馈");
  await expect(backtestFeedback).not.toContainText("SUCCESS");

  await workflow.getByRole("button", { name: /评分选择/ }).click();
  await expect(page.getByRole("region", { name: "评分选择最近操作反馈" })).toContainText("strategy_score_id=701");

  await workflow.getByRole("button", { name: /受控 Dry-run/ }).click();
  const dryRunFeedback = page.getByRole("region", { name: "受控 Dry-run最近操作反馈" });
  await expect(dryRunFeedback).toHaveCount(1);
  await expect(dryRunFeedback).toContainText("不适用于当前对象");
  await expect(dryRunFeedback).not.toContainText("SUCCESS");
  await expectNoPageOverflow(page);
});
