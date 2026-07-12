import { useCallback, useEffect, useState } from "react";

import {
  ACTION_EVIDENCE_STORAGE_KEY,
  type ActionEvidence,
  recordActionEvidence,
} from "./actionEvidence";

function loadActionEvidence(): ActionEvidence[] {
  try {
    const stored = window.localStorage.getItem(ACTION_EVIDENCE_STORAGE_KEY);
    const parsed: unknown = stored ? JSON.parse(stored) : [];
    return Array.isArray(parsed) ? parsed as ActionEvidence[] : [];
  } catch {
    return [];
  }
}

export function useActionEvidence() {
  const [history, setHistory] = useState<ActionEvidence[]>(loadActionEvidence);

  useEffect(() => {
    try {
      window.localStorage.setItem(ACTION_EVIDENCE_STORAGE_KEY, JSON.stringify(history));
    } catch {
      // Storage is a convenience layer only. Server-side API/DB evidence remains authoritative.
    }
  }, [history]);

  const record = useCallback((entry: ActionEvidence) => {
    setHistory((current) => recordActionEvidence(current, entry));
  }, []);

  return { history, record };
}
