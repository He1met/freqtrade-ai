import { expect, test } from "@playwright/test";

test("legacy and development routes provide a one-hop return to the formal workspace", async ({ page }) => {
  for (const path of ["/legacy/dashboard", "/strategies", "/configuration", "/research-queue", "/okx-demo", "/generation-runs", "/backtest-runs", "/backtest-tasks", "/hyperopt-runs", "/ranking", "/live-governance"]) {
    await page.goto(path);
    await expect(page.getByText("高级与历史证据")).toBeVisible();
    await expect(page.getByRole("link", { name: "返回 V1.3 工作台" })).toHaveAttribute("href", "/v13");
  }

  await page.goto("/local-strategy-lab");
  const localLabBanner = page.locator('[data-context="local-lab"]');
  await expect(localLabBanner.getByText("开发实验", { exact: true })).toBeVisible();
  await expect(localLabBanner.getByText(/不进入正式候选生命周期/)).toBeVisible();
  await expect(page.getByRole("link", { name: "返回策略目录" })).toHaveAttribute("href", "/v13/strategies");

  await page.goto("/operator-dashboard");
  await expect(page.getByRole("link", { name: "返回研究与运行" })).toHaveAttribute("href", "/v13/research");
});

test("advanced entry is the only default path to preserved development and legacy pages", async ({ page }) => {
  await page.goto("/v13");
  const navigation = page.getByRole("navigation", { name: "主导航" });
  await expect(navigation.getByRole("link", { name: "高级入口" })).toHaveAttribute("href", "/advanced");
  await expect(navigation.getByRole("link", { name: /Legacy|生成批次|Local Strategy Lab/ })).toHaveCount(0);

  await navigation.getByRole("link", { name: "高级入口" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "高级入口" })).toBeVisible();
  await expect(page.getByText("非 canonical production 权威", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("不进入正式候选生命周期", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: /Legacy 总览/ })).toHaveAttribute("href", "/legacy/dashboard");
  await expect(page.getByRole("link", { name: /Local Strategy Lab/ })).toHaveAttribute("href", "/local-strategy-lab");
});

test("desktop formal pages trust only the explicit candidate lifecycle projection", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "formal visual acceptance uses the desktop baseline only");

  const workspace = {
    schema_version: "formal-strategy-research-workspace-v2",
    as_of: "2026-08-09T12:00:00Z",
    source_type: "database",
    core_data: true,
    evidence_status: "COMPLETE",
    sections: {
      attempts: { status: "AVAILABLE", reason_code: null },
      quality: { status: "AVAILABLE", reason_code: null },
      batch: { status: "AVAILABLE", reason_code: null },
      bridge: { status: "AVAILABLE", reason_code: null },
      approval: { status: "AVAILABLE", reason_code: null },
      deployment: { status: "AVAILABLE", reason_code: null },
    },
    attempts: [],
    latest_quality_receipt: {
      id: 9,
      contract_version: "market-data-quality-v1",
      exchange: "okx",
      pair: "BTC/USDT:USDT",
      timeframe: "15m",
      file_format: "feather",
      inspected_at: "2026-08-09T11:55:00Z",
      row_count: 12000,
      first_open_at: "2025-01-01T00:00:00Z",
      last_open_at: "2026-08-09T11:45:00Z",
      expected_interval_seconds: 900,
      missing_interval_count: 0,
      duplicate_timestamp_count: 0,
      out_of_order_count: 0,
      misaligned_timestamp_count: 0,
      null_ohlcv_count: 0,
      invalid_ohlc_count: 0,
      negative_volume_count: 0,
      freshness_seconds: 600,
      status: "PASSED",
      reason_codes: [],
      created_at: "2026-08-09T11:55:00Z",
    },
    latest_batch: null,
    handoff_status: "CANONICAL_LINK_UNAVAILABLE",
    candidate_lifecycles: [{
      candidate_id: 41,
      batch_id: 7,
      candidate_name: "ExplicitBlueprintCandidate",
      research_status: "QUALIFIED",
      lifecycle_status: "BRIDGED_PENDING_CANONICAL_VALIDATION",
      reason_code: "CANONICAL_VALIDATION_REQUIRED",
      source_code_digest: "a".repeat(64),
      bridge_event_id: 5,
      bridge_outcome: "BRIDGED",
      bridge_contract_version: "formal-candidate-blueprint-v2-bridge-v1",
      blueprint_digest: "b".repeat(64),
      canonical_strategy_id: 10,
      canonical_strategy_version_id: 11,
      canonical_full_chain_run_id: 12,
      candidate_approval_id: null,
      candidate_approval_status: null,
      deployment_id: null,
      deployment_status: null,
      active_slot: null,
      created_at: "2026-08-09T12:00:00Z",
    }],
    lifecycle_summary: {
      status: "BRIDGED_PENDING_CANONICAL_VALIDATION",
      qualified_count: 1,
      unbridged_count: 0,
      pending_canonical_validation_count: 1,
      pending_approval_count: 0,
      approved_not_deployed_count: 0,
      active_demo_count: 0,
      unknown_count: 0,
      reason_code: "CANONICAL_VALIDATION_REQUIRED",
    },
  };
  let workspacePayload: Record<string, unknown> = workspace;
  await page.route("**/api/strategy-research/workspace?*", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(workspacePayload),
  }));

  await page.goto("/legacy/dashboard");
  await expect(page.getByText("1 个候选已有权威 bridge 证据")).toBeVisible();
  await expect(page.getByText("0 个", { exact: true }).first()).toBeVisible();

  await page.goto("/strategies");
  await expect(page.getByText("分钟数据：").locator("..").getByText("通过", { exact: true })).toBeVisible();
  await page.getByText("查看分钟数据质量证据").click();
  await expect(page.getByText("缺口 0 · 错位 0 · 乱序 0")).toBeVisible();

  workspacePayload = {
    ...workspace,
    schema_version: "formal-strategy-research-workspace-v1",
    sections: { ...workspace.sections, bridge: undefined },
    candidate_lifecycles: undefined,
    lifecycle_summary: undefined,
  };

  await page.reload();
  await expect(page.getByText(/生命周期未知：权威 bridge 投影不可用/)).toBeVisible();
  await expect(page.getByText(/不会从 QUALIFIED、策略名称或摘要推断/)).toBeVisible();
});
