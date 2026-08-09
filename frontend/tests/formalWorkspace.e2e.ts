import { expect, test } from "@playwright/test";

const routes = [
  { path: "/", heading: "总览" },
  { path: "/strategies", heading: "策略工厂" },
  { path: "/okx-demo", heading: "模拟盘" },
] as const;

for (const viewport of [
  { width: 390, height: 844, label: "mobile" },
  { width: 1024, height: 768, label: "tablet" },
]) {
  test(`formal workspace is readable at ${viewport.label} width`, async ({ page }) => {
    await page.setViewportSize(viewport);
    for (const route of routes) {
      await page.goto(route.path);
      await expect(page.getByRole("heading", { level: 1, name: route.heading })).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `${route.path} has document-level horizontal overflow`).toBeLessThanOrEqual(1);
    }
  });
}

test("legacy and development routes provide a one-hop return to the formal workspace", async ({ page }) => {
  await page.goto("/generation-runs");
  await expect(page.getByText("高级与历史证据")).toBeVisible();
  await expect(page.getByRole("link", { name: "返回正式入口" })).toHaveAttribute("href", "/strategies");

  await page.goto("/local-strategy-lab");
  await expect(page.locator("#main-content").getByText("开发实验", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回策略工厂" })).toHaveAttribute("href", "/strategies");
});
