import { useCallback, useEffect, useState } from "react";

import { fetchJson } from "./http";
import * as N from "./normalizers";
import {
  isCoreDataSourceTrace,
} from "./sourceState";
import type { RankingEntry, StrategyGenerationVersion, StrategySummary } from "./types";
import type { ReadModelState } from "./useFormalReadModels";

type FormalCatalogState = {
  strategies: ReadModelState<StrategySummary[]>;
  strategyVersions: ReadModelState<StrategyGenerationVersion[]>;
  ranking: ReadModelState<RankingEntry[]>;
};

const loading = <T,>(): ReadModelState<T> => ({ data: null, error: null, loading: true });
const errorText = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);

export function useFormalCatalogData(): FormalCatalogState & { refresh: () => void } {
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const [state, setState] = useState<FormalCatalogState>({
    strategies: loading(),
    strategyVersions: loading(),
    ranking: loading(),
  });

  useEffect(() => {
    const controller = new AbortController();
    setState((current) => ({
      strategies: { ...current.strategies, error: null, loading: true },
      strategyVersions: { ...current.strategyVersions, error: null, loading: true },
      ranking: { ...current.ranking, error: null, loading: true },
    }));
    void Promise.allSettled([
      fetchJson<N.RawStrategySummary[]>("/strategies", controller.signal),
      fetchJson<N.RawStrategyGenerationVersion[]>("/strategy-versions", controller.signal),
      fetchJson<N.RawRankingEntry[]>("/ranking", controller.signal),
    ]).then(([strategyResult, versionResult, rankingResult]) => {
      if (controller.signal.aborted) return;
      const versions = versionResult.status === "fulfilled"
        ? versionResult.value
          .map(N.normalizeStrategyGenerationVersion)
          .filter((version) => isCoreDataSourceTrace(version.dataSource))
        : [];
      const strategies = strategyResult.status === "fulfilled"
        ? strategyResult.value
          .map((strategy) => N.normalizeStrategySummary(strategy, versions))
          .filter((strategy) => isCoreDataSourceTrace(strategy.dataSource))
        : [];
      const ranking = rankingResult.status === "fulfilled"
        ? rankingResult.value.map(N.normalizeRankingEntry).filter((entry) => isCoreDataSourceTrace(entry.dataSource))
        : [];
      setState({
        strategies: strategyResult.status === "fulfilled"
          ? { data: strategies, error: versionResult.status === "rejected" ? "策略版本读取失败，目录版本状态未知" : null, loading: false }
          : { data: null, error: errorText(strategyResult.reason), loading: false },
        strategyVersions: versionResult.status === "fulfilled"
          ? { data: versions, error: null, loading: false }
          : { data: null, error: errorText(versionResult.reason), loading: false },
        ranking: rankingResult.status === "fulfilled"
          ? { data: ranking, error: null, loading: false }
          : { data: null, error: errorText(rankingResult.reason), loading: false },
      });
    });
    return () => controller.abort();
  }, [revision]);

  return { ...state, refresh };
}
