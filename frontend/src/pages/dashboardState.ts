export type DashboardViewState = "loading" | "failed" | "empty" | "ready";
export type DashboardActivityState = DashboardViewState | "partial";

export function dashboardViewState({
  error,
  isLoading,
  source,
  visibleRecordCount,
}: {
  error: string | null;
  isLoading: boolean;
  source: string;
  visibleRecordCount: number;
}): DashboardViewState {
  if (isLoading) {
    return "loading";
  }
  if (error || source === "failed") {
    return "failed";
  }
  if (visibleRecordCount === 0) {
    return "empty";
  }
  return "ready";
}

export function dashboardActivityState({
  error,
  isLoading,
  visibleRecordCount,
}: {
  error: string | null;
  isLoading: boolean;
  visibleRecordCount: number;
}): DashboardActivityState {
  if (isLoading && visibleRecordCount === 0) return "loading";
  if (error && visibleRecordCount === 0) return "failed";
  if (visibleRecordCount === 0) return "empty";
  if (error || isLoading) return "partial";
  return "ready";
}
