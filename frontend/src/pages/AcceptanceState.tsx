import type { AcceptanceStateSummary } from "../api/types";

const STATE_STYLES: Record<AcceptanceStateSummary["state"], { background: string; color: string; borderColor: string }> = {
  ACCEPTABLE: { background: "#ecfdf3", color: "#166534", borderColor: "#86efac" },
  NOT_ACCEPTABLE: { background: "#fff7ed", color: "#9a3412", borderColor: "#fdba74" },
  BLOCKED: { background: "#fef3c7", color: "#92400e", borderColor: "#fcd34d" },
  FAILED: { background: "#fef2f2", color: "#991b1b", borderColor: "#fca5a5" },
  NOT_RUN: { background: "#eff6ff", color: "#1d4ed8", borderColor: "#93c5fd" },
  API_GAP: { background: "#f5f3ff", color: "#6d28d9", borderColor: "#c4b5fd" },
};

export function AcceptanceState({ summary }: { summary: AcceptanceStateSummary }) {
  const tone = STATE_STYLES[summary.state];

  return (
    <div style={{ display: "grid", gap: "0.4rem" }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", alignItems: "center" }}>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            padding: "0.15rem 0.5rem",
            borderRadius: "999px",
            border: `1px solid ${tone.borderColor}`,
            background: tone.background,
            color: tone.color,
            fontSize: "0.75rem",
            fontWeight: 700,
            lineHeight: 1.2,
          }}
        >
          {summary.state}
        </span>
        <span>{summary.canAccept ? "可验收" : "不可验收"}</span>
      </div>
      <span>{summary.reason}</span>
    </div>
  );
}
