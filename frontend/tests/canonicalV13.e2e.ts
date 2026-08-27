import { expect, test, type Page, type Route } from "@playwright/test";

const ID_A = "123e4567-e89b-42d3-a456-426614174000";
const ID_B = "123e4567-e89b-42d3-a456-426614174001";
const ID_C = "123e4567-e89b-42d3-a456-426614174002";
const ID_D = "123e4567-e89b-42d3-a456-426614174003";
const DIGEST = "a".repeat(64);

function canonicalStrategy(overrides: Record<string, unknown> = {}) {
  return {
    strategy_id: ID_A,
    display_name: "Alpha",
    catalog_status: "DRAFT",
    intake_status: "INTAKE_ACCEPTED",
    current_version_id: ID_B,
    version_number: 1,
    artifact_id: ID_C,
    artifact_digest: DIGEST,
    validation_status: "VALIDATED",
    validation_status_domain: "INTAKE_CODE",
    qualification_status: "NOT_EVALUATED",
    qualification_status_domain: "OOS_PLAN",
    execution_authorized: false,
    created_at: "2026-08-14T00:00:00Z",
    ...overrides,
  };
}

function canonicalResearchChain(overrides: Record<string, unknown> = {}) {
  return {
    validation_plan_id: ID_C,
    validation_plan_digest: DIGEST,
    strategy_version_id: ID_B,
    research_target_id: ID_D,
    target_key: "btc-5m",
    plan_status: "COMPLETE",
    validation_attempt_id: ID_D,
    attempt_status: "SUCCEEDED",
    attempt_receipt_digest: DIGEST,
    target_score_id: ID_C,
    overall_score: "81.00000000",
    score_digest: DIGEST,
    qualification_decision_id: ID_D,
    qualification_status: "REJECTED",
    qualification_reason_code: "REQUIRED_WINDOW_GATE_FAILED",
    qualification_decision_digest: DIGEST,
    ...overrides,
  };
}

function canonicalResearchResults(overrides: Record<string, unknown> = {}) {
  return {
    validation_plan_id: ID_C,
    validation_plan_digest: DIGEST,
    strategy_version_id: ID_B,
    research_target_id: ID_D,
    target_key: "btc-5m",
    configuration_bundle_id: ID_A,
    configuration_bundle_digest: DIGEST,
    market_snapshot_id: ID_B,
    market_snapshot_digest: DIGEST,
    plan_status: "COMPLETE",
    attempt: {
      validation_attempt_id: ID_D,
      attempt_number: 1,
      status: "SUCCEEDED",
      executor_identity: "canonical-research-worker",
      executor_image_digest: DIGEST,
      receipt_digest: DIGEST,
      created_at: "2026-08-14T00:00:00Z",
      completed_at: "2026-08-14T01:00:00Z",
    },
    windows: [{
      validation_plan_window_id: ID_A,
      window_key: "oos-2026-07",
      required: true,
      window_start: "2026-07-01T00:00:00Z",
      window_end: "2026-08-01T00:00:00Z",
      window_member_digest: DIGEST,
      result: {
        validation_window_result_id: ID_B,
        metrics_json: { net_return_after_cost: -0.031, max_drawdown: 0.12, trade_count: 24 },
        metrics_digest: DIGEST,
        receipt_digest: DIGEST,
        created_at: "2026-08-14T01:00:00Z",
      },
      qualification_evidence: {
        qualification_window_evidence_id: ID_C,
        hard_gate_passed: false,
        evidence_digest: DIGEST,
        gates: [{
          gate_key: "positive-return",
          metric: "net_return_after_cost",
          operator: ">",
          threshold: "0",
          observed: "-0.031",
          passed: false,
        }],
      },
    }],
    score: {
      target_score_id: ID_C,
      scoring_snapshot_id: ID_A,
      overall_score: "81.00000000",
      required_window_result_set_digest: DIGEST,
      score_digest: DIGEST,
      scorer_identity: "canonical-scorer",
      created_at: "2026-08-14T01:01:00Z",
    },
    qualification: {
      qualification_decision_id: ID_D,
      target_score_id: ID_C,
      quality_snapshot_id: ID_A,
      status: "REJECTED",
      reason_code: "REQUIRED_WINDOW_GATE_FAILED",
      decision_digest: DIGEST,
      qualifier_identity: "canonical-qualifier",
      evidence_count: 1,
      created_at: "2026-08-14T01:02:00Z",
    },
    ...overrides,
  };
}

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
    profiles: [],
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
  "/api/canonical-v13/research/gates": { status: "EMPTY", items: [] },
  "/api/canonical-v13/research-runs": { status: "EMPTY", items: [] },
  "/api/canonical-v13/research/validation-plans": { status: "EMPTY", items: [] },
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

test("default route opens the V1.3 workbench with project state and one next action", async ({ page }) => {
  const calls = await installCanonicalMocks(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/v13$/);
  await expect(page.getByRole("heading", { level: 1, name: "V1.3 用户工作台" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "尚无 canonical 策略" })).toBeVisible();
  await expect(page.getByRole("link", { name: "提交第一个策略" })).toHaveAttribute("href", "/v13/submission");
  await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name: "工作台首页" })).toBeVisible();
  expect(calls.length).toBeGreaterThan(0);
  expect(calls.every((call) => call.includes("/api/canonical-v13/"))).toBe(true);
});

