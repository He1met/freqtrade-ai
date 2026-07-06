import type { DataSource, MvpDataSetKey, MvpDataSources } from "./types";

export const MVP_DATA_SET_KEYS: MvpDataSetKey[] = [
  "strategies",
  "strategyVersions",
  "generationRuns",
  "backtestRuns",
  "backtestTasks",
  "backtestResults",
  "hyperoptRuns",
  "dryRun",
  "liveCandidates",
  "operatorDashboard",
  "ranking",
  "failureReasons",
  "versionLineage",
];

export function fallbackMvpDataSources(): MvpDataSources {
  return Object.fromEntries(MVP_DATA_SET_KEYS.map((key) => [key, "fallback"])) as MvpDataSources;
}

export function combineDataSources(
  sources: MvpDataSources,
  keys: MvpDataSetKey[],
): DataSource {
  return keys.some((key) => sources[key] === "api") ? "api" : "fallback";
}
