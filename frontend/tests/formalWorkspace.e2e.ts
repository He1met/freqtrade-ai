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
