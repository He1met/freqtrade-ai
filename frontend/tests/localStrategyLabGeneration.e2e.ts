import { expect, test, type Page } from "@playwright/test";

import { captureBrowserProblems, expectNoPageOverflow, SUPERSEDED_API_REQUESTS } from "./helpers/desktopGate";

async function confirmOperatorCredentialPresence(
  page: Page,
  { deepSeek = false }: { deepSeek?: boolean } = {},
): Promise<void> {
  await page.route("**/api/runtime/operator-status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.env_presence = [
      ...(body.env_presence ?? []).filter(
        (entry: { name?: string }) =>
          entry.name !== "FREQTRADE_AI_OPERATOR_TOKEN" &&
          (!deepSeek || entry.name !== "DEEPSEEK_API_KEY"),
      ),
      {
        name: "FREQTRADE_AI_OPERATOR_TOKEN",
        present: true,
        required: false,
        source: "env",
        value_rendered: false,
      },
      ...(deepSeek
        ? [{
            name: "DEEPSEEK_API_KEY",
            present: true,
            required: false,
            source: "env",
            value_rendered: false,
          }]
        : []),
    ];
    await route.fulfill({ response, json: body });
  });
}

test("generation form explains its inputs and blockers above the 1280x720 fold", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "Issue #427 desktop acceptance uses 1280x720.");
  const browserProblems = captureBrowserProblems(page, SUPERSEDED_API_REQUESTS);
  await confirmOperatorCredentialPresence(page);
  for (const path of [
    "strategies",
    "strategy-versions",
    "strategy-generation-runs",
    "backtest-runs",
    "backtest-tasks",
    "backtest-results",
    "ranking",
  ]) {
    await page.route(`**/api/${path}`, (route) =>
      route.fulfill({ contentType: "application/json", body: "[]" }),
    );
  }

  await page.goto("/local-strategy-lab");

  const form = page.getByTestId("generation-stage-form");
  const idea = page.getByLabel("策略构想（Strategy idea）");
  const token = page.getByLabel("本地操作授权（operator token）");
  const providerAuthorization = page.getByLabel("本次提交调用真实 Provider");
  const submit = page.getByRole("button", { name: "提交生成" });
  const reasons = form.locator(".generation-stage__submit-reasons");

  await expect(form).toBeVisible();
  await expect(page.locator("#requested-count")).toHaveCount(0);
  await expect(form).toContainText("requested_count=1");
  await expect(idea).toHaveAttribute("rows", "3");
  await expect(token).toHaveAttribute("type", "password");
  await expect(form).toContainText("DeepSeek Keychain / Provider readiness");
  await expect(providerAuthorization).not.toBeChecked();
  await expect(submit).toBeDisabled();
  await expect(reasons).toContainText("输入本次请求使用的本地 operator token");

  for (const [name, locator] of [
    ["策略构想", idea],
    ["operator token", token],
    ["一次性 Provider 授权", providerAuthorization],
    ["提交按钮", submit],
    ["禁用原因", reasons],
  ] as const) {
    const bounds = await locator.boundingBox();
    expect(bounds).not.toBeNull();
    expect(
      bounds.y + bounds.height,
      `${name} must be understandable above the 1280x720 fold`,
    ).toBeLessThanOrEqual(720);
  }

  const secret = "issue-427-browser-only-token";
  await token.fill(secret);
  await expect(submit).toBeEnabled();
  const storedValues = await page.evaluate(() =>
    [...Object.values(localStorage), ...Object.values(sessionStorage)].join("\n"),
  );
  expect(storedValues).not.toContain(secret);
  await page.reload();
  await expect(page.getByLabel("本地操作授权（operator token）")).toHaveValue("");
  await expect(page.locator("body")).not.toContainText(secret);

  await expectNoPageOverflow(page);
  expect(browserProblems).toEqual([]);
});

