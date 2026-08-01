import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  actionDescriptors,
  auditPrimaryActionSource,
  elementPurposeMatrix,
  primaryActionIdsForRoute,
  routePurposeContracts,
  validateActionDescriptor,
  validateElementPurposeMatrix,
} from "../src/ux/elementPurpose.ts";

test("Issue #556 matrix has one complete contract for every product route", () => {
  assert.deepEqual(validateElementPurposeMatrix(elementPurposeMatrix), []);

  for (const route of routePurposeContracts) {
    const entries = elementPurposeMatrix.filter((item) => item.route === route.route);
    assert.ok(entries.length > 0, `${route.route} must have matrix entries`);
    assert.equal(
      primaryActionIdsForRoute(route.route).length,
      route.default_primary_action_id ? 1 : 0,
      `${route.route} default primary action count`,
    );
    if (route.default_primary_action_id) {
      assert.deepEqual(primaryActionIdsForRoute(route.route), [route.default_primary_action_id]);
    }
  }
});
test("every ActionDescriptor is complete and action labels contain verb plus object", () => {
  for (const descriptor of Object.values(actionDescriptors)) {
    assert.deepEqual(validateActionDescriptor(descriptor), [], descriptor.action_id);
  }
});

test("primary controls cannot enter without a registered data-action-id", () => {
  const sourceFiles = [
    "src/pages/FreqUILink.tsx",
    "src/pages/NotFound.tsx",
    "src/pages/localStrategyLab/CandidateWorkbench.tsx",
    "src/pages/localStrategyLab/DryRunDecisionPanel.tsx",
    "src/pages/localStrategyLab/GenerationStage.tsx",
  ];
  const errors = sourceFiles.flatMap((relativePath) => {
    const source = readFileSync(new URL(`../${relativePath}`, import.meta.url), "utf8");
    return auditPrimaryActionSource(source, relativePath);
  });

  assert.deepEqual(errors, []);
});
