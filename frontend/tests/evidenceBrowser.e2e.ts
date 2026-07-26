import { expect, test } from "@playwright/test";

import { captureBrowserProblems, expectNoPageOverflow } from "./helpers/desktopGate";

test("evidence browser keeps one record class visible and opens audit details within 1280px", async ({ page }) => {
  const browserProblems = captureBrowserProblems(page);
  await page.goto("/local-strategy-lab?lab_tab=versions&lab_scope=diagnostic");

  const browser = page.getByTestId("lab-evidence-browser");
  await expect(browser).toBeVisible({ timeout: 20_000 });
  await expect(browser.getByRole("tab", { name: "策略版本" })).toHaveAttribute("aria-selected", "true");
  await expect(browser.getByRole("tab")).toHaveCount(4);

  const cards = browser.locator(".lab-evidence-card");
  await expect(cards.first()).toBeVisible({ timeout: 20_000 });
  expect(await cards.count()).toBeGreaterThan(0);
  await cards.first().click();
  await expect(page).toHaveURL(/lab_record=/);
  const detail = browser.getByTestId("lab-evidence-detail");
  await expect(detail).toContainText("database IDs");
  await detail.getByText("查看完整来源与环境证据").click();
  await expect(detail).toContainText("source_type");
  const copyArtifact = detail.getByRole("button", { name: /复制artifact ref/ }).first();
  const rawValue = await copyArtifact.locator("xpath=..").locator(".compact-text-value").innerText();
  await copyArtifact.click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(rawValue);
  expect(await page.evaluate(() => navigator.clipboard.readText())).not.toContain("strategy_file_path:");
  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("diagnostic URL is read-only and a stale record is never silently replaced", async ({ page }) => {
  await page.goto("/local-strategy-lab?lab_tab=generation&lab_scope=diagnostic&lab_record=missing-430");
  const browser = page.getByTestId("lab-evidence-browser");
  await expect(browser.locator(".lab-evidence-browser__diagnostic-note")).toBeVisible({ timeout: 20_000 });
  await expect(browser.getByText("记录已过期或被过滤")).toBeVisible();
  await expect(page).toHaveURL(/lab_record=missing-430/);
  await expect(page).toHaveURL(/lab_scope=diagnostic/);
});

test("invalid URL values fail closed to a legal default without selecting a record", async ({ page }) => {
  await page.goto("/local-strategy-lab?lab_tab=unknown&lab_scope=all&lab_record=");
  const browser = page.getByTestId("lab-evidence-browser");
  await expect(browser.getByRole("tab", { name: "生成记录" })).toHaveAttribute("aria-selected", "true");
  await expect(browser.getByRole("button", { name: /当前核心/ })).toHaveAttribute("aria-pressed", "true");
  await expect(browser.getByText("尚未选择记录")).toBeVisible();
});