test("V1.3 workbench keeps the project unknown when a required API projection fails", async ({ page }) => {
  await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": async (route) => route.fulfill({
      contentType: "application/json",
      status: 503,
      body: JSON.stringify({ status: "BLOCKED", error: { code: "CATALOG_UNAVAILABLE", detail: "catalog unavailable" } }),
    }),
  });
  await page.goto("/v13");
  await expect(page.getByRole("heading", { level: 2, name: "项目状态未知" })).toBeVisible();
  await expect(page.getByText("未知原因码", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回工作台核对 API" })).toHaveAttribute("href", "/v13");
  const raw = page.getByRole("group", { name: "原始诊断：CATALOG_UNAVAILABLE" });
  await raw.getByText("查看原始诊断", { exact: true }).click();
  await expect(raw.getByText("CATALOG_UNAVAILABLE", { exact: true })).toBeVisible();
  await expect(page.getByText("研究已就绪", { exact: true })).toHaveCount(0);
});

test("seven canonical routes render true empty, blocked, and pending states without legacy requests", async ({ page }) => {
  const calls = await installCanonicalMocks(page);
  const routes = [
    ["/v13", "V1.3 用户工作台"],
    ["/v13/submission", "策略受控入库"],
    ["/v13/strategies", "策略目录"],
    ["/v13/configuration", "配置中心"],
    ["/v13/market-data", "行情证据"],
    ["/v13/research", "研究与 Runtime 状态"],
    ["/v13/optimization", "优化与回测结果"],
  ] as const;
  for (const [path, heading] of routes) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
  }
  await expect(page.getByText("等待首次回测", { exact: true }).first()).toBeVisible();
  expect(calls.every((call) => call.includes("/api/canonical-v13/"))).toBe(true);
  expect(calls.some((call) => call.includes("/api/v1") || call.includes("/api/strategies"))).toBe(false);
});

test("research page shows offline history without promoting it into qualification", async ({ page }) => {
  const calls = await installCanonicalMocks(page, {
    "/api/canonical-v13/research-runs": {
      status: "AVAILABLE",
      items: [{
        run_id: "cross-asset-crowding-current-main-20260827",
        name: "跨资产拥挤反转 current-main 复跑",
        hypothesis: "严格滞后确认后的衍生品拥挤反转。",
        universe: ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
        timeframe: "15m",
        status: "BOUNDED_NEGATIVE",
        reason_code: "TRAIN_NO_EDGE_STOP_WITHOUT_VALIDATION_PNL",
        dataset_digest: DIGEST,
        artifact_path: "/tmp/research/RESULT.json",
        result_digest: DIGEST,
        train_validation_holdout_summary: {
          train: "NO_EDGE_STOP",
          validation: "NOT_EVALUATED",
          holdout: "SEALED_UNREAD",
        },
        metrics_summary: { finalist_count: 0, f3_pooled_normal_return_pct: -1.414664 },
        created_at: "2026-08-27T02:50:01Z",
      }],
    },
  });
  await page.goto("/v13/research");
  await expect(page.getByRole("heading", { level: 2, name: "历史研究运行" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 3, name: "跨资产拥挤反转 current-main 复跑" })).toBeVisible();
  await expect(page.getByText("有界负结果", { exact: true })).toBeVisible();
  await page.getByText("查看完整研究结果", { exact: true }).click();
  await expect(page.locator(".canonical-v13-research-history-json").first()).toContainText("SEALED_UNREAD");
  await expect(page.getByText("/tmp/research/RESULT.json", { exact: true })).toBeVisible();
  expect(calls).toContain("GET /api/canonical-v13/research-runs");
});

test("default V1.3 pages expose selectors instead of manual internal identity inputs", async ({ page }) => {
  await installCanonicalMocks(page);
  await page.goto("/v13/submission");
  await expect(page.getByLabel("Current version identity", { exact: true })).toHaveCount(0);

  await page.goto("/v13/configuration");
  await expect(page.getByLabel("Scope / Workflow 上下文", { exact: true })).toHaveJSProperty("tagName", "SELECT");
  await expect(page.getByLabel("配置 Profile", { exact: true })).toHaveJSProperty("tagName", "SELECT");

  await page.goto("/v13/research");
  await expect(page.getByLabel("策略", { exact: true })).toHaveJSProperty("tagName", "SELECT");
  await expect(page.getByLabel("Validation plan", { exact: true })).toHaveJSProperty("tagName", "SELECT");

  await page.goto("/v13/optimization");
  await expect(page.getByLabel("策略", { exact: true })).toHaveJSProperty("tagName", "SELECT");

  await page.goto("/v13/market-data");
  await expect(page.getByLabel("已验证 Profile 版本", { exact: true })).toHaveJSProperty("tagName", "SELECT");
  await expect(page.getByLabel("已封存 Profile 快照", { exact: true })).toHaveJSProperty("tagName", "SELECT");
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
    const state = page.locator('.canonical-v13-state[data-state="unknown"]');
    await expect(state).toBeVisible();
    await expect(state.getByRole("list", { name: "阻塞原因与解决建议" })).toBeVisible();
    await expect(state.getByRole("link").first()).toHaveAttribute("href", /^\/v13/);
  }
  await page.waitForTimeout(100);
  expect(calls).toEqual([]);
});

