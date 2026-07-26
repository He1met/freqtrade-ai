import { expect, test } from "@playwright/test";

import { captureBrowserProblems, expectNoPageOverflow, SUPERSEDED_API_REQUESTS } from "./helpers/desktopGate";

test("shows one Dry-run decision, one blocker and at most one primary action at 1280x720", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "Issue #429 desktop acceptance uses 1280x720.");
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);
  const source = (key: string, id: number) => ({
    source_type: "database",
    source_detail: "isolated current E2E database record",
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
  await page.route("**/api/strategy-generation-runs", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 101, status: "succeeded", provider: "deepseek", model: "deepseek-chat",
      requested_count: 1, generated_count: 1, accepted_count: 1, failed_count: 0,
      data_source: source("strategy_generation_run_id", 101),
    }]),
  }));
  await page.route("**/api/strategy-versions", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 201, strategy_id: 301, generation_run_id: 101, version_number: 1,
      file_path: "user_data/strategies/generated/E2E.py", validation_status: "valid",
      file_state: { status: "SUCCESS", path: "user_data/strategies/generated/E2E.py", exists: true, is_file: true },
      data_source: source("strategy_version_id", 201),
    }]),
  }));
  await page.route("**/api/strategies", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 301, name: "E2E", slug: "e2e", status: "validated", source: "deepseek",
      current_version_id: 201, data_source: source("strategy_id", 301),
    }]),
  }));
  await page.route("**/api/backtest-results", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 401, backtest_run_id: 501, backtest_task_id: 601, result_path: "/tmp/result.json",
      data_source: source("backtest_result_id", 401),
    }]),
  }));
  await page.route("**/api/ranking", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      rank: 1, score_id: 701, strategy_id: 301, strategy_version_id: 201,
      backtest_result_id: 401, strategy_name: "E2E", version_number: 1,
      file_path: "user_data/strategies/generated/E2E.py", total_score: 80,
      data_source: source("strategy_score_id", 701),
    }]),
  }));
  await page.goto("/local-strategy-lab");

  const workflow = page.getByTestId("lab-workflow");
  await expect(workflow).toHaveAttribute("data-current-stage", "dry-run", { timeout: 20_000 });
  await workflow.getByRole("button", { name: /受控 Dry-run/ }).click();

  const decision = page.getByTestId("dry-run-decision");
  await expect(decision).toBeVisible();
  await expect(page.getByText("Readiness 与受控 Dry-run", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Dry-run readiness", exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Controlled dry-run", exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "运行域就绪状态", exact: true })).toHaveCount(0);
  await expect(decision.getByText("唯一阻断原因", { exact: true })).toHaveCount(1);
  await expect(decision.locator("button.primary-button")).toHaveCount(1);
  await expect(decision).not.toContainText("live-ready");
  await expect(decision.locator("details")).not.toHaveAttribute("open", "");
  const decisionBounds = await decision.boundingBox();
  expect(decisionBounds).not.toBeNull();
  expect(decisionBounds.height, "the complete decision must fit in one 720px viewport").toBeLessThanOrEqual(720);
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("reconciles READY to RUNNING and RUNNING to STOPPED using persistent management", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "Issue #429 state transition acceptance uses 1280x720.");
  const source = (key: string, id: number) => ({
    source_type: "database",
    source_detail: "isolated current E2E database record",
    core_data: true,
    database_ids: { [key]: id },
    artifact_refs: {},
    environment: { scope: "current", runnable: true, migration_verified: false, reason: "current E2E" },
  });
  await page.route("**/api/strategy-generation-runs", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 101, status: "succeeded", provider: "deepseek", model: "deepseek-chat",
      requested_count: 1, generated_count: 1, accepted_count: 1, failed_count: 0,
      data_source: source("strategy_generation_run_id", 101),
    }]),
  }));
  await page.route("**/api/strategy-versions", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 201, strategy_id: 301, generation_run_id: 101, version_number: 1,
      file_path: "user_data/strategies/generated/E2E.py", validation_status: "valid",
      file_state: { status: "SUCCESS", path: "user_data/strategies/generated/E2E.py", exists: true, is_file: true },
      data_source: source("strategy_version_id", 201),
    }]),
  }));
  await page.route("**/api/strategies", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 301, name: "E2E", status: "validated", source: "deepseek", current_version_id: 201,
      data_source: source("strategy_id", 301),
    }]),
  }));
  await page.route("**/api/backtest-results", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 401, backtest_run_id: 501, backtest_task_id: 601, result_path: "/tmp/result.json",
      data_source: source("backtest_result_id", 401),
    }]),
  }));
  await page.route("**/api/ranking", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      rank: 1, score_id: 701, strategy_id: 301, strategy_version_id: 201,
      backtest_result_id: 401, strategy_name: "E2E", version_number: 1,
      file_path: "user_data/strategies/generated/E2E.py", total_score: 80,
      data_source: source("strategy_score_id", 701),
    }]),
  }));

  let persistedStatus = "BLOCKED";
  let failManagementRefresh = false;
  const management = () => {
    const visibleStatus = persistedStatus;
    return ({
    manifest: {
      status: visibleStatus === "RUNNING" ? "SUCCESS" : visibleStatus,
      profile_name: "local-e2e",
      strategy_version_id: 201,
      strategy_name: "E2E",
      manifest_path: "/current/dry-run.json",
      config_path: "/current/config.json",
      command_args: ["freqtrade", "trade", "--dry-run"],
      blocked_reason: visibleStatus === "BLOCKED" ? "尚未同步最新控制结果" : null,
    },
    status_snapshot: {
      status: visibleStatus,
      profile_name: "local-e2e",
      strategy_version_id: 201,
      strategy_name: "E2E",
      dry_run: true,
      artifact_manifest_path: "/current/dry-run.json",
      blocked_reason: visibleStatus === "BLOCKED" ? "尚未同步最新控制结果" : null,
      recent_events: [],
    },
    freq_ui_link: { enabled: false },
  });
  };
  const fulfillManagement = (route: Parameters<Parameters<typeof page.route>[1]>[0]) => {
    if (failManagementRefresh) {
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "isolated management refresh failed" }),
      });
    }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(management()) });
  };
  await page.route("**/api/dry-run/management", fulfillManagement);
  await page.route("**/api/dry-run/status", fulfillManagement);
  await page.route("**/api/mvp/dry-run", fulfillManagement);
  await page.route("**/api/dry-run/readiness", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      status: "READY",
      generated_at: "2026-07-26T00:00:00Z",
      strategy_version_id: 201,
      profile_name: "local-e2e",
      blocked_reasons: [],
      checks: [{ name: "dry_run_config_preview", status: "READY", summary: "safe", evidence: {} }],
      config_preview: { dry_run: true, initial_state: "stopped" },
      safety: {
        readiness_only: true, starts_freqtrade: false, exchange_connection: false,
        live_trading: false, real_orders: false,
      },
    }),
  }));
  await page.route("**/api/dry-run/control/start", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 180));
    persistedStatus = "RUNNING";
    failManagementRefresh = true;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        status: "SUCCESS", manifest_path: "/current/dry-run.json",
        status_snapshot_path: "/current/status.json", status_snapshot: management().status_snapshot,
        blocked_reasons: [], safety: {},
      }),
    });
  });
  let stopAttempts = 0;
  await page.route("**/api/dry-run/control/stop", async (route) => {
    stopAttempts += 1;
    await new Promise((resolve) => setTimeout(resolve, 180));
    if (stopAttempts === 1) {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "isolated stop failure" }),
      });
      return;
    }
    persistedStatus = "STOPPED";
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        status: "STOPPED", manifest_path: "/current/dry-run.json",
        status_snapshot_path: "/current/status.json", status_snapshot: management().status_snapshot,
        blocked_reasons: [], safety: {},
      }),
    });
  });

  await page.goto("/local-strategy-lab");
  const workflow = page.getByTestId("lab-workflow");
  await expect(workflow).toHaveAttribute("data-current-stage", "dry-run", { timeout: 20_000 });
  await workflow.getByRole("button", { name: /受控 Dry-run/ }).click();
  const decision = page.getByTestId("dry-run-decision");
  await decision.getByRole("button", { name: "重新检查" }).click();
  await expect(decision).toHaveAttribute("data-state", "READY");

  const token = decision.getByLabel("Operator token");
  await expect(token).toHaveAttribute("type", "password");
  await expect(token).toHaveAttribute("autocomplete", "off");
  await token.fill("local-e2e-token");
  await decision.getByLabel("人工批准本次受控 Dry-run").check();
  await decision.getByRole("button", { name: "启动 Dry-run" }).click();
  await expect(decision).toHaveAttribute("data-state", "STARTING");
  await expect(decision.locator("button.primary-button")).toHaveCount(0);
  await expect(decision.getByText("启动中", { exact: true })).toBeVisible();
  await expect(decision).toHaveAttribute("data-state", "BLOCKED", { timeout: 10_000 });
  await expect(decision).toContainText("刷新 management 失败");

  failManagementRefresh = false;
  await decision.getByRole("button", { name: "刷新状态" }).click();
  await expect(decision).toHaveAttribute("data-state", "STARTING");
  await expect(decision).toHaveAttribute("data-state", "RUNNING", { timeout: 10_000 });

  await decision.getByRole("button", { name: "停止 Dry-run" }).click();
  await expect(decision).toHaveAttribute("data-state", "STOPPING");
  await expect(decision.getByText("停止中", { exact: true })).toBeVisible();
  await expect(decision).toHaveAttribute("data-state", "RUNNING", { timeout: 10_000 });
  await expect(decision.getByRole("region", { name: "受控 Dry-run最近操作反馈" })).toContainText("FAILED");

  await decision.getByRole("button", { name: "停止 Dry-run" }).click();
  await expect(decision).toHaveAttribute("data-state", "STOPPING");
  await expect(decision).toHaveAttribute("data-state", "STOPPED", { timeout: 10_000 });
});

