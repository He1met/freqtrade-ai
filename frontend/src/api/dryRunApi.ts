import type {
  DryRunControlPayload,
  DryRunControlReport,
  DryRunReadinessPayload,
  DryRunReadinessReport,
} from "./types";
import { postJson } from "./http";
import {
  normalizeDryRunControl,
  normalizeDryRunReadiness,
  type RawDryRunControlReport,
  type RawDryRunReadinessReport,
} from "./normalizers";

export async function checkDryRunReadiness(payload: DryRunReadinessPayload, signal?: AbortSignal): Promise<DryRunReadinessReport> {
  const raw = await postJson<RawDryRunReadinessReport>("/dry-run/readiness", {
    strategy_version_id: Number(payload.strategyVersionId), strategy_name: payload.strategyName || undefined,
    pair: payload.pair ?? "BTC/USDT:USDT", timeframe: payload.timeframe ?? "15m", exchange: payload.exchange ?? "okx",
  }, { signal });
  return normalizeDryRunReadiness(raw);
}

export async function startControlledDryRun(
  payload: DryRunControlPayload,
  operatorToken: string,
  signal?: AbortSignal,
): Promise<DryRunControlReport> {
  const raw = await postJson<RawDryRunControlReport>("/dry-run/control/start", {
    strategy_version_id: Number(payload.strategyVersionId), strategy_name: payload.strategyName || undefined,
    pair: payload.pair ?? "BTC/USDT:USDT", timeframe: payload.timeframe ?? "15m", exchange: payload.exchange ?? "okx",
    manual_approval: payload.manualApproval === true,
  }, {
    idempotencyKey: `dry-run-start-${crypto.randomUUID()}`,
    operatorToken,
    signal,
  });
  return normalizeDryRunControl(raw);
}

export async function stopControlledDryRun(operatorToken: string, signal?: AbortSignal): Promise<DryRunControlReport> {
  const raw = await postJson<RawDryRunControlReport>(
    "/dry-run/control/stop",
    { reason: "manual stop requested from Local Strategy Lab" },
    {
      idempotencyKey: `dry-run-stop-${crypto.randomUUID()}`,
      operatorToken,
      signal,
    },
  );
  return normalizeDryRunControl(raw);
}
