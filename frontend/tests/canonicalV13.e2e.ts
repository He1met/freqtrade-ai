import { expect, test, type Page, type Route } from "@playwright/test";

const ID_A = "123e4567-e89b-42d3-a456-426614174000";
const ID_B = "123e4567-e89b-42d3-a456-426614174001";
const DIGEST = "a".repeat(64);

const emptyResponses: Record<string, unknown> = {
  "/api/canonical-v13/strategies": { status: "EMPTY", items: [] },
  "/api/canonical-v13/configurations": {
    status: "UNSET",
    configured_kinds: [],
    unset_kinds: ["TARGET", "WINDOW", "GENERATION", "DIVERSITY", "QUALITY_QUALIFICATION", "SCORING", "RESEARCH_AGGREGATE"],
    items: [],
  },
  "/api/canonical-v13/market-data": {
    status: "MARKET_SNAPSHOT_UNSET",
    profile_count: 0,
    validated_profile_count: 0,
    artifact_count: 0,
    accepted_receipt_count: 0,
    snapshots: [],
  },
  "/api/canonical-v13/readiness/research": {
    status: "BLOCKED",
    reason_codes: ["RESEARCH_BUNDLE_UNSET"],
    scope_key: null,
    workflow_key: null,
    configuration_bundle_id: null,
    bundle_digest: null,
    market_snapshot_id: null,
    target_count: null,
    total_candidate_count: null,
    deployment_id: null,
    runtime_instance_id: null,
  },
  "/api/canonical-v13/readiness/runtime": {
    status: "BLOCKED",
    reason_codes: ["TRADING_DISABLED", "ACTIVE_DEPLOYMENT_UNSET"],
    scope_key: null,
    workflow_key: null,
    configuration_bundle_id: null,
    bundle_digest: null,
    market_snapshot_id: null,
    target_count: null,
    total_candidate_count: null,
    deployment_id: null,
    runtime_instance_id: null,
  },
  "/api/canonical-v13/optimizations": { status: "PENDING_FIRST_BACKTEST", items: [] },
};

type MockOverride = unknown | ((route: Route) => Promise<void> | void);

async function installCanonicalMocks(page: Page, overrides: Record<string, MockOverride> = {}) {
  const calls: string[] = [];
  await page.route(/^https?:\/\/[^/]+\/api\/(?!canonical-v13(?:\/|$)).*/, async (route) => {
    const url = new URL(route.request().url());
    calls.push(`${route.request().method()} ${url.pathname}${url.search}`);
    await route.fulfill({ status: 599, body: "legacy API forbidden in canonical E2E" });
  });
  await page.route("**/api/canonical-v13/**", async (route) => {
    const url = new URL(route.request().url());
    const key = url.pathname;
    calls.push(`${route.request().method()} ${key}${url.search}`);
    const override = overrides[key];
    if (typeof override === "function") {
      await override(route);
      return;
    }
    const body = override ?? emptyResponses[key];
    if (body === undefined) {
      await route.fulfill({ status: 404, body: "unmocked canonical route" });
      return;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body), status: 200 });
  });
  return calls;
}

test("six canonical routes render true empty, blocked, and pending states without legacy requests", async ({ page }) => {
  const calls = await installCanonicalMocks(page);
  const routes = [
    ["/v13/submission", "Strategy Submission"],
    ["/v13/strategies", "Strategy Catalog"],
    ["/v13/configuration", "Configuration Center"],
    ["/v13/market-data", "Market Data"],
    ["/v13/research", "Research / Runtime Readiness"],
    ["/v13/optimization", "Optimization"],
  ] as const;
  for (const [path, heading] of routes) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
  }
  await expect(page.getByText("PENDING_FIRST_BACKTEST", { exact: true })).toBeVisible();
  expect(calls.every((call) => call.includes("/api/canonical-v13/"))).toBe(true);
  expect(calls.some((call) => call.includes("/api/v1") || call.includes("/api/strategies"))).toBe(false);
});

test("invalid URL state on every canonical page performs zero API requests", async ({ page }) => {
  const calls = await installCanonicalMocks(page);
  const invalidRoutes = [
    "/v13/submission?legacy=1",
    "/v13/strategies?strategy=not-a-uuid",
    "/v13/configuration?scope=research",
    "/v13/market-data?profile=not-a-uuid",
    "/v13/research?scope=research",
    "/v13/optimization?strategy=not-a-uuid",
  ];
  for (const path of invalidRoutes) {
    await page.goto(path);
    await expect(page.getByText("INVALID_URL_STATE", { exact: true })).toBeVisible();
  }
  await page.waitForTimeout(100);
  expect(calls).toEqual([]);
});

test("strategy selection is explicit, deep-linkable, refreshable, and restorable", async ({ page }) => {
  const item = (id: string, name: string) => ({
    strategy_id: id,
    display_name: name,
    catalog_status: "DRAFT",
    intake_status: "INTAKE_ACCEPTED",
    current_version_id: id,
    version_number: 1,
    artifact_id: id,
    artifact_digest: DIGEST,
    validation_status: "UNVALIDATED",
    qualification_status: "NOT_EVALUATED",
    execution_authorized: false,
    created_at: "2026-08-14T00:00:00Z",
  });
  const alpha = item(ID_A, "Alpha");
  const beta = item(ID_B, "Beta");
  const calls = await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": { status: "AVAILABLE", items: [alpha, beta] },
    [`/api/canonical-v13/strategies/${ID_A}`]: alpha,
    [`/api/canonical-v13/strategies/${ID_B}`]: beta,
  });
  await page.goto("/v13/strategies");
  await expect(page.getByText("尚未选择策略")).toBeVisible();
  expect(calls.some((call) => call.includes(`/strategies/${ID_A}`))).toBe(false);

  await page.getByRole("button", { name: /Alpha/ }).click();
  await expect(page).toHaveURL(new RegExp(`strategy=${ID_A}`));
  await expect(page.getByRole("heading", { level: 2, name: "Alpha" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { level: 2, name: "Alpha" })).toBeVisible();
  await page.goBack();
  await expect(page).not.toHaveURL(/strategy=/);
  await expect(page.getByText("尚未选择策略")).toBeVisible();
});

test("research and runtime errors remain independent", async ({ page }) => {
  await installCanonicalMocks(page, {
    "/api/canonical-v13/readiness/research": async (route) => route.fulfill({
      contentType: "application/json",
      status: 503,
      body: JSON.stringify({ status: "BLOCKED", error: { code: "BLOCKED_WRONG_CANONICAL_DATABASE", detail: "identity mismatch" } }),
    }),
  });
  await page.goto("/v13/research");
  await expect(page.getByText("Research readiness状态未知", { exact: true })).toBeVisible();
  await expect(page.getByText("TRADING_DISABLED", { exact: true })).toBeVisible();
  await expect(page.getByText("ACTIVE_DEPLOYMENT_UNSET", { exact: true })).toBeVisible();
});

test("unknown enum disables projection actions and renders contract drift", async ({ page }) => {
  await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": { status: "FUTURE_GREEN", items: [] },
  });
  await page.goto("/v13/strategies");
  await expect(page.getByText("Strategy projection 合同漂移", { exact: true })).toBeVisible();
  await expect(page.getByText("UNKNOWN_CONTRACT_VALUE", { exact: true })).toBeVisible();
  await expect(page.locator(".canonical-v13-select-card")).toHaveCount(0);
});
