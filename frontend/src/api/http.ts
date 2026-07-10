const DEFAULT_API_BASE_URL = "/api";

function apiBaseUrl(): string {
  const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
  return (env?.VITE_API_BASE_URL?.trim() || DEFAULT_API_BASE_URL).replace(/\/$/, "");
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function redact(value: string): string {
  return value
    .replace(
      /\b(api[_-]?key|api[_-]?secret|secret|password|passphrase|token)(\s*[:=]\s*)([^\s,;]+)/gi,
      "$1$2[REDACTED]",
    )
    .replace(/\bbearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]");
}

export class StrategyGenerationApiError extends Error {
  readonly detail: unknown;
  readonly failedReason: string | null;
  readonly status: number;
  readonly statusText: string;
  readonly strategyGenerationRunId: string | null;

  constructor(response: Response, detail: unknown) {
    const value = asRecord(detail);
    const nested = asRecord(value.detail ?? detail);
    const failedReason = optionalString(nested.failed_reason ?? nested.failedReason);
    const detailText = typeof value.detail === "string"
      ? value.detail
      : optionalString(nested.message ?? nested.reason);
    super(redact(failedReason ?? detailText ?? `${response.status} ${response.statusText}`));
    this.name = "StrategyGenerationApiError";
    this.detail = detail;
    this.failedReason = failedReason ? redact(failedReason) : null;
    this.status = response.status;
    this.statusText = response.statusText;
    const runId = nested.strategy_generation_run_id ?? nested.strategyGenerationRunId;
    this.strategyGenerationRunId = runId === null || runId === undefined ? null : String(runId);
  }
}

export async function fetchJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${apiBaseUrl()}${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

export async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${apiBaseUrl()}${path}`, {
    body: JSON.stringify(body),
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    method: "POST",
    signal,
  });
  if (!response.ok) {
    let detail: unknown = null;
    try { detail = await response.json(); } catch { detail = null; }
    throw new StrategyGenerationApiError(response, detail);
  }
  return response.json() as Promise<T>;
}

function requestFailure(paths: string[], errors: unknown[]): Error {
  const detail = errors
    .map((error) => error instanceof Error ? error.message : String(error))
    .join("; ");
  return new Error(`Backend API request failed for ${paths.join(" or ")}: ${detail}`);
}

export async function fetchList<T>(paths: string[], signal?: AbortSignal) {
  const errors: unknown[] = [];
  for (const path of paths) {
    try { return { items: await fetchJson<T[]>(path, signal), usedFallback: false }; }
    catch (error) {
      if (signal?.aborted) throw error;
      errors.push(error);
    }
  }
  throw requestFailure(paths, errors);
}

export async function fetchValue<T>(paths: string[], signal?: AbortSignal) {
  const errors: unknown[] = [];
  for (const path of paths) {
    try { return { item: await fetchJson<T>(path, signal), usedFallback: false }; }
    catch (error) {
      if (signal?.aborted) throw error;
      errors.push(error);
    }
  }
  throw requestFailure(paths, errors);
}
