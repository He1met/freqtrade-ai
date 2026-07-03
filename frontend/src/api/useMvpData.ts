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

let cachedState: MvpDataState | null = null;
let pendingLoad: Promise<MvpDataState> | null = null;

function getMvpDataState(): Promise<MvpDataState> {
  if (cachedState) {
    return Promise.resolve(cachedState);
  }

  if (!pendingLoad) {
    pendingLoad = loadMvpData()
      .then(({ data, usedFallback }) => {
        cachedState = {
          data,
          source: usedFallback ? "fallback" : "api",
          isLoading: false,
          error: null,
        };
        return cachedState;
      })
      .catch((error: unknown) => {
        cachedState = {
          data: mockMvpData,
          source: "fallback",
          isLoading: false,
          error:
            error instanceof Error
              ? error.message
              : "无法加载 MVP 数据，已使用本地示例数据。",
        };
        return cachedState;
      })
      .finally(() => {
        pendingLoad = null;
      });
  }

  return pendingLoad;
}

export function useMvpData(): MvpDataState {
  const [state, setState] = useState<MvpDataState>(cachedState ?? initialState);

  useEffect(() => {
    let isMounted = true;

    getMvpDataState().then((nextState) => {
      if (isMounted) {
        setState(nextState);
      }
    });

    return () => {
      isMounted = false;
    };
  }, []);

  return state;
}

export function resetMvpDataCacheForTests() {
  cachedState = null;
  pendingLoad = null;
}
