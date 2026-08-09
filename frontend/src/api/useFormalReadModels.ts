import { useCallback, useEffect, useState } from "react";

import { fetchOkxDemoRuntimeActivity, type OkxDemoRuntimeActivity } from "./okxDemoRuntimeActivityApi";
import { fetchStrategyResearchWorkspace, type StrategyResearchWorkspace } from "./strategyResearchApi";

export type ReadModelState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
};

const initial = <T,>(): ReadModelState<T> => ({ data: null, error: null, loading: true });

function errorText(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

export function useFormalReadModels() {
  const [workspace, setWorkspace] = useState<ReadModelState<StrategyResearchWorkspace>>(initial);
  const [runtimeActivity, setRuntimeActivity] = useState<ReadModelState<OkxDemoRuntimeActivity>>(initial);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setWorkspace((current) => ({ ...current, error: null, loading: true }));
    setRuntimeActivity((current) => ({ ...current, error: null, loading: true }));
    fetchStrategyResearchWorkspace(controller.signal)
      .then((data) => setWorkspace({ data, error: null, loading: false }))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setWorkspace({ data: null, error: errorText(reason), loading: false });
      });
    fetchOkxDemoRuntimeActivity(controller.signal)
      .then((data) => setRuntimeActivity({ data, error: null, loading: false }))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setRuntimeActivity({ data: null, error: errorText(reason), loading: false });
      });
    return () => controller.abort();
  }, [revision]);

  return { workspace, runtimeActivity, refresh };
}
