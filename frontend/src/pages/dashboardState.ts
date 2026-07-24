export type DashboardViewState = "loading" | "failed" | "empty" | "ready";

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
