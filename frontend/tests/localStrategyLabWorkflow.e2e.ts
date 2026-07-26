import { expect, test } from "@playwright/test";

import { captureBrowserProblems, expectNoPageOverflow } from "./helpers/desktopGate";

test("shows the complete task-flow decision above the fold at 1280x720", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "Issue #426 desktop acceptance uses 1280x720.");
  const browserProblems = captureBrowserProblems(page);

  await page.goto("/local-strategy-lab");

  const workflow = page.getByTestId("lab-workflow");
  await expect(workflow).toBeVisible();
  await expect(workflow).toHaveAttribute("data-current-stage", "generation", { timeout: 20_000 });
  await expect(workflow.getByRole("button")).toHaveCount(4);
  const current = workflow.getByRole("button", { name: /策略生成/ });
  await expect(current).toHaveAttribute("aria-current", "step");
  await expect(current).toHaveAttribute("aria-pressed", "true");
  const decision = workflow.locator(".lab-workflow__decision");
  await expect(decision.getByText("当前阶段", { exact: true })).toBeVisible();
  await expect(decision.getByText("当前结论 / 阻断原因", { exact: true })).toBeVisible();
  await expect(decision.getByText("唯一推荐下一步", { exact: true })).toBeVisible();
  await expect(workflow.getByRole("button", { name: /回测验证/ })).toBeDisabled();
  await expect(workflow.getByRole("button", { name: /评分选择/ })).toBeDisabled();
  await expect(workflow.getByRole("button", { name: /受控 Dry-run/ })).toBeDisabled();

  const bounds = await workflow.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds.y + bounds.height, "task-flow decision must fit above the 720px fold").toBeLessThanOrEqual(720);
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});
