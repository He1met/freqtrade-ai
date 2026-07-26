import { expect, test } from "@playwright/test";

import { captureBrowserProblems, expectNoPageOverflow, SUPERSEDED_API_REQUESTS } from "./helpers/desktopGate";

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
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);
  const versionsResponse = page.waitForResponse(
    (response) => response.url().includes("/api/strategy-versions") && response.status() === 200,
  );

  await page.goto("/local-strategy-lab");

  const versions = await (await versionsResponse).json() as Array<Record<string, unknown>>;
  expect(versions.some((version) => {
    const source = version.data_source as {
      environment?: { scope?: unknown; runnable?: unknown };
    } | undefined;
    return (
      source?.environment?.scope === "current" &&
      source.environment.runnable === true
    );
  })).toBe(true);
  const generationResponse = await page.request.get("/api/strategy-generation-runs");
  expect(await generationResponse.json()).toEqual([
    expect.objectContaining({
      provider: "qa-seed",
      model: "no-call",
      params_snapshot: expect.objectContaining({ provider_executed: false }),
    }),
  ]);

  const conclusion = page.getByTestId("lab-evidence-conclusion");
  await expect(conclusion).toHaveAttribute("data-state", "NOT_ACCEPTABLE", { timeout: 20_000 });
  await expect(conclusion.getByTestId("lab-evidence-status")).toHaveText("NOT_ACCEPTABLE");
  await expect(page.getByTestId("lab-core-evidence-rejection")).toContainText("没有可证明的核心成功结果");
  await expect(page.getByTestId("lab-strategy-version-count").locator("strong")).toHaveText("0");
  await expect(page.getByTestId("lab-backtest-result-count").locator("strong")).toHaveText("1");
  await expect(page.getByTestId("lab-core-ranking-count").locator("strong")).not.toHaveText("0");
  await expect(page.getByRole("heading", { name: "非核心诊断记录（不可验收）" })).toBeVisible();
  expect(await page.locator(".lab-source-summary[data-core-source='false']").count()).toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: /回测验证/ })).toBeDisabled();
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("shows a stable NOT_RUN empty state without claiming core success", async ({ page }) => {
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);

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
