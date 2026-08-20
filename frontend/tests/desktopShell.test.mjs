import assert from "node:assert/strict";
import test from "node:test";

import {
  advancedNavigationItems,
  advancedNavigationSections,
  isNavigationItemActive,
  navigationItems,
  navigationLabelForPath,
  navigationSections,
} from "../src/layout/navigation.ts";
import { dashboardActivityState, dashboardViewState } from "../src/pages/dashboardState.ts";

test("desktop navigation groups every route once while preserving detail route matching", () => {
  assert.deepEqual(
    navigationSections.map((section) => section.label),
    ["主要任务", "配置与数据", "更多"],
  );
  assert.equal(new Set(navigationItems.map((item) => item.to)).size, navigationItems.length);
  assert.deepEqual(navigationSections.at(-1).items, [{ to: "/advanced", label: "高级入口" }]);
  assert.equal(navigationItems.some((item) => item.to === "/strategies"), false);
  assert.equal(advancedNavigationSections.find((section) => section.kind === "legacy").items.length, 11);
  assert.equal(new Set(advancedNavigationItems.map((item) => item.to)).size, advancedNavigationItems.length);
  assert.equal(navigationLabelForPath("/v13"), "工作台首页");
  assert.equal(navigationLabelForPath("/v13/strategies"), "策略目录");
  assert.equal(isNavigationItemActive("/v13/strategies", { to: "/v13", label: "工作台首页", end: true }), false);
  assert.equal(navigationLabelForPath("/strategies/42"), "Legacy 策略工厂");
  assert.equal(navigationLabelForPath("/advanced"), "高级入口");
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
