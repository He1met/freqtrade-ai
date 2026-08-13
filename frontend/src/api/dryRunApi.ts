import type {
  DryRunControlPayload,
  DryRunControlReport,
  DryRunReadinessPayload,
  DryRunReadinessReport,
} from "./types";
import { postJson } from "./http";
import { requiredDryRunTargetField } from "./dryRunTarget.ts";
import {
  normalizeDryRunControl,
  normalizeDryRunReadiness,
  type RawDryRunControlReport,
  type RawDryRunReadinessReport,
} from "./normalizers";

export async function checkDryRunReadiness(payload: DryRunReadinessPayload, signal?: AbortSignal): Promise<DryRunReadinessReport> {
  const raw = await postJson<RawDryRunReadinessReport>("/dry-run/readiness", {
    strategy_version_id: Number(payload.strategyVersionId), strategy_name: payload.strategyName || undefined,
    pair: requiredDryRunTargetField(payload.pair, "pair"),
    timeframe: requiredDryRunTargetField(payload.timeframe, "timeframe"),
    exchange: requiredDryRunTargetField(payload.exchange, "exchange"),
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
    pair: requiredDryRunTargetField(payload.pair, "pair"),
    timeframe: requiredDryRunTargetField(payload.timeframe, "timeframe"),
    exchange: requiredDryRunTargetField(payload.exchange, "exchange"),
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
