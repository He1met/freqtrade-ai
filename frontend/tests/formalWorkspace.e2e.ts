import { expect, test } from "@playwright/test";

test("legacy and development routes provide a one-hop return to the formal workspace", async ({ page }) => {
  await page.goto("/generation-runs");
  await expect(page.getByText("高级与历史证据")).toBeVisible();
  await expect(page.getByRole("link", { name: "返回策略工厂" })).toHaveAttribute("href", "/strategies");

  await page.goto("/local-strategy-lab");
  await expect(page.locator("#main-content").getByText(/开发实验，不进入正式候选生命周期/)).toBeVisible();
  await expect(page.getByRole("link", { name: "返回策略工厂" })).toHaveAttribute("href", "/strategies");

  await page.goto("/operator-dashboard");
  await expect(page.getByRole("link", { name: "返回模拟盘" })).toHaveAttribute("href", "/okx-demo");
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
    latest_quality_receipt: null,
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
  await page.route("**/api/strategy-research/workspace?*", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(workspace),
  }));

  await page.goto("/");
  await expect(page.getByText("1 个候选已有权威 bridge 证据")).toBeVisible();
  await expect(page.getByText("0 个", { exact: true }).first()).toBeVisible();

  await page.unroute("**/api/strategy-research/workspace?*");
  await page.route("**/api/strategy-research/workspace?*", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      ...workspace,
      schema_version: "formal-strategy-research-workspace-v1",
      sections: { ...workspace.sections, bridge: undefined },
      candidate_lifecycles: undefined,
      lifecycle_summary: undefined,
    }),
  }));

  await page.goto("/strategies");
  await expect(page.getByText(/生命周期未知：权威 bridge 投影不可用/)).toBeVisible();
  await expect(page.getByText(/不会从 QUALIFIED、策略名称或摘要推断/)).toBeVisible();
});
