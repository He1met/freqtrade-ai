import assert from "node:assert/strict";
import test from "node:test";

import {
  isNavigationItemActive,
  navigationItems,
  navigationLabelForPath,
  navigationSections,
} from "../src/layout/navigation.ts";
import { dashboardActivityState, dashboardViewState } from "../src/pages/dashboardState.ts";

test("desktop navigation groups every route once while preserving detail route matching", () => {
  assert.deepEqual(
    navigationSections.map((section) => section.label),
    ["正式工作台", "开发实验", "高级与历史"],
  );
  assert.equal(new Set(navigationItems.map((item) => item.to)).size, navigationItems.length);
  assert.equal(navigationLabelForPath("/strategies/42"), "策略工厂");
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

test("dashboard activities keep loading failure empty partial and data states distinct", () => {
  assert.equal(dashboardActivityState({ error: null, isLoading: true, visibleRecordCount: 0 }), "loading");
  assert.equal(dashboardActivityState({ error: "runtime unavailable", isLoading: false, visibleRecordCount: 0 }), "failed");
  assert.equal(dashboardActivityState({ error: null, isLoading: false, visibleRecordCount: 0 }), "empty");
  assert.equal(dashboardActivityState({ error: "one source unavailable", isLoading: false, visibleRecordCount: 2 }), "partial");
  assert.equal(dashboardActivityState({ error: null, isLoading: false, visibleRecordCount: 2 }), "ready");
});
