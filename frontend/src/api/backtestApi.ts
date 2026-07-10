import { fetchList } from "./http";
import type {
  RawBacktestResultSummary,
  RawBacktestRunSummary,
  RawBacktestTaskSummary,
} from "./normalizers";

export function loadBacktestCollections(
  fallback: {
    runs: RawBacktestRunSummary[];
    tasks: RawBacktestTaskSummary[];
    results: RawBacktestResultSummary[];
  },
  signal?: AbortSignal,
) {
  return Promise.all([
    fetchList(["/backtest-runs", "/mvp/backtest-runs"], fallback.runs, signal),
    fetchList(["/backtest-tasks", "/mvp/backtest-tasks"], fallback.tasks, signal),
    fetchList(["/backtest-results"], fallback.results, signal),
  ]);
}
