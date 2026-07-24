import { expect, test } from "@playwright/test";

import { captureBrowserProblems, expectNoPageOverflow } from "./helpers/desktopGate";

const listApiPaths = new Set([
  "/api/strategies",
  "/api/strategy-versions",
  "/api/strategy-generation-runs",
  "/api/backtest-runs",
  "/api/backtest-tasks",
  "/api/backtest-results",
  "/api/hyperopt-runs",
  "/api/governance-events",
  "/api/ranking",
  "/api/strategy-failure-reasons",
  "/api/strategy-version-lineage",
]);

test("shows fail-closed real database evidence while keeping deterministic Provider seed non-core", async ({ page }) => {
  const browserProblems = captureBrowserProblems(page);
  const versionsResponse = page.waitForResponse(
    (response) => response.url().includes("/api/strategy-versions") && response.status() === 200,
  );

  await page.goto("/local-strategy-lab");

  const versions = await (await versionsResponse).json() as Array<Record<string, unknown>>;
  expect(versions.some((version) => {
    const source = version.data_source as { core_data?: unknown } | undefined;
    return source?.core_data === false;
  })).toBe(true);

  const conclusion = page.getByTestId("lab-evidence-conclusion");
  await expect(conclusion).toHaveAttribute("data-state", "FAILED", { timeout: 20_000 });
  await expect(conclusion.getByTestId("lab-evidence-status")).toHaveText("FAILED");
  await expect(page.getByTestId("lab-core-evidence-rejection")).toContainText("没有可证明的核心成功结果");
  await expect(page.getByTestId("lab-strategy-version-count").locator("strong")).toHaveText("0");
  await expect(page.getByTestId("lab-backtest-result-count").locator("strong")).not.toHaveText("0");
  await expect(page.getByTestId("lab-core-ranking-count").locator("strong")).not.toHaveText("0");
  await expect(page.getByRole("heading", { name: "非核心诊断记录（不可验收）" })).toBeVisible();
  await expect(page.locator(".lab-source-summary[data-core-source='true']").first()).toBeVisible();
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("shows a stable NOT_RUN empty state without claiming core success", async ({ page }) => {
  const browserProblems = captureBrowserProblems(page);

  await page.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (!listApiPaths.has(pathname)) {
      await route.continue();
      return;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify([]) });
  });

  await page.goto("/local-strategy-lab");

  const conclusion = page.getByTestId("lab-evidence-conclusion");
  await expect(conclusion).toHaveAttribute("data-state", "NOT_RUN", { timeout: 20_000 });
  await expect(conclusion.getByTestId("lab-evidence-status")).toHaveText("NOT_RUN");
  await expect(page.getByTestId("lab-core-evidence-rejection")).toContainText("没有可证明的核心成功结果");
  await expect(page.getByTestId("lab-strategy-version-count").locator("strong")).toHaveText("0");
  await expect(page.getByTestId("lab-backtest-result-count").locator("strong")).toHaveText("0");
  await expect(page.getByTestId("lab-core-ranking-count").locator("strong")).toHaveText("0");
  await expect(page.locator(".lab-source-summary[data-core-source='true']")).toHaveCount(0);
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});
