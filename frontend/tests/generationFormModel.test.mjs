import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizePresenceSource,
  normalizeReportsEnvValues,
  normalizeValueRendered,
} from "../src/api/operatorPresenceContract.ts";
import { mockMvpData } from "../src/data/mock.ts";
import {
  deriveProviderCredentialReadiness,
  generationFormModel,
} from "../src/pages/localStrategyLab/generationFormModel.ts";

function dashboardWithCredential(entry) {
  const dashboard = structuredClone(mockMvpData.operatorDashboard);
  dashboard.operatorStatus.envPresence = entry ? [entry] : [];
  return dashboard;
}

test("Provider readiness only trusts presence metadata from the real API", () => {
  const present = deriveProviderCredentialReadiness(
    dashboardWithCredential({
      name: "DEEPSEEK_API_KEY",
      present: true,
      source: "env",
      valueRendered: false,
    }),
    "api",
  );
  assert.equal(present.state, "ready");
  assert.match(present.detail, /仅确认凭据存在/);
  assert.doesNotMatch(present.detail, /sk-[A-Za-z0-9]/);

  for (const source of ["fixture", "failed"]) {
    const fallback = deriveProviderCredentialReadiness(
      dashboardWithCredential({
        name: "DEEPSEEK_API_KEY",
        present: true,
        source: "env",
        valueRendered: false,
      }),
      source,
    );
    assert.equal(fallback.state, "unknown");
    assert.match(fallback.label, /未由真实 API 确认/);
  }

  const leaked = deriveProviderCredentialReadiness(
    dashboardWithCredential({
      name: "DEEPSEEK_API_KEY",
      present: true,
      source: "env",
      valueRendered: true,
    }),
    "api",
  );
  assert.equal(leaked.state, "unknown");
});

test("malformed or fixture-like credential metadata never becomes ready", () => {
  for (const entry of [
    { name: "DEEPSEEK_API_KEY", present: true, valueRendered: false },
    { name: "DEEPSEEK_API_KEY", present: true, source: "fixture", valueRendered: false },
    { name: "DEEPSEEK_API_KEY", present: true, source: "env" },
  ]) {
    const readiness = deriveProviderCredentialReadiness(
      dashboardWithCredential(entry),
      "api",
    );
    assert.equal(readiness.state, "unknown");
  }

  const unsafeDashboard = dashboardWithCredential({
    name: "DEEPSEEK_API_KEY",
    present: true,
    source: "env",
    valueRendered: false,
  });
  unsafeDashboard.operatorStatus.safety.reportsEnvValues = true;
  assert.equal(
    deriveProviderCredentialReadiness(unsafeDashboard, "api").state,
    "unknown",
  );
});

test("normalizer helpers preserve missing credential safety metadata as fail-closed", () => {
  assert.equal(normalizePresenceSource(undefined), "unknown");
  assert.equal(normalizePresenceSource("fixture"), "fixture");
  assert.equal(normalizeValueRendered(undefined, undefined), true);
  assert.equal(normalizeValueRendered(false, undefined), false);
  assert.equal(normalizeValueRendered(true, false), true);
  assert.equal(normalizeReportsEnvValues(undefined, undefined), true);
  assert.equal(normalizeReportsEnvValues(undefined, false), false);
  assert.equal(normalizeReportsEnvValues(true, false), true);
});

test("missing Provider credential is distinct from the local operator token", () => {
  const providerReadiness = deriveProviderCredentialReadiness(
    dashboardWithCredential({
      name: "DEEPSEEK_API_KEY",
      present: false,
      source: "env",
      valueRendered: false,
    }),
    "api",
  );
  const ordinarySubmission = generationFormModel({
    authorizeRealProvider: false,
    idea: "RSI 入场，均线退出，单笔风险 1%，15m。",
    isSubmitting: false,
    operatorTokenPresent: true,
    providerReadiness,
  });

  assert.equal(providerReadiness.state, "missing");
  assert.equal(ordinarySubmission.canSubmit, true);
  assert.equal(ordinarySubmission.providerCallLabel, "未授权真实 Provider 调用");

  const realProviderSubmission = generationFormModel({
    authorizeRealProvider: true,
    idea: "RSI 入场，均线退出，单笔风险 1%，15m。",
    isSubmitting: false,
    operatorTokenPresent: true,
    providerReadiness,
  });
  assert.equal(realProviderSubmission.canSubmit, false);
  assert.match(realProviderSubmission.disabledReasons.join(" "), /Provider 凭据未就绪/);
});

test("disabled submit reasons are concrete and requested_count remains fixed", () => {
  const model = generationFormModel({
    authorizeRealProvider: false,
    idea: " ",
    isSubmitting: false,
    operatorTokenPresent: false,
    providerReadiness: {
      state: "unknown",
      label: "未确认",
      detail: "未确认",
    },
  });

  assert.equal(model.canSubmit, false);
  assert.equal(model.requestedCount, 1);
  assert.equal(model.disabledReasons.length, 2);
  assert.match(model.disabledReasons[0], /填写策略构想/);
  assert.match(model.disabledReasons[1], /operator token/);
});

test("submitting state blocks another request and keeps authorization semantics explicit", () => {
  const model = generationFormModel({
    authorizeRealProvider: true,
    idea: "RSI 入场，均线退出。",
    isSubmitting: true,
    operatorTokenPresent: true,
    providerReadiness: {
      state: "ready",
      label: "已就绪",
      detail: "只确认存在。",
    },
  });

  assert.equal(model.canSubmit, false);
  assert.equal(model.providerCallLabel, "仅授权下一次提交");
  assert.match(model.disabledReasons.join(" "), /正在提交/);
});
