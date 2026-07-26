import { useCallback, useEffect, useState } from "react";

import {
  ACTION_EVIDENCE_STORAGE_KEY,
  LEGACY_ACTION_EVIDENCE_STORAGE_KEY,
  type ActionEvidence,
  type ActionEvidenceHistoryState,
  parseStoredActionEvidence,
  recordActionEvidence,
} from "./actionEvidence";

function loadActionEvidence(): {
  history: ActionEvidence[];
  state: ActionEvidenceHistoryState;
} {
  try {
    return parseStoredActionEvidence(
      window.localStorage.getItem(ACTION_EVIDENCE_STORAGE_KEY),
      window.localStorage.getItem(LEGACY_ACTION_EVIDENCE_STORAGE_KEY),
    );
  } catch {
    return { history: [], state: "unavailable" };
  }
}

export function useActionEvidence() {
  const [initial] = useState(loadActionEvidence);
  const [history, setHistory] = useState<ActionEvidence[]>(initial.history);

  useEffect(() => {
    try {
      window.localStorage.setItem(ACTION_EVIDENCE_STORAGE_KEY, JSON.stringify(history));
      if (initial.state === "migrated-v1") {
        window.localStorage.removeItem(LEGACY_ACTION_EVIDENCE_STORAGE_KEY);
      }
    } catch {
      // Storage is a convenience layer only. Server-side API/DB evidence remains authoritative.
    }
  }, [history, initial.state]);

  const record = useCallback((entry: ActionEvidence) => {
    setHistory((current) => recordActionEvidence(current, entry));
  }, []);

  return { history, historyState: initial.state, record };
}
