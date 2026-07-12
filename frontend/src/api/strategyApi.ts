import type { StrategyGenerationApiResult, StrategyGenerationSubmitPayload } from "./types";
import { postJson } from "./http";
import {
  normalizeStrategyGenerationResponse,
  type RawStrategyGenerationApiResponse,
} from "./normalizers";

export async function createStrategyGenerationRun(
  payload: StrategyGenerationSubmitPayload,
  signal?: AbortSignal,
): Promise<StrategyGenerationApiResult> {
  const raw = await postJson<RawStrategyGenerationApiResponse>(
    "/strategy-generation-runs",
    { prompt_summary: payload.promptSummary, requested_count: payload.requestedCount },
    {
      idempotencyKey: `strategy-generation-${crypto.randomUUID()}`,
      operatorToken: payload.operatorToken,
      providerAuthorization: payload.authorizeRealProvider ? "once" : undefined,
      signal,
    },
  );
  return normalizeStrategyGenerationResponse(raw);
}
