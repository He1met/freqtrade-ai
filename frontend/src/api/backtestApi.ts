import { fetchList } from "./http";
import type {
  RawBacktestResultSummary,
  RawBacktestRunSummary,
  RawBacktestTaskSummary,
} from "./normalizers";

export function loadBacktestCollections(
  signal?: AbortSignal,
) {
  return Promise.all([
    fetchList<RawBacktestRunSummary>(["/backtest-runs", "/mvp/backtest-runs"], signal),
    fetchList<RawBacktestTaskSummary>(["/backtest-tasks", "/mvp/backtest-tasks"], signal),
    fetchList<RawBacktestResultSummary>(["/backtest-results"], signal),
  ]);
}
