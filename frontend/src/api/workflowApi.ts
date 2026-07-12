import { postJson } from "./http";
import { normalizeStrategyGenerationResponse, type RawStrategyGenerationApiResponse } from "./normalizers";
import type { StrategyGenerationApiResult } from "./types";

export type WorkflowApiResponse = Record<string, unknown>;

function operatorOptions(operatorToken: string, providerAuthorization?: "once") {
  return {
    idempotencyKey: crypto.randomUUID(),
    operatorToken,
    providerAuthorization,
  };
}

export async function triggerLocalBacktest(
  strategyVersionId: string,
  operatorToken: string,
): Promise<WorkflowApiResponse> {
  return postJson<WorkflowApiResponse>(
    "/backtest-runs/local",
    { strategy_version_id: Number(strategyVersionId), profile: {} },
    operatorOptions(operatorToken),
  );
}

export async function ingestBacktestArtifact(
  taskId: string,
  payload: { manifestPath?: string | null; resultPath?: string | null; strategyName?: string | null },
  operatorToken: string,
): Promise<WorkflowApiResponse> {
  return postJson<WorkflowApiResponse>(
    `/backtest-tasks/${encodeURIComponent(taskId)}/artifact-ingest`,
    {
      manifest_path: payload.manifestPath || undefined,
      result_path: payload.resultPath || undefined,
      strategy_name: payload.strategyName || undefined,
    },
    operatorOptions(operatorToken),
  );
}

export async function runDeepSeekSingle(
  promptSummary: string,
  operatorToken: string,
  allowRealCall: boolean,
): Promise<StrategyGenerationApiResult> {
  const raw = await postJson<RawStrategyGenerationApiResponse>(
    "/strategy-generation-runs/deepseek-single",
    { prompt_summary: promptSummary, allow_real_call: allowRealCall },
    operatorOptions(operatorToken, allowRealCall ? "once" : undefined),
  );
  return normalizeStrategyGenerationResponse(raw);
}
