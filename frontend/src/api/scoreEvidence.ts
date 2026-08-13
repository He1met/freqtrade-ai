import type { RankingScoreBreakdownItem } from "./types.ts";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function optionalFiniteScore(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function normalizePersistedScoreBreakdown(raw: unknown): RankingScoreBreakdownItem[] {
  return Array.isArray(raw)
    ? raw.map((item) => {
        const value = record(item);
        return {
          name: typeof value.name === "string" ? value.name : "score",
          score: optionalFiniteScore(value.score),
          weight: optionalFiniteScore(value.weight),
          contribution: optionalFiniteScore(value.contribution),
        };
      })
    : [];
}
