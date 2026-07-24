import assert from "node:assert/strict";
import test from "node:test";

import { operatorDashboardNotice } from "../src/pages/operatorDashboardNotice.ts";

test("operator dashboard does not report an unavailable backend when API data is active", () => {
  const notice = operatorDashboardNotice("api");

  assert.match(notice, /只读运行契约来自 Backend API/);
  assert.match(notice, /不等于运行流程成功/);
  assert.doesNotMatch(notice, /unavailable/i);
});

test("operator dashboard labels an explicit fixture without claiming a backend fallback", () => {
  const notice = operatorDashboardNotice("fixture");

  assert.match(notice, /fixture/);
  assert.match(notice, /不能作为真实运行验收依据/);
  assert.doesNotMatch(notice, /unavailable/i);
});

test("operator dashboard leaves failed requests to the failure notice", () => {
  assert.equal(operatorDashboardNotice("failed"), undefined);
});