test("research plan URL renders only the canonical chain projection", async ({ page }) => {
  const calls = await installCanonicalMocks(page, {
    [`/api/canonical-v13/research/validation-plans/${ID_A}`]: {
      validation_plan_id: ID_A,
      validation_plan_digest: DIGEST,
      strategy_version_id: ID_B,
      research_target_id: ID_A,
      target_key: "btc-5m",
      plan_status: "COMPLETE",
      validation_attempt_id: ID_B,
      attempt_status: "SUCCEEDED",
      attempt_receipt_digest: DIGEST,
      target_score_id: ID_A,
      overall_score: "99.00000000",
      score_digest: DIGEST,
      qualification_decision_id: ID_B,
      qualification_status: "REJECTED",
      qualification_reason_code: "REQUIRED_WINDOW_GATE_FAILED",
      qualification_decision_digest: DIGEST,
    },
    [`/api/canonical-v13/research/validation-plans/${ID_A}/results`]: canonicalResearchResults({
      validation_plan_id: ID_A,
      research_target_id: ID_A,
    }),
  });
  await page.goto(`/v13/research?plan=${ID_A}`);
  await expect(page.getByRole("heading", { name: "精确研究链路" })).toBeVisible();
  await expect(page.getByText("必需窗口 Gate 未通过", { exact: true }).first()).toBeVisible();
  const reason = page.getByRole("group", { name: "原始诊断：REQUIRED_WINDOW_GATE_FAILED" }).first();
  await reason.getByText("诊断码", { exact: true }).click();
  await expect(reason.getByText("REQUIRED_WINDOW_GATE_FAILED", { exact: true })).toBeVisible();
  await expect(page.getByText("99.00000000", { exact: true })).toBeVisible();
  expect(calls).toContain(`GET /api/canonical-v13/research/validation-plans/${ID_A}`);
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
    validation_status_domain: "INTAKE_CODE",
    qualification_status: "NOT_EVALUATED",
    qualification_status_domain: "OOS_PLAN",
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
  await expect(page.getByRole("heading", { level: 2, name: "Alpha", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { level: 2, name: "Alpha", exact: true })).toBeVisible();
  await page.goBack();
  await expect(page).not.toHaveURL(/strategy=/);
  await expect(page.getByText("尚未选择策略")).toBeVisible();
});

test("390px strategy catalog preserves a complete copyable long name without document overflow", async ({ page }) => {
  const longName = "OkxDemoFirstRealCandidateMeanReversionV2ExactCanonicalName";
  const strategy = canonicalStrategy({ display_name: longName });
  await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": { status: "AVAILABLE", items: [strategy] },
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/v13/strategies");
  await expect(page.getByText(longName, { exact: true })).toHaveText(longName);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("configuration context profile and version use API selectors across history and refresh", async ({ page }) => {
  const configuration = {
    status: "AVAILABLE",
    configured_kinds: ["TARGET"],
    unset_kinds: ["WINDOW", "GENERATION", "DIVERSITY", "QUALITY_QUALIFICATION", "SCORING", "RESEARCH_AGGREGATE"],
    items: [{
      profile_id: ID_A,
      profile_key: "primary-targets",
      configuration_kind: "TARGET",
      scope_key: "research",
      workflow_key: "canonical",
      versions: [{
        version_id: ID_B,
        version_number: 2,
        lifecycle_status: "VALIDATED",
        schema_json: {},
        payload_json: { source: "api" },
        schema_digest: DIGEST,
        payload_digest: DIGEST,
        adapter_identity: "canonical-target-adapter",
        adapter_digest: DIGEST,
        snapshot_id: ID_C,
        snapshot_digest: DIGEST,
        active_in_bundle: true,
        active_activation_id: ID_C,
        active_bundle_id: ID_D,
        active_bundle_digest: DIGEST,
        created_at: "2026-08-14T00:00:00Z",
        validated_at: "2026-08-14T01:00:00Z",
      }],
    }],
  };
  await installCanonicalMocks(page, { "/api/canonical-v13/configurations": configuration });
  await page.goto("/v13/configuration");

  await expect(page).toHaveURL(/scope=research&workflow=canonical/);
  await expect(page.getByRole("heading", { level: 2, name: "当前默认配置" })).toBeVisible();
  await expect(page.getByText("版本 2 · 当前生效", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "查看完整配置" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "primary-targets · 版本 2" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 3, name: "具体配置" })).toBeVisible();
  await expect(page.getByText("source", { exact: true })).toBeVisible();
  await expect(page.getByText("api", { exact: true })).toBeVisible();
  await page.getByText("高级标识与不可变摘要", { exact: true }).click();
  await expect(page.getByText("Schema digest", { exact: true })).toBeVisible();
  await expect(page.getByText("Activation", { exact: true })).toBeVisible();
  await expect(page.getByText("Active Bundle digest", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`profile=${ID_A}.*version=${ID_B}`));

  await page.reload();
  await expect(page.getByRole("heading", { level: 2, name: "primary-targets · 版本 2" })).toBeVisible();
  await page.goBack();
  await expect(page).not.toHaveURL(/version=/);
  await expect(page.getByText("尚未选择具体配置", { exact: true })).toBeVisible();
});

test("configuration defaults show seven exact active members while latest stays historical", async ({ page }) => {
  const kinds = ["TARGET", "WINDOW", "GENERATION", "DIVERSITY", "QUALITY_QUALIFICATION", "SCORING", "RESEARCH_AGGREGATE"];
  const activationId = "1dab142c-2c8d-440d-8308-f9cc53d12afd";
  const bundleId = "ac3b556b-3bbe-5892-bf37-ab84949e7139";
  const bundleDigest = "359a8baed8e1e4780b6ee15bd9ae021cbb286c9ddb5caf749049746fa7657682";
  const stableId = (value: number) => `123e4567-e89b-42d3-a456-${String(value).padStart(12, "0")}`;
  const version = (profileIndex: number, number: number, active: boolean) => ({
    version_id: stableId(100 + profileIndex * 10 + number),
    version_number: number,
    lifecycle_status: "VALIDATED",
    schema_json: { type: "object", additionalProperties: false },
    payload_json: { canonical_value: `${profileIndex}:${number}` },
    schema_digest: DIGEST,
    payload_digest: DIGEST,
    adapter_identity: "canonical-config-adapter",
    adapter_digest: DIGEST,
    snapshot_id: stableId(300 + profileIndex * 10 + number),
    snapshot_digest: DIGEST,
    active_in_bundle: active,
    active_activation_id: active ? activationId : null,
    active_bundle_id: active ? bundleId : null,
    active_bundle_digest: active ? bundleDigest : null,
    created_at: `2026-08-${String(10 + number).padStart(2, "0")}T00:00:00Z`,
    validated_at: `2026-08-${String(10 + number).padStart(2, "0")}T01:00:00Z`,
  });
  const items = kinds.map((kind, index) => ({
    profile_id: stableId(index + 1),
    profile_key: `profile-${kind.toLowerCase()}`,
    configuration_kind: kind,
    scope_key: "research",
    workflow_key: "canonical",
    versions: index === 0 ? [version(index, 1, true), version(index, 2, false)] : [version(index, 1, true)],
  }));
  await installCanonicalMocks(page, {
    "/api/canonical-v13/configurations": { status: "AVAILABLE", configured_kinds: kinds, unset_kinds: [], items },
  });
  await page.goto("/v13/configuration");

  await expect(page.locator('section[aria-label="当前生效配置"] article')).toHaveCount(7);
  await expect(page.getByText("版本 1 · 当前生效", { exact: true })).toHaveCount(7);
  await page.locator('section[aria-label="当前生效配置"] article').first().getByRole("button", { name: "查看完整配置" }).click();
  await expect(page.getByText("这是当前 Bundle 正在使用的配置。", { exact: true })).toBeVisible();
  await page.getByLabel("配置版本", { exact: true }).selectOption(version(0, 2, false).version_id);
  await expect(page.getByText("这是该 Profile 的最新历史版本，但当前 Bundle 未使用该版本。", { exact: true })).toBeVisible();
  await page.getByText("原始 JSON 与 Schema", { exact: true }).click();
  await expect(page.getByRole("heading", { name: "Payload JSON" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Schema JSON" })).toBeVisible();
});

test("market inventory distinguishes versions snapshots and uses numeric version ordering", async ({ page }) => {
  const marketVersion = (number: number, id: string) => ({
    market_profile_id: ID_A,
    profile_key: "canonical-market",
    scope_key: "research",
    version_id: id,
    version_number: number,
    lifecycle_status: "VALIDATED",
    payload_digest: DIGEST,
    created_at: "2026-08-14T00:00:00Z",
    validated_at: "2026-08-14T01:00:00Z",
  });
  await installCanonicalMocks(page, {
    "/api/canonical-v13/market-data": {
      status: "AVAILABLE",
      profile_count: 1,
      validated_profile_count: 3,
      artifact_count: 12,
      accepted_receipt_count: 12,
      profiles: [marketVersion(10, ID_C), marketVersion(2, ID_B), marketVersion(1, ID_A)],
      snapshots: [],
    },
  });
  await page.goto("/v13/market-data");
  await expect(page.getByText("行情 Profiles", { exact: true })).toBeVisible();
  await expect(page.getByText("已验证 Profile 版本", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("已封存 Profile 快照", { exact: true })).toBeVisible();
  const labels = await page.getByLabel("已验证 Profile 版本", { exact: true }).locator("option").allTextContents();
  expect(labels.filter((label) => label.includes("canonical-market"))).toEqual([
    "canonical-market · 版本 1 · 已验证 · Scope research",
    "canonical-market · 版本 2 · 已验证 · Scope research",
    "canonical-market · 版本 10 · 已验证 · Scope research",
  ]);
});

test("blocked optimization renders canonical reasons and persisted summary counts", async ({ page }) => {
  await installCanonicalMocks(page, {
    "/api/canonical-v13/optimizations": {
      status: "AVAILABLE",
      items: [{
        optimization_run_id: ID_A,
        baseline_qualification_decision_id: ID_B,
        status: "BLOCKED",
        request_digest: DIGEST,
        receipt_digest: DIGEST,
        terminal_reason_codes: ["ZERO_TRAIN_VALIDATION_ELIGIBLE_FINALISTS"],
        trial_count: 96,
        result_count: 96,
        submitted_strategy_count: 0,
        result_digest: DIGEST,
        created_at: "2026-08-14T00:00:00Z",
        completed_at: "2026-08-14T01:00:00Z",
      }],
    },
  });
  await page.goto("/v13/optimization");
  await expect(page.getByText("Trials 96 · Results 96 · 已提交策略 0", { exact: true })).toBeVisible();
  await expect(page.getByText("优化未产生可提交结果", { exact: true })).toBeVisible();
  await page.getByText("高级优化标识", { exact: true }).click();
  await expect(page.getByRole("button", { name: "复制Terminal result digest" })).toBeVisible();
});

test("research selectors preserve API IDs while showing names and exact lineage", async ({ page }) => {
  const alpha = canonicalStrategy();
  const plan = canonicalResearchChain();
  await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": { status: "AVAILABLE", items: [alpha] },
    [`/api/canonical-v13/strategies/${ID_A}`]: alpha,
    "/api/canonical-v13/research/validation-plans": { status: "AVAILABLE", items: [plan] },
    [`/api/canonical-v13/research/validation-plans/${ID_C}`]: plan,
    [`/api/canonical-v13/research/validation-plans/${ID_C}/results`]: canonicalResearchResults(),
  });
  await page.goto("/v13/research");

  await page.getByLabel("策略", { exact: true }).selectOption(ID_A);
  await expect(page).toHaveURL(new RegExp(`strategy=${ID_A}`));
  await page.getByLabel("研究目标", { exact: true }).selectOption(ID_D);
  await page.getByLabel("Validation plan", { exact: true }).selectOption(ID_C);
  await expect(page).toHaveURL(new RegExp(`target=${ID_D}.*strategy=${ID_A}.*plan=${ID_C}`));
  await expect(page.getByRole("heading", { level: 2, name: "Alpha 的研究流程" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "精确研究链路" })).toBeVisible();
  await expect(page.getByLabel("策略", { exact: true })).toHaveValue(ID_A);
});

test("qualified exact results expose read-only Phase 9 A-D receipts without execution controls", async ({ page }) => {
  const alpha = canonicalStrategy({ validation_status: "UNVALIDATED", qualification_status: "QUALIFIED" });
  const plan = canonicalResearchChain({ qualification_status: "QUALIFIED", qualification_reason_code: "QUALIFIED" });
  const results = canonicalResearchResults({
    qualification: {
      qualification_decision_id: ID_D,
      target_score_id: ID_C,
      quality_snapshot_id: ID_A,
      status: "QUALIFIED",
      reason_code: "QUALIFIED",
      decision_digest: DIGEST,
      qualifier_identity: "canonical-qualifier",
      evidence_count: 1,
      created_at: "2026-08-21T00:00:00Z",
    },
  });
  const calls = await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": { status: "AVAILABLE", items: [alpha] },
    [`/api/canonical-v13/strategies/${ID_A}`]: alpha,
    "/api/canonical-v13/research/validation-plans": { status: "AVAILABLE", items: [plan] },
    [`/api/canonical-v13/research/validation-plans/${ID_C}`]: plan,
    [`/api/canonical-v13/research/validation-plans/${ID_C}/results`]: results,
    "/api/canonical-v13/phase9/readiness": async (route) => {
      const stage = new URL(route.request().url()).searchParams.get("stage");
      await route.fulfill({
        contentType: "application/json",
        status: 200,
        body: JSON.stringify({
          contract: "canonical-v13-phase9-readiness-receipt-v2",
          stage,
          status: stage === "QUALIFICATION_HANDOFF" ? "READY" : "BLOCKED",
          reason_codes: stage === "QUALIFICATION_HANDOFF" ? [] : [
            stage === "NO_ORDER_SOAK" ? "RUNTIME_NOT_HEALTHY"
              : stage === "SIGNAL_RISK_SHADOW" ? "EXACT_HEALTHY_LONG_LIVED_RUNTIME_EVIDENCE_UNSET"
                : stage === "OKX_DEMO_CANARY" ? "EXACT_SINGLE_CANARY_ORDER_COUNT_REQUIRED"
                  : "EXACT_RECOVERY_SOAK_ACCEPTANCE_UNSET",
          ],
          qualification_status_counts: { QUALIFIED: 1 },
          execution_domain_counts: { orders: 0, fills: 0, ledger_entries: 0, reconciliation_runs: 0 },
          lineage_evidence_counts: {},
          handoff: {
            qualification_decision_id: ID_D,
            qualification_decision_digest: DIGEST,
            strategy_version_id: ID_B,
            research_target_id: ID_D,
            configuration_bundle_id: ID_A,
            configuration_bundle_digest: DIGEST,
            market_snapshot_id: ID_B,
            market_snapshot_digest: DIGEST,
            validation_plan_id: ID_C,
            validation_plan_digest: DIGEST,
          },
          topology_digest: DIGEST,
          receipt_digest: DIGEST,
        }),
      });
    },
  });
  await page.goto(`/v13/research?strategy=${ID_A}&target=${ID_D}&plan=${ID_C}`);
  await expect(page.getByRole("heading", { level: 2, name: "Phase 9 分段验收" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 3, name: "A · 无订单运行" })).toBeVisible();
  await expect(page.getByText("入库代码验证", { exact: true })).toBeVisible();
  await expect(page.getByText("OOS 资格决策", { exact: true })).toBeVisible();
  await expect(page.getByText("Runtime 未处于健康运行状态", { exact: true })).toBeVisible();
  await expect(page.getByText("缺少精确的长期 Runtime 健康证据", { exact: true })).toBeVisible();
  await expect(page.getByText("尚未满足唯一 Canary 订单计数", { exact: true })).toBeVisible();
  await expect(page.getByText("恢复 Soak 尚未验收", { exact: true })).toBeVisible();
  await expect(page.getByText("本页面不提供 deployment、runtime 或 order 写入控制。")).toBeVisible();
  await expect(page.getByRole("button", { name: /order|deployment|runtime/i })).toHaveCount(0);
  const phase9Calls = calls.filter((call) => call.includes("/phase9/readiness?"));
  // React StrictMode intentionally replays effects in the development E2E server.
  // The contract is five distinct stage requests, with no hidden sixth stage.
  expect(new Set(phase9Calls).size).toBe(5);
  expect(phase9Calls.length).toBeGreaterThanOrEqual(5);
});

test("stale and failed selector projections remain explicit and never choose the first option", async ({ page }) => {
  const configuration = {
    status: "AVAILABLE",
    configured_kinds: ["TARGET"],
    unset_kinds: [],
    items: [{
      profile_id: ID_A,
      profile_key: "only-api-profile",
      configuration_kind: "TARGET",
      scope_key: "research",
      workflow_key: "canonical",
      versions: [],
    }],
  };
  await installCanonicalMocks(page, { "/api/canonical-v13/configurations": configuration });
  await page.goto(`/v13/configuration?scope=research&workflow=canonical&profile=${ID_C}`);
  await expect(page.getByText("所选配置 Profile 不存在", { exact: true })).toBeVisible();
  await expect(page.getByText("当前 URL 中的对象不在最新 API 选项内；页面不会自动改选第一项。", { exact: true })).toBeVisible();
  await expect(page.getByLabel("配置 Profile", { exact: true })).toHaveValue("");

  await page.unrouteAll({ behavior: "wait" });
  await installCanonicalMocks(page, {
    "/api/canonical-v13/configurations": async (route) => route.fulfill({
      contentType: "application/json",
      status: 403,
      body: JSON.stringify({ status: "BLOCKED", error: { code: "CONFIGURATION_READ_FORBIDDEN", detail: "permission denied" } }),
    }),
  });
  await page.goto("/v13/configuration");
  await expect(page.getByText("配置选择器暂不可用", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Scope / Workflow 上下文", { exact: true })).toBeDisabled();
  await expect(page.getByText("未知原因码", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回工作台核对 API" })).toHaveAttribute("href", "/v13");
});

test("the default journey reaches a target strategy research state in three clicks", async ({ page }) => {
  const alpha = canonicalStrategy();
  await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": { status: "AVAILABLE", items: [alpha] },
    [`/api/canonical-v13/strategies/${ID_A}`]: alpha,
  });

  await page.goto("/");
  await page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name: "策略目录" }).click();
  await page.getByRole("button", { name: /Alpha/ }).click();
  await page.locator(".canonical-v13-workflow-research-link").click();

  await expect(page).toHaveURL(new RegExp(`/v13/research\\?strategy=${ID_A}$`));
  await expect(page.getByRole("heading", { level: 2, name: "Alpha 的研究流程" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Alpha 的策略研究流程" })).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(new RegExp(`/v13/research\\?strategy=${ID_A}$`));
  await expect(page.getByRole("heading", { level: 2, name: "Alpha 的研究流程" })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/v13/strategies\\?strategy=${ID_A}$`));
  await expect(page.getByRole("heading", { level: 2, name: "Alpha", exact: true })).toBeVisible();
});

test("strategy context links to exact API backtest metrics and qualification evidence", async ({ page }) => {
  const alpha = canonicalStrategy({ qualification_status: "REJECTED" });
  const plan = canonicalResearchChain();
  await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": { status: "AVAILABLE", items: [alpha] },
    [`/api/canonical-v13/strategies/${ID_A}`]: alpha,
    "/api/canonical-v13/research/validation-plans": { status: "AVAILABLE", items: [plan] },
    [`/api/canonical-v13/research/validation-plans/${ID_C}`]: plan,
    [`/api/canonical-v13/research/validation-plans/${ID_C}/results`]: canonicalResearchResults(),
  });
  await page.goto(`/v13/strategies?strategy=${ID_A}`);
  await page.getByRole("link", { name: /查看 exact 回测证据/ }).click();

  await expect(page.getByRole("heading", { level: 2, name: "回测与资格结果" })).toBeVisible();
  await expect(page.getByRole("meter", { name: "API Overall score" })).toHaveAttribute("aria-valuenow", "81");
  await expect(page.getByRole("region", { name: "窗口指标对比" })).toContainText("-0.031");
  await expect(page.getByText("Hard Gate 未通过", { exact: true })).toBeVisible();
  await expect(page.getByText("net_return_after_cost: -0.031 > 0", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "回测与资格结果" }).getByText("必需窗口 Gate 未通过", { exact: true })).toBeVisible();
  await expect(page.getByText("已合格", { exact: true })).toHaveCount(0);
  await expect(page.getByText("研究已就绪", { exact: true })).toHaveCount(0);
});

test("an exact strategy and plan URL renders five API-derived research steps", async ({ page }) => {
  const alpha = canonicalStrategy({ qualification_status: "REJECTED" });
  await installCanonicalMocks(page, {
    [`/api/canonical-v13/strategies/${ID_A}`]: alpha,
    [`/api/canonical-v13/research/validation-plans/${ID_C}`]: canonicalResearchChain(),
    [`/api/canonical-v13/research/validation-plans/${ID_C}/results`]: canonicalResearchResults(),
  });

  await page.goto(`/v13/research?strategy=${ID_A}&plan=${ID_C}`);
  const workflow = page.getByRole("navigation", { name: "Alpha 的策略研究流程" });
  await expect(workflow.locator("li")).toHaveCount(5);
  await expect(workflow.locator('[data-step-state="complete"]')).toHaveCount(4);
  await expect(workflow.locator('[data-step-state="blocked"]')).toHaveCount(1);
  await expect(workflow.locator('[aria-current="step"]')).toContainText("资格决策");
  await workflow.getByText("高级诊断", { exact: true }).first().click();
  await expect(workflow.getByText("INTAKE_ACCEPTED", { exact: true }).last()).toBeVisible();
});

test("lineage mismatch leaves all later research steps unknown", async ({ page }) => {
  const alpha = canonicalStrategy();
  await installCanonicalMocks(page, {
    [`/api/canonical-v13/strategies/${ID_A}`]: alpha,
    [`/api/canonical-v13/research/validation-plans/${ID_C}`]: canonicalResearchChain({ strategy_version_id: ID_A }),
    [`/api/canonical-v13/research/validation-plans/${ID_C}/results`]: canonicalResearchResults({ strategy_version_id: ID_A }),
  });

  await page.goto(`/v13/research?strategy=${ID_A}&plan=${ID_C}`);
  const workflow = page.getByRole("navigation", { name: "Alpha 的策略研究流程" });
  await expect(workflow.locator('[data-step-state="complete"]')).toHaveCount(2);
  await expect(workflow.locator('[data-step-state="unknown"]')).toHaveCount(3);
  await expect(workflow.getByText("策略与研究计划 Lineage 不一致", { exact: true })).toHaveCount(3);
  await expect(workflow.locator('li:nth-child(n+3)[data-step-state="complete"]')).toHaveCount(0);
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
  await expect(page.getByText("研究准备状态未知", { exact: true })).toBeVisible();
  await expect(page.getByText("Canonical 数据库身份不匹配", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回工作台核对服务" }).first()).toBeVisible();
  await expect(page.getByText("交易能力已明确禁用", { exact: true })).toBeVisible();
  await expect(page.getByText("尚无启用中的部署", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "查看 Runtime 诊断" }).first()).toHaveAttribute("href", "/v13/research");

  const raw = page.getByRole("group", { name: "原始诊断：TRADING_DISABLED" });
  await expect(raw).not.toHaveAttribute("open", "");
  await raw.getByText("查看原始诊断", { exact: true }).click();
  await expect(raw.getByText("TRADING_DISABLED", { exact: true })).toBeVisible();
});

test("unknown enum disables projection actions and renders contract drift", async ({ page }) => {
  await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": { status: "FUTURE_GREEN", items: [] },
  });
  await page.goto("/v13/strategies");
  await expect(page.getByText("策略目录合同漂移", { exact: true })).toBeVisible();
  await expect(page.getByText("接口合同无法识别", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回工作台核对 API" })).toHaveAttribute("href", "/v13");
  const raw = page.getByRole("group", { name: "原始诊断：UNKNOWN_CONTRACT_VALUE" });
  await raw.getByText("查看原始诊断", { exact: true }).click();
  await expect(raw.getByText("UNKNOWN_CONTRACT_VALUE", { exact: true })).toBeVisible();
  await expect(page.locator(".canonical-v13-select-card")).toHaveCount(0);
});

test("partial research evidence stays explicit and never invents zero values or a decision", async ({ page }) => {
  const partial = canonicalResearchResults({
    score: null,
    qualification: null,
    windows: [{
      validation_plan_window_id: ID_A,
      window_key: "oos-awaiting-result",
      required: true,
      window_start: "2026-07-01T00:00:00Z",
      window_end: "2026-08-01T00:00:00Z",
      window_member_digest: DIGEST,
      result: null,
      qualification_evidence: null,
    }],
  });
  await installCanonicalMocks(page, {
    [`/api/canonical-v13/research/validation-plans/${ID_C}`]: canonicalResearchChain({
      target_score_id: null,
      overall_score: null,
      score_digest: null,
      qualification_decision_id: null,
      qualification_status: null,
      qualification_reason_code: null,
      qualification_decision_digest: null,
    }),
    [`/api/canonical-v13/research/validation-plans/${ID_C}/results`]: partial,
  });

  await page.goto(`/v13/research?plan=${ID_C}`);
  const results = page.getByRole("region", { name: "回测与资格结果" });
  await expect(results.getByText("未提供", { exact: true })).toBeVisible();
  await expect(results.getByText("尚无决策", { exact: true })).toBeVisible();
  await expect(results.getByText("API 未提供窗口结果", { exact: true })).toBeVisible();
  await expect(results.getByText("Canonical API 未返回该窗口的 persisted result；不显示零值或 fallback。", { exact: true })).toBeVisible();
  await expect(results.getByRole("meter")).toHaveCount(0);
  await expect(results.getByText("已合格", { exact: true })).toHaveCount(0);
});

test("mobile workbench and exact research flow fit a 390px viewport with touch targets", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const alpha = canonicalStrategy({ qualification_status: "REJECTED" });
  await installCanonicalMocks(page, {
    "/api/canonical-v13/strategies": { status: "AVAILABLE", items: [alpha] },
    [`/api/canonical-v13/strategies/${ID_A}`]: alpha,
    "/api/canonical-v13/research/validation-plans": { status: "AVAILABLE", items: [canonicalResearchChain()] },
    [`/api/canonical-v13/research/validation-plans/${ID_C}`]: canonicalResearchChain(),
    [`/api/canonical-v13/research/validation-plans/${ID_C}/results`]: canonicalResearchResults(),
  });

  await page.goto("/v13");
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeHidden();
  const mobileMenu = page.getByRole("group").filter({ has: page.getByLabel(/打开主导航/) });
  await page.getByLabel(/打开主导航/).click();
  await expect(page.getByRole("navigation", { name: "移动端主导航" })).toBeVisible();
  await page.getByRole("navigation", { name: "移动端主导航" }).getByRole("link", { name: "策略目录" }).click();
  await page.getByRole("button", { name: /Alpha/ }).click();
  await page.getByRole("link", { name: /查看 exact 回测证据/ }).click();
  await expect(page.getByRole("heading", { level: 2, name: "回测与资格结果" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Alpha 的策略研究流程" }).locator("li")).toHaveCount(5);
  const viewportWidth = await page.evaluate(() => document.documentElement.clientWidth);
  const pageWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(pageWidth).toBe(viewportWidth);

  const targetHeights = await page.locator(".mobile-nav summary, .canonical-v13-workflow-research-link, .canonical-v13-search-select select").evaluateAll((elements) =>
    elements.filter((element) => (element as HTMLElement).offsetParent !== null).map((element) => Math.round(element.getBoundingClientRect().height)),
  );
  expect(targetHeights.length).toBeGreaterThan(0);
  expect(targetHeights.every((height) => height >= 48)).toBe(true);
  await expect(mobileMenu).toHaveCount(1);
});

test("keyboard users can skip navigation, see focus, and open the mobile menu", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installCanonicalMocks(page);
  await page.goto("/v13");

  await expect(page.locator("#main-content")).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  const menuSummary = page.getByLabel(/打开主导航/);
  await expect(menuSummary).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("link", { name: "跳到主要内容" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  await page.keyboard.press("Shift+Tab");
  await expect(menuSummary).toBeFocused();
  const focusOutline = await menuSummary.evaluate((element) => getComputedStyle(element).outlineStyle);
  expect(focusOutline).not.toBe("none");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("navigation", { name: "移动端主导航" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("navigation", { name: "移动端主导航" })).toBeHidden();
});

test("canonical pages expose unique IDs, labelled controls, live selector counts, and alert errors", async ({ page }) => {
  await installCanonicalMocks(page, {
    "/api/canonical-v13/configurations": async (route) => route.fulfill({
      contentType: "application/json",
      status: 503,
      body: JSON.stringify({ status: "BLOCKED", error: { code: "CONFIGURATION_API_UNAVAILABLE", detail: "unavailable" } }),
    }),
  });
  await page.goto("/v13/configuration");

  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  await expect(page.getByRole("alert")).toContainText("配置选择器暂不可用");
  await expect(page.locator('[aria-live="polite"]')).toHaveCount(3);
  const audit = await page.evaluate(() => {
    const ids = [...document.querySelectorAll<HTMLElement>("[id]")].map((element) => element.id).filter(Boolean);
    const duplicates = ids.filter((id, index) => ids.indexOf(id) !== index);
    const unnamed = [...document.querySelectorAll<HTMLElement>("input, select, button, a[href], summary")]
      .filter((element) => element.offsetParent !== null)
      .filter((element) => {
        const id = element.id;
        const labelled = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
        return !(element.getAttribute("aria-label") || element.getAttribute("aria-labelledby") || labelled || element.closest("label") || element.textContent?.trim() || element.getAttribute("title"));
      })
      .map((element) => element.outerHTML);
    return { duplicates: [...new Set(duplicates)], unnamed };
  });
  expect(audit).toEqual({ duplicates: [], unnamed: [] });
});

test("default navigation exposes one advanced entry while preserved legacy routes keep a clear boundary", async ({ page }) => {
  const calls = await installCanonicalMocks(page);
  await page.goto("/v13");
  const navigation = page.getByRole("navigation", { name: "主导航" });
  await expect(navigation.getByRole("link", { name: "高级入口" })).toBeVisible();
  await expect(navigation.getByRole("link", { name: /Legacy|生成批次|回测批次|Local Strategy Lab/ })).toHaveCount(0);

  await navigation.getByRole("link", { name: "高级入口" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "高级入口" })).toBeVisible();
  await expect(page.getByRole("note")).toContainText("V1.3 页面只读取 /api/canonical-v13");
  await expect(page.getByRole("link", { name: /Legacy 总览/ })).toHaveAttribute("href", "/legacy/dashboard");
  expect(calls.every((call) => call.includes("/api/canonical-v13/"))).toBe(true);
  await page.getByRole("link", { name: /Legacy 总览/ }).click();
  await expect(page.getByText("高级与历史证据", { exact: true })).toBeVisible();
  await expect(page.getByText(/不是 V1.3 canonical production 权威/)).toBeVisible();
  await expect(page.getByRole("link", { name: "返回 V1.3 工作台" })).toHaveAttribute("href", "/v13");
});