test("candidate changes and disappearance clear readiness and manual approval", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "Issue #429 candidate-scoped readiness uses 1280x720.");
  const source = (key: string, id: number) => ({
    source_type: "database",
    source_detail: "isolated current E2E database record",
    core_data: true,
    database_ids: { [key]: id },
    artifact_refs: {},
    environment: { scope: "current", runnable: true, migration_verified: false, reason: "current E2E" },
  });
  await page.route("**/api/strategy-generation-runs", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 101, status: "succeeded", provider: "deepseek", model: "deepseek-chat",
      requested_count: 2, generated_count: 2, accepted_count: 2, failed_count: 0,
      data_source: source("strategy_generation_run_id", 101),
    }]),
  }));
  await page.route("**/api/strategy-versions", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([
      {
        id: 201, strategy_id: 301, generation_run_id: 101, version_number: 1,
        file_path: "user_data/strategies/generated/E2EA.py", validation_status: "valid",
        file_state: {
          status: "SUCCESS", path: "user_data/strategies/generated/E2EA.py",
          exists: true, is_file: true, class_name: "E2EA",
        },
        data_source: source("strategy_version_id", 201),
      },
      {
        id: 202, strategy_id: 302, generation_run_id: 101, version_number: 1,
        file_path: "user_data/strategies/generated/E2EB.py", validation_status: "valid",
        file_state: {
          status: "SUCCESS", path: "user_data/strategies/generated/E2EB.py",
          exists: true, is_file: true, class_name: "E2EB",
        },
        data_source: source("strategy_version_id", 202),
      },
    ]),
  }));
  await page.route("**/api/strategies", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([
      {
        id: 301, name: "E2EA", status: "validated", source: "deepseek", current_version_id: 201,
        data_source: source("strategy_id", 301),
      },
      {
        id: 302, name: "E2EB", status: "validated", source: "deepseek", current_version_id: 202,
        data_source: source("strategy_id", 302),
      },
    ]),
  }));
  await page.route("**/api/backtest-results", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 401, backtest_run_id: 501, backtest_task_id: 601, result_path: "/tmp/result.json",
      data_source: source("backtest_result_id", 401),
    }]),
  }));
  await page.route("**/api/ranking", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      rank: 1, score_id: 701, strategy_id: 301, strategy_version_id: 201,
      backtest_result_id: 401, strategy_name: "E2EA", version_number: 1,
      file_path: "user_data/strategies/generated/E2EA.py", total_score: 80,
      data_source: {
        ...source("strategy_score_id", 701),
        source_type: "api_aggregate",
        database_ids: {
          strategy_score_id: 701,
          strategy_version_id: 201,
          backtest_result_id: 401,
        },
      },
    }]),
  }));
  let readinessMode: "immediate" | "delayed-success" | "delayed-error" | "mismatched-success" = "immediate";
  let releaseReadiness: (() => void) | null = null;
  await page.route("**/api/dry-run/readiness", async (route) => {
    const request = route.request().postDataJSON() as { strategy_version_id: number };
    const requestMode = readinessMode;
    if (requestMode === "delayed-success" || requestMode === "delayed-error") {
      await new Promise<void>((resolve) => {
        releaseReadiness = resolve;
      });
    }
    if (requestMode === "delayed-error") {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "delayed readiness failure for the old candidate" }),
      });
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        status: "READY",
        generated_at: "2026-07-26T00:00:00Z",
        strategy_version_id: requestMode === "mismatched-success" ? 999 : request.strategy_version_id,
        profile_name: "local-e2e",
        blocked_reasons: [],
        checks: [{ name: "dry_run_config_preview", status: "READY", summary: "safe", evidence: {} }],
        config_preview: { dry_run: true, initial_state: "stopped" },
        safety: {
          readiness_only: true, starts_freqtrade: false, exchange_connection: false,
          live_trading: false, real_orders: false,
        },
      }),
    });
  });

  await page.goto("/local-strategy-lab");
  const workflow = page.getByTestId("lab-workflow");
  await expect(workflow).toHaveAttribute("data-current-stage", "dry-run", { timeout: 20_000 });
  await workflow.getByRole("button", { name: /受控 Dry-run/ }).click();

  const candidateSelect = page.getByLabel("当前 strategy version");
  const decision = page.getByTestId("dry-run-decision");
  await candidateSelect.selectOption("201", { force: true });
  await decision.getByRole("button", { name: "重新检查" }).click();
  await expect(decision).toHaveAttribute("data-state", "READY");
  const approval = decision.getByLabel("人工批准本次受控 Dry-run");
  await approval.check();
  await expect(approval).toBeChecked();

  await candidateSelect.selectOption("202", { force: true });
  await expect(decision).toHaveAttribute("data-state", "BLOCKED");
  await expect(decision.locator(".dry-run-decision__summary").getByText("NOT_CHECKED", { exact: true })).toBeVisible();
  await decision.getByRole("button", { name: "重新检查" }).click();
  await expect(decision).toHaveAttribute("data-state", "READY");
  await expect(decision.getByLabel("人工批准本次受控 Dry-run")).not.toBeChecked();

  await decision.getByLabel("人工批准本次受控 Dry-run").check();
  await candidateSelect.selectOption("", { force: true });
  await expect(decision).toHaveAttribute("data-state", "BLOCKED");
  await expect(decision.getByLabel("人工批准本次受控 Dry-run")).toHaveCount(0);

  await candidateSelect.selectOption("201", { force: true });
  await expect(decision).toHaveAttribute("data-state", "BLOCKED");
  await expect(decision.locator(".dry-run-decision__summary").getByText("NOT_CHECKED", { exact: true })).toBeVisible();
  await decision.getByRole("button", { name: "重新检查" }).click();
  await expect(decision).toHaveAttribute("data-state", "READY");
  await expect(decision.getByLabel("人工批准本次受控 Dry-run")).not.toBeChecked();

  await candidateSelect.selectOption("202", { force: true });
  await candidateSelect.selectOption("201", { force: true });
  readinessMode = "delayed-success";
  await decision.getByRole("button", { name: "重新检查" }).click();
  await expect(decision).toHaveAttribute("data-state", "CHECKING");
  await candidateSelect.selectOption("202", { force: true });
  await expect(decision).toHaveAttribute("data-state", "BLOCKED");
  const releaseOldSuccess = releaseReadiness;
  expect(releaseOldSuccess).not.toBeNull();

  readinessMode = "immediate";
  await decision.getByRole("button", { name: "重新检查" }).click();
  await expect(decision).toHaveAttribute("data-state", "READY");
  await decision.getByLabel("人工批准本次受控 Dry-run").check();
  releaseOldSuccess?.();
  await expect(decision).toHaveAttribute("data-state", "READY");
  await expect(decision.getByLabel("人工批准本次受控 Dry-run")).toBeChecked();
  await expect(decision.getByRole("region", { name: "受控 Dry-run最近操作反馈" })).toContainText("SUCCESS");

  await candidateSelect.selectOption("201", { force: true });
  readinessMode = "delayed-error";
  releaseReadiness = null;
  await decision.getByRole("button", { name: "重新检查" }).click();
  await expect(decision).toHaveAttribute("data-state", "CHECKING");
  await candidateSelect.selectOption("", { force: true });
  await candidateSelect.selectOption("201", { force: true });
  await expect(decision).toHaveAttribute("data-state", "BLOCKED");
  const releaseOldError = releaseReadiness;
  expect(releaseOldError).not.toBeNull();

  readinessMode = "immediate";
  await decision.getByRole("button", { name: "重新检查" }).click();
  await expect(decision).toHaveAttribute("data-state", "READY");
  await decision.getByLabel("人工批准本次受控 Dry-run").check();
  releaseOldError?.();
  await expect(decision).toHaveAttribute("data-state", "READY");
  await expect(decision.getByLabel("人工批准本次受控 Dry-run")).toBeChecked();
  const latestFeedback = decision.getByRole("region", { name: "受控 Dry-run最近操作反馈" });
  await expect(latestFeedback).toContainText("SUCCESS");
  await expect(latestFeedback).not.toContainText("delayed readiness failure");

  await candidateSelect.selectOption("", { force: true });
  await candidateSelect.selectOption("201", { force: true });
  readinessMode = "mismatched-success";
  await decision.getByRole("button", { name: "重新检查" }).click();
  await expect(decision).toHaveAttribute("data-state", "BLOCKED");
  await expect(decision.getByLabel("人工批准本次受控 Dry-run")).toHaveCount(0);
  await expect(latestFeedback).toContainText("不适用于当前对象");
  await page.getByText(/操作历史/).click();
  await expect(page.getByRole("region", { name: "本浏览器操作反馈" })).toContainText("API_GAP");
  await expect(page.getByRole("region", { name: "本浏览器操作反馈" })).toContainText("strategy_version_id=999");
});
