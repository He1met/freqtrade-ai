export const summary = [
  { label: "Strategies", value: "0" },
  { label: "Versions", value: "0" },
  { label: "Backtests", value: "0" },
  { label: "Candidates", value: "0" },
];

export const mockStrategies = [
  {
    id: "ai-rsi-ema-001",
    name: "AiRsiEma001",
    status: "draft",
    timeframe: "1h",
    source: "ai",
  },
  {
    id: "ai-breakout-001",
    name: "AiBreakout001",
    status: "candidate",
    timeframe: "15m",
    source: "ai",
  },
];

export const mockRuns = [
  { name: "phase0-placeholder", status: "pending", total: 0, success: 0, failed: 0 },
];
