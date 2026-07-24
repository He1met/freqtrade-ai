import { expect, type Page } from "@playwright/test";

export type BrowserProblem = {
  kind: "console.error" | "console.warning" | "pageerror";
  text: string;
};

export function captureBrowserProblems(page: Page): BrowserProblem[] {
  const problems: BrowserProblem[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" || message.type() === "warning") {
      problems.push({
        kind: message.type() === "error" ? "console.error" : "console.warning",
        text: message.text(),
      });
    }
  });
  page.on("pageerror", (error) => {
    problems.push({ kind: "pageerror", text: error.message });
  });
  return problems;
}

export async function expectNoPageOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    body: {
      clientWidth: document.body.clientWidth,
      scrollWidth: document.body.scrollWidth,
    },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
  }));

  expect(dimensions.body.scrollWidth, "body must not overflow horizontally").toBeLessThanOrEqual(
    dimensions.body.clientWidth + 1,
  );
  expect(
    dimensions.document.scrollWidth,
    "document must not overflow horizontally",
  ).toBeLessThanOrEqual(dimensions.document.clientWidth + 1);
}

export async function expectPageReady(page: Page): Promise<void> {
  await expect(page.locator("main .page")).toBeVisible();
  await expect(page.locator("main h1").first()).toBeVisible();
  await expectNoPageOverflow(page);
}
