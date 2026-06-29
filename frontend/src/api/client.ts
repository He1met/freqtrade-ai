import { mockMvpData } from "../data/mock";
import type { MvpData } from "./types";

const DEFAULT_API_BASE_URL = "/api";

function getApiBaseUrl() {
  const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
  const configuredUrl = env?.VITE_API_BASE_URL?.trim();

  return (configuredUrl || DEFAULT_API_BASE_URL).replace(/\/$/, "");
}

async function fetchJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }

  return response.json() as Promise<T>;
}

async function fetchList<T>(
  paths: string[],
  fallback: T[],
  signal?: AbortSignal,
): Promise<{ items: T[]; usedFallback: boolean }> {
  for (const path of paths) {
    try {
      return { items: await fetchJson<T[]>(path, signal), usedFallback: false };
    } catch (error) {
      if (signal?.aborted) {
        throw error;
      }
    }
  }

  return { items: fallback, usedFallback: true };
}

export async function loadMvpData(signal?: AbortSignal): Promise<{
  data: MvpData;
  usedFallback: boolean;
}> {
  const [strategies, generationRuns, backtestRuns, backtestTasks, ranking] = await Promise.all([
    fetchList(["/strategies", "/mvp/strategies"], mockMvpData.strategies, signal),
    fetchList(
      ["/generation-runs", "/strategy-generation-runs", "/mvp/generation-runs"],
      mockMvpData.generationRuns,
      signal,
    ),
    fetchList(["/backtest-runs", "/mvp/backtest-runs"], mockMvpData.backtestRuns, signal),
    fetchList(["/backtest-tasks", "/mvp/backtest-tasks"], mockMvpData.backtestTasks, signal),
    fetchList(["/ranking", "/strategy-ranking", "/mvp/ranking"], mockMvpData.ranking, signal),
  ]);

  return {
    data: {
      strategies: strategies.items,
      generationRuns: generationRuns.items,
      backtestRuns: backtestRuns.items,
      backtestTasks: backtestTasks.items,
      ranking: ranking.items,
    },
    usedFallback:
      strategies.usedFallback ||
      generationRuns.usedFallback ||
      backtestRuns.usedFallback ||
      backtestTasks.usedFallback ||
      ranking.usedFallback,
  };
}
