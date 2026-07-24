import assert from "node:assert/strict";
import test from "node:test";

import {
  dryRunDisplayConclusion,
  redactCommandArgs,
  redactFreqUiText,
  redactFreqUiUrl,
  safeFreqUiLink,
  safetyBoundarySummary,
} from "../src/pages/freqUiDisplay.ts";

function snapshot(overrides = {}) {
  return {
    status: "SUCCESS",
    profileName: "local",
    strategyVersionId: 7,
    strategyName: "Strategy",
    exchange: "okx",
    pair: "BTC/USDT",
    timeframe: "15m",
    dryRun: true,
    balanceSummary: {
      currency: "USDT",
      total: 100,
      free: 90,
      used: 10,
      realizedProfit: 1,
      unrealizedProfit: 2,
    },
    openTradesSummary: {
      totalOpenTrades: 0,
      pairCount: 0,
      pairs: [],
      totalStakeAmount: null,
      totalProfitAbs: null,
      totalProfitPct: null,
    },
    recentEvents: [],
    blockedReason: null,
    failedReason: null,
    skippedReason: null,
    lastUpdated: "2026-07-24T01:00:00Z",
    artifactManifestPath: "/reports/status.json",
    ...overrides,
  };
}

function conclusion(overrides = {}) {
  return dryRunDisplayConclusion({
    error: null,
    isLoading: false,
    manifest: null,
    snapshot: snapshot(),
    source: "api",
    ...overrides,
  });
}

test("real empty, not loaded, failed and normal states remain distinct", () => {
  assert.equal(conclusion().state, "REAL_EMPTY");
  assert.equal(conclusion({ snapshot: snapshot({ lastUpdated: null }) }).state, "NOT_LOADED");
  assert.equal(conclusion({ error: "request failed" }).state, "FAILED");
  assert.equal(
    conclusion({
      snapshot: snapshot({
        openTradesSummary: {
          ...snapshot().openTradesSummary,
          totalOpenTrades: 2,
          pairCount: 2,
        },
      }),
    }).state,
    "NORMAL",
  );
});

test("fixture zero rows are never promoted to real empty trades", () => {
  const result = conclusion({ source: "fixture" });
  assert.equal(result.state, "NON_CORE");
  assert.match(result.reason, /不能证明真实余额/);
});

test("backend disabled cannot be bypassed by a URL", () => {
  assert.deepEqual(
    safeFreqUiLink({
      enabled: false,
      baseUrl: "http://127.0.0.1:8080",
      environmentLabel: "local",
      blockedReason: "disabled by backend",
      accessMode: "read-only-link",
    }),
    {
      enabled: false,
      href: null,
      displayUrl: "http://127.0.0.1:8080/",
      status: "BLOCKED",
      reason: "disabled by backend",
    },
  );
});

test("fixture metadata cannot expose an otherwise valid FreqUI link", () => {
  const link = safeFreqUiLink(
    {
      enabled: true,
      baseUrl: "http://127.0.0.1:8080",
      environmentLabel: "local",
      blockedReason: null,
      accessMode: "read-only-link",
    },
    undefined,
    "fixture",
  );
  assert.equal(link.enabled, false);
  assert.equal(link.href, null);
  assert.equal(link.status, "UNAVAILABLE");
});

test("unsafe URL credentials and sensitive query parameters are redacted and blocked", () => {
  const unsafe = safeFreqUiLink({
    enabled: true,
    baseUrl: "http://admin:secret@localhost:8080/?token=visible&view=trades",
    environmentLabel: "local",
    blockedReason: null,
    accessMode: "read-only-link",
  });
  assert.equal(unsafe.enabled, false);
  assert.equal(unsafe.href, null);
  assert.doesNotMatch(unsafe.displayUrl, /secret|visible/);
  assert.match(redactFreqUiUrl("http://localhost/?api_key=abc"), /REDACTED/);
  assert.match(redactFreqUiUrl("http://localhost/?view=private-value"), /REDACTED/);
});

test("event text and command parameters are redacted defensively", () => {
  assert.equal(redactFreqUiText("token=abc123 failed"), "token=[REDACTED] failed");
  assert.deepEqual(
    redactCommandArgs(["freqtrade", "--token", "abc123", "--password=hunter2", "--dry-run"]),
    ["freqtrade", "--token", "[REDACTED]", "--password=[REDACTED]", "--dry-run"],
  );
});

test("runtime safety exposes any live or real-order boundary violation", () => {
  const result = safetyBoundarySummary({
    readOnly: true,
    allowLiveTrading: true,
    allowRealOrders: true,
    allowExchangeConnection: false,
    allowDeployControl: false,
    canStartStopBot: false,
    boundary: "read only",
  });
  assert.equal(result.status, "FAILED");
  assert.deepEqual(result.violations, ["允许 Live trading", "允许真实订单"]);
  assert.equal(
    safeFreqUiLink(
      {
        enabled: true,
        baseUrl: "http://127.0.0.1:8080",
        environmentLabel: "local",
        blockedReason: null,
        accessMode: "read-only-link",
      },
      {
        readOnly: true,
        allowLiveTrading: true,
        allowRealOrders: false,
        allowExchangeConnection: false,
        allowDeployControl: false,
        canStartStopBot: false,
        boundary: "unsafe",
      },
    ).enabled,
    false,
  );
});
