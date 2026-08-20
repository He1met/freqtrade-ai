export type CanonicalWindowEvidenceState = "gate-failed" | "gate-passed" | "missing-result" | "result-only";

type MetricWindow = {
  result: { metrics_json: Record<string, unknown> } | null;
  window_key: string;
};

function metricText(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

export function canonicalScorePercent(value: string | null): number | null {
  if (value === null || !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 && parsed <= 100 ? parsed : null;
}

export function canonicalMetricMatrix(windows: readonly MetricWindow[]) {
  const metricKeys = [...new Set(windows.flatMap((window) => (
    window.result ? Object.keys(window.result.metrics_json) : []
  )))].sort((left, right) => left.localeCompare(right, "en"));
  return {
    metricKeys,
    rows: windows.map((window) => ({
      values: window.result ? Object.fromEntries(Object.entries(window.result.metrics_json).map(
        ([key, value]) => [key, metricText(value)],
      )) : {},
      windowKey: window.window_key,
    })),
  };
}

export function canonicalWindowEvidenceState(window: {
  qualification_evidence: { hard_gate_passed: boolean } | null;
  result: object | null;
}): CanonicalWindowEvidenceState {
  if (!window.result) return "missing-result";
  if (!window.qualification_evidence) return "result-only";
  return window.qualification_evidence.hard_gate_passed ? "gate-passed" : "gate-failed";
}
