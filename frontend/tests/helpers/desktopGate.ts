import { expect, type Page } from "@playwright/test";

export type BrowserProblem = {
  kind: "console.error" | "console.warning" | "pageerror" | "requestfailed" | "http.error";
  text: string;
};

export type BrowserProblemOptions = {
  allowedHttpStatuses?: number[];
  allowedAbortedUrlPatterns?: RegExp[];
};

export const SUPERSEDED_API_REQUESTS: BrowserProblemOptions = {
  allowedAbortedUrlPatterns: [/\/api\/(?:strategies|strategy-versions|strategy-generation-runs|backtest-runs|backtest-tasks|backtest-results|hyperopt-runs|dry-run\/management|live-candidates\/governance|runtime\/read-only|runtime\/operator-status|governance-events|ranking|strategy-failure-reasons|strategy-version-lineage)(?:\?|$)/],
};

export function captureBrowserProblems(
  page: Page,
  options: BrowserProblemOptions = {},
): BrowserProblem[] {
  const problems: BrowserProblem[] = [];
  const allowedHttpStatuses = new Set(options.allowedHttpStatuses ?? []);
  const allowedAbortedUrlPatterns = options.allowedAbortedUrlPatterns ?? [];
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
  page.on("requestfailed", (request) => {
    const explicitlySuperseded =
      request.failure()?.errorText === "net::ERR_ABORTED" &&
      allowedAbortedUrlPatterns.some((pattern) => pattern.test(request.url()));
    if (explicitlySuperseded) return;
    problems.push({
      kind: "requestfailed",
      text: `${request.method()} ${request.url()} ${request.failure()?.errorText ?? "failed"}`,
    });
  });
  page.on("response", (response) => {
    const status = response.status();
    if (status >= 400 && !allowedHttpStatuses.has(status)) {
      problems.push({
        kind: "http.error",
        text: `${status} ${response.request().method()} ${response.url()}`,
      });
    }
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
