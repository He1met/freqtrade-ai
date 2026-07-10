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
  }, signal);
  return normalizeDryRunReadiness(raw);
}

export async function startControlledDryRun(payload: DryRunControlPayload, signal?: AbortSignal): Promise<DryRunControlReport> {
  const raw = await postJson<RawDryRunControlReport>("/dry-run/control/start", {
    strategy_version_id: Number(payload.strategyVersionId), strategy_name: payload.strategyName || undefined,
    pair: payload.pair ?? "BTC/USDT:USDT", timeframe: payload.timeframe ?? "15m", exchange: payload.exchange ?? "okx",
    manual_approval: payload.manualApproval === true,
  }, signal);
  return normalizeDryRunControl(raw);
}

export async function stopControlledDryRun(signal?: AbortSignal): Promise<DryRunControlReport> {
  const raw = await postJson<RawDryRunControlReport>("/dry-run/control/stop", { reason: "manual stop requested from Local Strategy Lab" }, signal);
  return normalizeDryRunControl(raw);
}
