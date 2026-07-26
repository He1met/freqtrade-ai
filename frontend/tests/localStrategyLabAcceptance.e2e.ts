import { expect, test, type Page } from "@playwright/test";

import { captureBrowserProblems, expectNoPageOverflow, SUPERSEDED_API_REQUESTS } from "./helpers/desktopGate";

const seedProfile = process.env.E2E_SEED_PROFILE ?? "complete-current";

type ApiRecord = {
  id?: number;
  score_id?: number;
  strategy_id?: number;
  strategy_version_id?: number;
  generation_run_id?: number;
  backtest_run_id?: number;
  backtest_task_id?: number;
  backtest_result_id?: number;
  data_source?: {
    source_type?: string;
    core_data?: boolean;
    database_ids?: Record<string, number>;
    environment?: { scope?: string; runnable?: boolean };
  };
};

async function apiList(page: Page, path: string): Promise<ApiRecord[]> {
  const response = await page.request.get(path);
  expect(response.status(), `${path} must be available`).toBe(200);
  return response.json();
}

test.beforeEach(async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== "desktop-1280x720",
    "Issue #433 is a 1280x720-only acceptance gate.",
  );
  await page.addInitScript(() => window.localStorage.clear());
});

test("controlled seed reconciles the complete persisted ID chain without claiming Provider or trading execution", async ({ page }) => {
  test.skip(seedProfile !== "complete-current", "requires complete-current seed profile");
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);
  const [
    generationRuns,
    strategies,
    versions,
    backtestRuns,
    backtestTasks,
    backtestResults,
    ranking,
  ] = await Promise.all([
    apiList(page, "/api/strategy-generation-runs"),
    apiList(page, "/api/strategies"),
    apiList(page, "/api/strategy-versions"),
    apiList(page, "/api/backtest-runs"),
    apiList(page, "/api/backtest-tasks"),
    apiList(page, "/api/backtest-results"),
    apiList(page, "/api/ranking"),
  ]);

  expect(generationRuns).toHaveLength(1);
  expect(strategies).toHaveLength(1);
  expect(versions).toHaveLength(1);
  expect(backtestRuns).toHaveLength(1);
  expect(backtestTasks).toHaveLength(1);
  expect(backtestResults).toHaveLength(1);
  expect(ranking).toHaveLength(1);

  const ids = {
    generation: generationRuns[0].id,
    strategy: strategies[0].id,
    version: versions[0].id,
    run: backtestRuns[0].id,
    task: backtestTasks[0].id,
    result: backtestResults[0].id,
    score: ranking[0].score_id,
  };
  expect(Object.values(ids).every((value) => Number.isInteger(value) && Number(value) > 0)).toBe(true);
  expect(versions[0].strategy_id).toBe(ids.strategy);
  expect(versions[0].generation_run_id).toBe(ids.generation);
  expect(backtestRuns[0].strategy_version_id).toBe(ids.version);
  expect(backtestTasks[0].backtest_run_id).toBe(ids.run);
  expect(backtestResults[0].backtest_run_id).toBe(ids.run);
  expect(backtestResults[0].backtest_task_id).toBe(ids.task);
  expect(ranking[0].strategy_version_id).toBe(ids.version);
  expect(ranking[0].backtest_result_id).toBe(ids.result);

  for (const record of [versions[0], backtestResults[0], ranking[0]]) {
    expect(["database", "api_aggregate"]).toContain(record.data_source?.source_type);
    expect(record.data_source?.core_data).toBe(true);
    expect(record.data_source?.environment?.scope).toBe("current");
    expect(record.data_source?.environment?.runnable).toBe(true);
  }
  expect(generationRuns[0]).toMatchObject({
    provider: "qa-seed",
    model: "no-call",
  });

  await page.goto("/local-strategy-lab?lab_tab=scores&lab_scope=diagnostic");
  const workflow = page.getByTestId("lab-workflow");
  await expect(workflow).toHaveAttribute("data-current-stage", "generation", { timeout: 20_000 });
  await expect(workflow).toContainText("Provider");
  await expect(workflow).toContainText(/unknown|不明确|未知/i);

  const browser = page.getByTestId("lab-evidence-browser");
  await expect(browser.getByRole("tab", { name: "评分" })).toHaveAttribute("aria-selected", "true");
  await expect(browser.locator(".lab-evidence-card")).toHaveCount(1);
  await browser.locator(".lab-evidence-card").click();
  const detail = browser.getByTestId("lab-evidence-detail");
  await expect(detail).toContainText(`strategy_score_id`);
  await expect(detail).toContainText(String(ids.score));
  await expect(detail).toContainText(String(ids.result));
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("isolated backend exposes no Provider credential or trading capability", async ({ page }) => {
  test.skip(seedProfile !== "complete-current", "requires complete-current seed profile");
  const operatorResponse = await page.request.get("/api/runtime/operator-status");
  expect(operatorResponse.status()).toBe(200);
  const operator = await operatorResponse.json();
  const deepseek = operator.env_presence.find(
    (entry: { name?: string }) => entry.name === "DEEPSEEK_API_KEY",
  );
  expect(deepseek).toMatchObject({ present: false, value_rendered: false });
  expect(operator.safety).toMatchObject({
    allow_live_trading: false,
    allow_real_orders: false,
    allow_exchange_connection: false,
    can_start_stop_bot: false,
  });

  const runtimeResponse = await page.request.get("/api/runtime/read-only");
  expect(runtimeResponse.status()).toBe(200);
  const runtime = await runtimeResponse.json();
  expect(runtime.safety).toMatchObject({
    allow_live_trading: false,
    allow_real_orders: false,
    allow_exchange_connection: false,
    can_start_stop_bot: false,
  });
  expect(JSON.stringify({ operator, runtime })).not.toContain("must-not-pass");
});

test("empty API remains NOT_RUN and browser history cannot unlock later stages", async ({ page }) => {
  test.skip(seedProfile !== "empty", "requires empty seed profile");
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "freqtrade-ai.local-strategy-lab.action-evidence.v2",
      JSON.stringify([{
        schemaVersion: 2,
        eventId: "forged-success",
        lifecycleId: "forged-success",
        environmentScope: "current",
        phase: "score",
        action: "导入回测结果并计算评分",
        artifactPaths: [],
        entityIds: {
          strategy_version_id: "999",
          backtest_result_id: "998",
          strategy_score_id: "997",
        },
        databaseIds: {
          strategy_version_id: "999",
          backtest_result_id: "998",
          strategy_score_id: "997",
        },
        message: "browser-only success",
        nextAction: "should not progress",
        recommendBug: false,
        repeatCount: 1,
        status: "SUCCESS",
        updatedAt: "2026-07-26T00:00:00Z",
      }]),
    );
  });

  await page.goto("/local-strategy-lab");
  const workflow = page.getByTestId("lab-workflow");
  await expect(workflow).toHaveAttribute("data-current-stage", "generation", { timeout: 20_000 });
  await expect(workflow).toContainText("NOT_RUN");
  await expect(workflow.getByRole("button", { name: /回测验证/ })).toBeDisabled();
  await expect(workflow.getByRole("button", { name: /评分选择/ })).toBeDisabled();
  await expect(workflow.getByRole("button", { name: /受控 Dry-run/ })).toBeDisabled();

  const form = page.getByTestId("generation-stage-form");
  const token = page.getByLabel("本地操作授权（operator token）");
  const secret = "issue-433-browser-only-secret";
  await expect(form.getByRole("button", { name: "提交生成" })).toBeDisabled();
  await expect(form).toContainText("operator token");
  await token.fill(secret);
  const storage = await page.evaluate(() =>
    [...Object.values(localStorage), ...Object.values(sessionStorage)].join("\n"),
  );
  expect(storage).not.toContain(secret);
  await expect(page.locator("body")).not.toContainText(secret);
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("missing BacktestResult is explicit and cannot promote a score", async ({ page }) => {
  test.skip(seedProfile !== "missing-result", "requires missing-result seed profile");
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);
  expect(await apiList(page, "/api/backtest-results")).toEqual([]);
  expect(await apiList(page, "/api/ranking")).toEqual([]);

  await page.goto("/local-strategy-lab");
  const workflow = page.getByTestId("lab-workflow");
  await expect(workflow).toHaveAttribute("data-current-stage", "generation", { timeout: 20_000 });
  await expect(workflow.getByRole("button", { name: /评分选择/ })).toBeDisabled();
  await expect(page.locator("body")).toContainText(/BacktestResult|回测结果/);
  await expect(page.locator("body")).toContainText("尚未观察到有 database ID 和 artifact path 的核心回测结果");
  await expect(page.locator("body")).toContainText("NOT_RUN");
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("missing strategy artifact stays BLOCKED and diagnostic-only", async ({ page }) => {
  test.skip(seedProfile !== "missing-strategy", "requires missing-strategy seed profile");
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);
  const versions = await apiList(page, "/api/strategy-versions");
  expect(versions).toHaveLength(1);
  expect(versions[0].data_source?.core_data).toBe(false);
  expect(versions[0].data_source?.environment?.scope).toBe("current");
  expect(versions[0].data_source?.environment?.runnable).toBe(false);

  await page.goto("/local-strategy-lab?lab_tab=versions&lab_scope=diagnostic");
  const browser = page.getByTestId("lab-evidence-browser");
  await expect(browser.locator(".lab-evidence-card")).toHaveCount(1);
  await browser.locator(".lab-evidence-card").click();
  await expect(browser.getByTestId("lab-evidence-detail")).toContainText(/BLOCKED|不可用|does not exist/);
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("long persisted diagnostics and multiple IDs remain inspectable without page overflow", async ({ page }) => {
  test.skip(seedProfile !== "long-evidence", "requires long-evidence seed profile");
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);
  const tasks = await apiList(page, "/api/backtest-tasks");
  expect(tasks).toHaveLength(1);
  expect(JSON.stringify(tasks[0])).toContain("controlled long diagnostic");

  await page.goto("/local-strategy-lab?lab_tab=backtests&lab_scope=diagnostic");
  const browser = page.getByTestId("lab-evidence-browser");
  await expect(browser.locator(".lab-evidence-card")).toHaveCount(1);
  await browser.locator(".lab-evidence-card").click();
  const detail = browser.getByTestId("lab-evidence-detail");
  await expect(detail).toContainText("database IDs");
  await expect(detail).toContainText(/backtest_result_id|backtest_task_id/);
  await expect(detail).toContainText("controlled long diagnostic");
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});
