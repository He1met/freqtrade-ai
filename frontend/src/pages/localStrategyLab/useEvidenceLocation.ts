import { useEffect, useState } from "react";

import {
  isEvidenceScope,
  isEvidenceTab,
  type EvidenceScope,
  type EvidenceTab,
} from "./evidenceBrowserModel";

export type EvidenceLocation = {
  tab: EvidenceTab;
  recordId: string | null;
  scope: EvidenceScope;
};

function readLocation(): EvidenceLocation {
  const params = new URLSearchParams(window.location.search);
  return {
    tab: isEvidenceTab(params.get("lab_tab")) ? params.get("lab_tab") as EvidenceTab : "generation",
    recordId: params.get("lab_record")?.trim() || null,
    scope: isEvidenceScope(params.get("lab_scope")) ? params.get("lab_scope") as EvidenceScope : "current",
  };
}

function writeLocation(location: EvidenceLocation) {
  const url = new URL(window.location.href);
  url.searchParams.set("lab_tab", location.tab);
  url.searchParams.set("lab_scope", location.scope);
  if (location.recordId) url.searchParams.set("lab_record", location.recordId);
  else url.searchParams.delete("lab_record");
  window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
}

export function useEvidenceLocation() {
  const [location, setLocationState] = useState<EvidenceLocation>(() => readLocation());

  useEffect(() => {
    writeLocation(location);
    const handlePopState = () => setLocationState(readLocation());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  function setLocation(next: EvidenceLocation) {
    setLocationState(next);
    writeLocation(next);
  }

  return { location, setLocation };
}