test("real Provider authorization is separate, optional and one-request scoped", async ({ page }) => {
  await confirmOperatorCredentialPresence(page, { deepSeek: true });

  let providerAuthorizationHeader: string | undefined;
  let requestBody: unknown;
  let releaseResponse!: () => void;
  const responseGate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let postRequests = 0;
  await page.route("**/api/strategy-generation-runs", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    postRequests += 1;
    providerAuthorizationHeader = route.request().headers()["x-provider-authorization"];
    requestBody = route.request().postDataJSON();
    await responseGate;
    await route.fulfill({
      body: JSON.stringify({ detail: "Issue #427 controlled API failure" }),
      contentType: "application/json",
      status: 503,
    });
  });
  await page.goto("/local-strategy-lab");

  const form = page.getByTestId("generation-stage-form");
  const token = page.getByLabel("本地操作授权（operator token）");
  const providerAuthorization = page.getByLabel("本次提交调用真实 Provider");
  const submit = form.locator('button[type="submit"]');

  const secret = "issue-427-provider-boundary-token";
  await token.fill(secret);
  await expect(submit).toBeEnabled();
  await expect(form).toContainText("未授权真实 Provider 调用");

  await providerAuthorization.check();
  await expect(form).toContainText("仅授权下一次提交");
  await expect(form).toContainText("Provider 凭据已就绪");
  await expect(submit).toBeEnabled();

  const advancedPanel = page.getByText("高级 / 受控：DeepSeek 单次 E2E");
  await advancedPanel.click();
  await page.getByLabel("显式授权一次 DeepSeek 调用").check();
  await expect(page.getByRole("button", { name: "运行 DeepSeek 单次 E2E" })).toBeEnabled();

  await submit.click();
  await expect(submit).toHaveText("提交中");
  await expect(providerAuthorization).not.toBeChecked();
  await expect(form).toContainText("未授权真实 Provider 调用");
  await expect.poll(() => providerAuthorizationHeader).toBe("once");
  expect(postRequests).toBe(1);

  await form.evaluate((element: HTMLFormElement) => element.requestSubmit());
  expect(postRequests).toBe(1);
  await expect(providerAuthorization).not.toBeChecked();

  releaseResponse();
  await expect(submit).toHaveText("提交生成");
  expect(postRequests).toBe(1);
  expect(providerAuthorizationHeader).toBe("once");
  expect(JSON.stringify(requestBody)).not.toContain(secret);
  const persistedValues = await page.evaluate(() =>
    [...Object.values(localStorage), ...Object.values(sessionStorage)].join("\n"),
  );
  expect(persistedValues).not.toContain(secret);
  await expect(page.locator("body")).not.toContainText(secret);
  await expectNoPageOverflow(page);
});

test("Advanced DeepSeek fails closed when Operator Dashboard source is not API", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "One desktop project is sufficient for the source gate.");
  await page.route("**/api/**operator-status", async (route) => {
    await route.fulfill({
      body: JSON.stringify({
        env_presence: [
          {
            name: "DEEPSEEK_API_KEY",
            present: true,
            required: false,
            source: "env",
            value_rendered: false,
          },
        ],
        safety: { reports_env_values: false },
      }),
      contentType: "application/json",
      status: 503,
    });
  });
  await page.goto("/local-strategy-lab");

  await page.getByLabel("本地操作授权（operator token）").fill("issue-428-source-gate-token");
  await page.getByText("高级 / 受控：DeepSeek 单次 E2E").click();
  await page.getByLabel("显式授权一次 DeepSeek 调用").check();

  await expect(page.getByRole("button", { name: "运行 DeepSeek 单次 E2E" })).toBeDisabled();
  await expect(page.locator("body")).toContainText("未由真实 API 确认");
});

test("unconfirmed Provider readiness cannot be bypassed by direct form submission", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1280x720", "One desktop project is sufficient for the submit guard.");
  await confirmOperatorCredentialPresence(page);
  let postRequests = 0;
  await page.route("**/api/strategy-generation-runs", async (route) => {
    if (route.request().method() === "POST") {
      postRequests += 1;
    }
    await route.continue();
  });
  await page.goto("/local-strategy-lab");

  const form = page.getByTestId("generation-stage-form");
  await page.getByLabel("本地操作授权（operator token）").fill("issue-427-submit-guard-token");
  await page.getByLabel("本次提交调用真实 Provider").check();
  await expect(page.getByRole("button", { name: "提交生成" })).toBeDisabled();

  await form.evaluate((element: HTMLFormElement) => element.requestSubmit());
  await expect(page.locator("body")).toContainText("未提交生成请求，也未消耗一次性授权");
  expect(postRequests).toBe(0);
  await expect(page.getByLabel("本次提交调用真实 Provider")).toBeChecked();
});
