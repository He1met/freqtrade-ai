import { expect, test } from "@playwright/test";

import { captureBrowserProblems, expectNoPageOverflow, SUPERSEDED_API_REQUESTS } from "./helpers/desktopGate";

test("keeps one explicit candidate chain and proves POST actions with matching GET records", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "Issue #428 desktop acceptance uses 1280x720.");
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);
  let backtestCreated = false;
  let blockedCreated = false;
  let scoreCreated = false;
  let backtestPosts = 0;
  let ingestPosts = 0;
  let runGets = 0;
  let rankingGets = 0;
  const runGetStates: boolean[] = [];
  let submittedProfile: Record<string, unknown> | null = null;
  let releaseBacktest!: () => void;
  let releaseIngest!: () => void;
  const backtestGate = new Promise<void>((resolve) => { releaseBacktest = resolve; });
  const ingestGate = new Promise<void>((resolve) => { releaseIngest = resolve; });

  const source = (ids: Record<string, number>) => ({
    source_type: "database",
    source_detail: "Issue #428 isolated current API record",
    core_data: true,
    database_ids: ids,
    artifact_refs: {},
    provider_provenance: "real",
    provider_name: "deepseek",
    provider_model: "deepseek-chat",
    environment: {
      scope: "current",
      runnable: true,
      migration_verified: false,
      reason: "isolated current E2E environment",
    },
  });
  const versions = [201, 202].map((id) => ({
    id,
    strategy_id: id - 100,
    generation_run_id: 101,
    version_number: 1,
    file_path: `user_data/strategies/generated/Candidate${id}.py`,
    validation_status: "valid",
    validation_errors: [],
    file_state: {
      status: "SUCCESS",
      path: `user_data/strategies/generated/Candidate${id}.py`,
      exists: true,
      is_file: true,
      checksum: `checksum-${id}`,
      checksum_matches: true,
      class_name: `Candidate${id}`,
      validation_errors: [],
    },
    data_source: source({ strategy_version_id: id, strategy_id: id - 100 }),
  }));

  await page.route("**/api/strategy-generation-runs", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([{
      id: 101,
      status: "succeeded",
      provider: "deepseek",
      model: "deepseek-chat",
      requested_count: 2,
      generated_count: 2,
      accepted_count: 2,
      failed_count: 0,
      data_source: source({ strategy_generation_run_id: 101 }),
    }]),
  }));
  await page.route("**/api/strategy-versions", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(versions),
  }));
  await page.route("**/api/strategies", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([201, 202].map((id) => ({
      id: id - 100,
      name: `Candidate${id}`,
      slug: `candidate-${id}`,
      status: "validated",
      source: "deepseek",
      current_version_id: id,
      data_source: source({ strategy_id: id - 100 }),
    }))),
  }));
  await page.route("**/api/backtest-runs", async (route) => {
    if (route.request().method() === "POST") {
      await route.continue();
      return;
    }
    runGets += 1;
    runGetStates.push(backtestCreated);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([
        ...(backtestCreated ? [{
        id: 501,
        strategy_version_id: 201,
        strategy_name: "Candidate201",
        status: "succeeded",
        profile_name: "local-strategy-lab",
        requested_task_count: 1,
        completed_task_count: 1,
        config_snapshot: {
          profile: {
            profile_name: "local-strategy-lab",
            pair: "BTC/USDT",
            timeframe: "5m",
            timerange: "20240101-20240201",
            strategy: { name: "Candidate201", path: "user_data/strategies/generated/Candidate201.py" },
            data_source: { kind: "local", exchange: "okx", datadir: "user_data/data" },
          },
        },
        data_source: source({ backtest_run_id: 501, strategy_version_id: 201 }),
        }] : []),
        ...(blockedCreated ? [{
          id: 502,
          strategy_version_id: 201,
          strategy_name: "Candidate201",
          status: "blocked",
          profile_name: "local-strategy-lab",
          requested_task_count: 1,
          completed_task_count: 0,
          config_snapshot: {
            profile: {
              profile_name: "local-strategy-lab",
              pair: "BTC/USDT",
              timeframe: "5m",
              timerange: "20240201-20240301",
              strategy: { name: "Candidate201", path: "user_data/strategies/generated/Candidate201.py" },
              data_source: { kind: "local", exchange: "okx", datadir: "user_data/data" },
            },
            blocked_reasons: ["<b>missing local candles</b>", "token=do-not-show"],
          },
          data_source: source({ backtest_run_id: 502, strategy_version_id: 201 }),
        }] : []),
      ]),
    });
  });
  await page.route("**/api/backtest-runs/local", async (route) => {
    backtestPosts += 1;
    submittedProfile = (route.request().postDataJSON() as { profile?: Record<string, unknown> }).profile ?? null;
    if (backtestPosts > 1) {
      blockedCreated = true;
      await route.fulfill({
        contentType: "application/json",
        status: 201,
        body: JSON.stringify({
          preflight_status: "blocked",
          blocked_reasons: ["<b>missing local candles</b>", "token=do-not-show"],
          run: { id: 502, status: "blocked" },
          tasks: [{ id: 602, status: "blocked" }],
        }),
      });
      return;
    }
    await backtestGate;
    backtestCreated = true;
    await route.fulfill({
      contentType: "application/json",
      status: 201,
      body: JSON.stringify({
        preflight_status: "ready",
        blocked_reasons: [],
        run: { id: 501, status: "pending" },
        tasks: [{ id: 601, status: "pending" }],
      }),
    });
  });
  await page.route("**/api/backtest-tasks", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(backtestCreated ? [{
      id: 601,
      backtest_run_id: 501,
      strategy_name: "Candidate201",
      pair: "BTC/USDT",
      timeframe: "5m",
      status: "succeeded",
      result_path: "/tmp/issue-428-result.json",
      artifact_manifest: {
        status: "SUCCESS",
        manifest_path: "/tmp/issue-428-manifest.json",
        result_path: "/tmp/issue-428-result.json",
        command_args: [],
      },
      data_source: source({ backtest_task_id: 601, backtest_run_id: 501 }),
    }] : []),
  }));
  await page.route("**/api/backtest-results", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(scoreCreated ? [{
      id: 401,
      backtest_run_id: 501,
      backtest_task_id: 601,
      result_path: "/tmp/issue-428-result.json",
      data_source: source({ backtest_result_id: 401, backtest_run_id: 501, backtest_task_id: 601 }),
    }] : []),
  }));
  await page.route("**/api/ranking", (route) => {
    rankingGets += 1;
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(scoreCreated ? [{
      rank: 1,
      score_id: 701,
      strategy_id: 101,
      strategy_version_id: 201,
      backtest_result_id: 401,
      strategy_name: "Candidate201",
      version_number: 1,
      file_path: "user_data/strategies/generated/Candidate201.py",
      total_score: 88,
      data_source: {
        ...source({
          strategy_score_id: 701,
          strategy_version_id: 201,
          backtest_result_id: 401,
        }),
        source_type: "api_aggregate",
      },
      }] : []),
    });
  });
  // This test owns its candidate/score fixtures in the browser, so its
  // promotion read must use the same deterministic lineage instead of asking
  // the isolated backend for IDs that only exist in this route fixture.
  await page.route("**/api/strategy-promotions/evaluate?*", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      status: "BLOCKED",
      reason: "Issue #527 fixture has no independently approved Demo promotion.",
      database_ids: {
        strategy_version_id: 201,
        backtest_result_id: 401,
        strategy_score_id: 701,
      },
      policy: null,
      evidence: null,
      approval: null,
    }),
  }));
  await page.route("**/api/backtest-tasks/601/artifact-ingest", async (route) => {
    ingestPosts += 1;
    await ingestGate;
    scoreCreated = true;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ingest_status: "succeeded",
        task: { id: 601 },
        run: { id: 501 },
        result: { id: 401, backtest_run_id: 501, backtest_task_id: 601 },
        score: { id: 701, strategy_version_id: 201, backtest_result_id: 401 },
      }),
    });
  });

  await page.goto("/local-strategy-lab");
  const workflow = page.getByTestId("lab-workflow");
  await expect(workflow).toHaveAttribute("data-current-stage", "backtest");
  await workflow.getByRole("button", { name: /策略生成/ }).click();
  await page.getByLabel("本地操作授权（operator token）").fill("issue-428-browser-only-token");
  await expect(page.getByLabel("本地操作授权（operator token）")).toHaveValue("issue-428-browser-only-token");
  await workflow.getByRole("button", { name: /回测验证/ }).click();

  const workbench = page.getByRole("region", { name: "候选策略到评分主操作台" });
  await expect(workbench).toBeVisible();
  const candidateSelect = workbench.getByLabel("当前 strategy version");
  await expect(candidateSelect).toHaveValue("");
  await expect(workbench.getByRole("button", { name: "触发此候选的回测" })).toBeDisabled();

  await candidateSelect.selectOption("201");
  await workbench.getByLabel("pair").fill("BTC/USDT");
  await workbench.getByLabel("timeframe").fill("5m");
  await workbench.getByLabel("timerange").fill("20240101-20240201");
  const trigger = workbench.getByRole("button", { name: "触发此候选的回测" });
  await expect(trigger).toBeEnabled();
  await trigger.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect.poll(() => backtestPosts).toBe(1);
  expect(submittedProfile).toMatchObject({
    schema_version: "2",
    profile_name: "local-strategy-lab",
    pair: "BTC/USDT",
    timeframe: "5m",
    timerange: "20240101-20240201",
    strategy: {
      name: "Candidate201",
      path: "user_data/strategies/generated/Candidate201.py",
    },
  });
  const initialRunGets = runGets;
  releaseBacktest();
  await expect.poll(() => runGets).toBeGreaterThan(initialRunGets);
  expect(runGetStates.at(-1)).toBe(true);
  await expect(workbench.getByLabel("BacktestRun")).toHaveValue("501");
  await expect(workbench.getByLabel("BacktestTask")).toHaveValue("601");

  const ingest = workbench.getByRole("button", { name: "导入此任务并评分" });
  await expect(ingest).toBeEnabled();
  await ingest.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect.poll(() => ingestPosts).toBe(1);
  const initialRankingGets = rankingGets;
  releaseIngest();
  await expect.poll(() => rankingGets).toBeGreaterThan(initialRankingGets);

  await workflow.getByRole("button", { name: /评分选择/ }).click();
  await expect(workbench).toContainText("持久评分");
  await expect(workbench).toContainText("88.0");
  await expect(workbench.getByRole("region", { name: "回测验证最近操作反馈" })).toContainText("SUCCESS");
  await expect(workbench.getByRole("region", { name: "评分选择最近操作反馈" })).toContainText("strategy_score_id=701");
  await expect(workbench).toContainText("不会启动 dry-run、live trading 或真实订单");

  await workbench.getByLabel("timerange").fill("20240201-20240301");
  const blockedRefreshBaseline = runGets;
  await workbench.getByRole("button", { name: "触发此候选的回测" }).click();
  await expect.poll(() => runGets).toBeGreaterThan(blockedRefreshBaseline);
  await workflow.getByRole("button", { name: /回测验证/ }).click();
  await expect(workbench).toContainText("missing local candles");
  await expect(workbench).toContainText("token=[REDACTED]");
  await expect(workbench).not.toContainText("do-not-show");
  await expect(workbench).toContainText("请修改 profile 后再试");
  await expect(workbench.getByRole("button", { name: "触发此候选的回测" })).toBeDisabled();
  expect(backtestPosts).toBe(2);
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});
