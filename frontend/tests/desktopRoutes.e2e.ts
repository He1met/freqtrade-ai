import { expect, test } from "@playwright/test";

import {
  captureBrowserProblems,
  expectPageReady,
} from "./helpers/desktopGate";

const routes = [
  { path: "/", heading: "总览" },
  { path: "/strategies", heading: "策略" },
  { path: "/generation-runs", heading: "生成批次" },
  { path: "/local-strategy-lab", heading: "本地策略实验室（Local Strategy Lab）" },
  { path: "/backtest-runs", heading: "回测批次" },
  { path: "/backtest-tasks", heading: "回测任务" },
  { path: "/hyperopt-runs", heading: "Hyperopt 参数优化" },
  { path: "/live-governance", heading: "实盘候选治理" },
  { path: "/operator-dashboard", heading: "Operator Dashboard" },
  { path: "/ranking", heading: "策略排行榜" },
  { path: "/freq-ui", heading: "Dry-run / FreqUI" },
] as const;

for (const route of routes) {
  test(`${route.path} desktop route has no overflow or browser diagnostics`, async ({ page }) => {
    const problems = captureBrowserProblems(page);

    await page.goto(route.path);
    await expect(page.getByRole("heading", { level: 1, name: route.heading })).toBeVisible();
    await expectPageReady(page);

    expect(problems, `${route.path} emitted browser diagnostics`).toEqual([]);
  });
}

test("strategy detail route renders against the isolated database", async ({ page, request }) => {
  const response = await request.get("/api/strategies");
  expect(response.ok()).toBe(true);
  const strategies = await response.json() as Array<{ id: number | string }>;
  expect(strategies.length, "the isolated Phase 8 seed must include a strategy").toBeGreaterThan(0);
  const problems = captureBrowserProblems(page);

  await page.goto(`/strategies/${strategies[0].id}`);
  await expectPageReady(page);

  expect(problems).toEqual([]);
});

test("unknown route renders the desktop 404 page", async ({ page }) => {
  const problems = captureBrowserProblems(page);

  await page.goto("/__desktop-e2e-not-found__");
  await expect(page.getByText("404", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: "页面未找到" })).toBeVisible();
  await expectPageReady(page);

  expect(problems).toEqual([]);
});

test("common keyboard, disclosure, tooltip, and copy contracts work", async ({ page }) => {
  const problems = captureBrowserProblems(page);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value: string) => {
          window.localStorage.setItem("desktop-e2e-copied-value", value);
        },
      },
    });
  });
  await page.goto("/backtest-runs");
  await expectPageReady(page);

  const navLinks = page.locator(".desktop-nav a:visible");
  expect(await navLinks.count()).toBeGreaterThan(1);
  await navLinks.first().focus();
  await expect(navLinks.first()).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(navLinks.nth(1)).toBeFocused();

  const disclosure = page.locator(".backtest-technical-details").first();
  await expect(disclosure).toBeVisible();
  const summary = disclosure.locator("summary");
  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(disclosure).toHaveAttribute("open", "");

  const compactText = disclosure.locator(".compact-text").first();
  await compactText.focus();
  await expect(compactText.locator("[role='tooltip']")).toBeVisible();

  const copyButton = disclosure.locator("button.copyable-value-button").first();
  await expect(copyButton).toHaveAccessibleName(/^复制/);
  await copyButton.focus();
  await expect(copyButton).toBeFocused();
  await copyButton.press("Space");
  await expect(copyButton).toHaveAccessibleName("已复制");
  expect(await page.evaluate(() => window.localStorage.getItem("desktop-e2e-copied-value"))).toBeTruthy();

  expect(problems).toEqual([]);
});
