import assert from "node:assert/strict";
import test from "node:test";

import {
  isNavigationItemActive,
  navigationItems,
  navigationLabelForPath,
  navigationSections,
} from "../src/layout/navigation.ts";
import { dashboardViewState } from "../src/pages/dashboardState.ts";

test("desktop navigation groups every route once while preserving detail route matching", () => {
  assert.deepEqual(
    navigationSections.map((section) => section.label),
    ["工作台", "研究与验证", "治理与运行"],
  );
  assert.equal(new Set(navigationItems.map((item) => item.to)).size, navigationItems.length);
  assert.equal(navigationLabelForPath("/strategies/42"), "策略");
  assert.equal(navigationLabelForPath("/missing"), "页面未找到");
  assert.equal(isNavigationItemActive("/generation-runs-old", { to: "/generation-runs", label: "生成批次" }), false);
});

test("dashboard never promotes initial zero values to a ready state", () => {
  assert.equal(
    dashboardViewState({ error: null, isLoading: true, source: "failed", visibleRecordCount: 0 }),
    "loading",
  );
  assert.equal(
    dashboardViewState({ error: "API unavailable", isLoading: false, source: "failed", visibleRecordCount: 0 }),
    "failed",
  );
  assert.equal(
    dashboardViewState({ error: null, isLoading: false, source: "api", visibleRecordCount: 0 }),
    "empty",
  );
  assert.equal(
    dashboardViewState({ error: null, isLoading: false, source: "api", visibleRecordCount: 2 }),
    "ready",
  );
});
