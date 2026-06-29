export type DataSource = "api" | "fallback";

export type StrategyVersionSummary = {
  id: string;
  versionNumber: number;
  filePath: string;
  validationStatus: string;
};

export type StrategySummary = {
  id: string;
  name: string;
  status: string;
  timeframe: string;
  source: string;
  description: string;
  tags: string[];
  currentVersion: StrategyVersionSummary | null;
};

export type GenerationRunSummary = {
  id: string;
  status: string;
  provider: string;
  model: string;
  requestedCount: number;
  generatedCount: number;
  acceptedCount: number;
  failedCount: number;
  errorMessage: string | null;
};

export type BacktestRunSummary = {
  id: string;
  strategyName: string;
  status: string;
  profileName: string;
  requestedTaskCount: number;
  completedTaskCount: number;
  profitPct: number | null;
  maxDrawdownPct: number | null;
};

export type BacktestTaskSummary = {
  id: string;
  runId: string;
  strategyName: string;
  pair: string;
  timeframe: string;
  status: string;
  configPath: string | null;
  resultPath: string | null;
  profitPct: number | null;
  errorMessage: string | null;
};

export type RankingEntry = {
  rank: number;
  strategyId: string;
  strategyName: string;
  versionNumber: number;
  filePath: string;
  totalScore: number;
  profitScore: number | null;
  riskScore: number | null;
  stabilityScore: number | null;
  qualityScore: number | null;
};

export type MvpData = {
  strategies: StrategySummary[];
  generationRuns: GenerationRunSummary[];
  backtestRuns: BacktestRunSummary[];
  backtestTasks: BacktestTaskSummary[];
  ranking: RankingEntry[];
};

export type MvpDataState = {
  data: MvpData;
  source: DataSource;
  isLoading: boolean;
  error: string | null;
};
