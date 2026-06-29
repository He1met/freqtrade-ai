import { useEffect, useState } from "react";

import { mockMvpData } from "../data/mock";
import { loadMvpData } from "./client";
import type { MvpDataState } from "./types";

const initialState: MvpDataState = {
  data: mockMvpData,
  source: "fallback",
  isLoading: true,
  error: null,
};

export function useMvpData(): MvpDataState {
  const [state, setState] = useState<MvpDataState>(initialState);

  useEffect(() => {
    const controller = new AbortController();

    loadMvpData(controller.signal)
      .then(({ data, usedFallback }) => {
        setState({
          data,
          source: usedFallback ? "fallback" : "api",
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
          isLoading: false,
          error: error instanceof Error ? error.message : "Unable to load MVP data.",
        });
      });

    return () => {
      controller.abort();
    };
  }, []);

  return state;
}
