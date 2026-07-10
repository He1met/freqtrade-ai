import { fetchList } from "./http";
import type {
  RawRankingEntry,
  RawStrategyFailureReason,
  RawStrategyVersionLineageEntry,
} from "./normalizers";

export function loadRankingCollections(
  signal?: AbortSignal,
) {
  return Promise.all([
    fetchList<RawRankingEntry>(["/ranking", "/strategy-ranking", "/mvp/ranking"], signal),
    fetchList<RawStrategyFailureReason>(["/strategy-failure-reasons", "/mvp/strategy-failure-reasons"], signal),
    fetchList<RawStrategyVersionLineageEntry>(
      ["/strategy-version-lineage", "/strategy-versions/lineage", "/mvp/strategy-version-lineage"],
      signal,
    ),
  ]);
}
