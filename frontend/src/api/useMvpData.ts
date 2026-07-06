import { useEffect, useState } from "react";

import { mockMvpData } from "../data/mock";
import { loadMvpData } from "./client";
import { fallbackMvpDataSources } from "./sourceState";
import type { MvpDataState } from "./types";

const initialState: MvpDataState = {
  data: mockMvpData,
  source: "fallback",
  sources: fallbackMvpDataSources(),
  isLoading: true,
  error: null,
};

export function useMvpData(refreshToken = 0): MvpDataState {
  const [state, setState] = useState<MvpDataState>(initialState);

  useEffect(() => {
    const controller = new AbortController();
    setState((current) => ({
      ...current,
      isLoading: true,
      error: null,
    }));

    loadMvpData(controller.signal)
      .then(({ data, sources, usedFallback }) => {
        setState({
          data,
          source: usedFallback ? "fallback" : "api",
          sources,
          isLoading: false,
          error: null,
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }

        setState({
          data: mockMvpData,
          source: "fallback",
          sources: fallbackMvpDataSources(),
          isLoading: false,
          error: error instanceof Error ? error.message : "Unable to load MVP data.",
        });
      });

    return () => {
      controller.abort();
    };
  }, [refreshToken]);

  return state;
}
