import { fetchList } from "./http";
import type {
  RawRankingEntry,
  RawStrategyFailureReason,
  RawStrategyVersionLineageEntry,
} from "./normalizers";

export function loadRankingCollections(
  fallback: {
    ranking: RawRankingEntry[];
    failureReasons: RawStrategyFailureReason[];
    versionLineage: RawStrategyVersionLineageEntry[];
  },
  signal?: AbortSignal,
) {
  return Promise.all([
    fetchList(["/ranking", "/strategy-ranking", "/mvp/ranking"], fallback.ranking, signal),
    fetchList(["/strategy-failure-reasons", "/mvp/strategy-failure-reasons"], fallback.failureReasons, signal),
    fetchList(
      ["/strategy-version-lineage", "/strategy-versions/lineage", "/mvp/strategy-version-lineage"],
      fallback.versionLineage,
      signal,
    ),
  ]);
}
