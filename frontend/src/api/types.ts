export type DataSource = "api" | "fallback";

export type StrategyVersionSummary = {
  id: string;
  versionNumber: number;
  filePath: string;
  validationStatus: string;
  validationErrors: ValidationErrorSummary[];
};

export type ValidationErrorSummary = {
  field: string | null;
  message: string;
  code: string | null;
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
  artifactManifest: BacktestArtifactManifest | null;
  metrics: BacktestMetricSummary;
  blockedReason: string | null;
  failedReason: string | null;
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
  artifactManifest: BacktestArtifactManifest | null;
  metrics: BacktestMetricSummary;
  blockedReason: string | null;
  failedReason: string | null;
};

export type BacktestArtifactStatus = "SUCCESS" | "FAILED" | "BLOCKED" | string;

export type BacktestArtifactManifest = {
  manifestVersion: number | null;
  status: BacktestArtifactStatus;
  configPath: string | null;
  strategyName: string | null;
  resultPath: string | null;
  manifestPath: string | null;
  commandArgs: string[];
  returnCode: number | null;
  stdout: string;
  stderr: string;
  datadir: string | null;
  strategyPath: string | null;
  userdir: string | null;
  blockedReason: string | null;
  failedReason: string | null;
};

export type BacktestMetricSummary = {
  profitTotal: number | null;
  profitPct: number | null;
  maxDrawdownPct: number | null;
  winRate: number | null;
  totalTrades: number | null;
  timerange: string | null;
  sharpe: number | null;
  sortino: number | null;
  calmar: number | null;
};

export type HyperoptArtifactStatus = "SUCCESS" | "FAILED" | "BLOCKED" | string;

export type HyperoptArtifactManifest = {
  manifestVersion: number | null;
  status: HyperoptArtifactStatus;
  configPath: string | null;
  strategyName: string | null;
  resultPath: string | null;
  manifestPath: string | null;
  commandArgs: string[];
  returnCode: number | null;
  stdout: string;
  stderr: string;
  datadir: string | null;
  strategyPath: string | null;
  userdir: string | null;
  spaces: string[];
  epochs: number | null;
  hyperoptLoss: string | null;
  blockedReason: string | null;
  failedReason: string | null;
};

export type HyperoptMetricComparison = {
  label: string;
  before: number | null;
  after: number | null;
  delta: number | null;
  suffix: string;
};

export type HyperoptComparisonSummary = {
  parentVersionId: string | null;
  optimizedVersionId: string | null;
  status: "SUCCESS" | "FAILED" | "BLOCKED" | string;
  metrics: HyperoptMetricComparison[];
  warnings: RankingSignalSummary[];
  blockedReason: string | null;
  failedReason: string | null;
};

export type HyperoptRunSummary = {
  id: string;
  strategyName: string;
  status: string;
  profileName: string;
  spaces: string[];
  bestParams: Record<string, unknown>;
  bestLoss: number | null;
  score: number | null;
  epoch: number | null;
  artifactManifest: HyperoptArtifactManifest | null;
  resultPath: string | null;
  manifestPath: string | null;
  blockedReason: string | null;
  failedReason: string | null;
  comparison: HyperoptComparisonSummary | null;
};

export type RankingEntry = {
  rank: number;
  strategyId: string;
  strategyName: string;
  versionNumber: number;
  filePath: string;
  scoringVersion: string | null;
  totalScore: number;
  rawTotalScore: number | null;
  profitScore: number | null;
  riskScore: number | null;
  stabilityScore: number | null;
  qualityScore: number | null;
  scoreBreakdown: RankingScoreBreakdownItem[];
  elimination: RankingEliminationSummary;
  warnings: RankingSignalSummary[];
};

export type RankingScoreBreakdownItem = {
  name: string;
  score: number;
  weight: number;
  contribution: number;
};

export type RankingSignalSummary = {
  code: string | null;
  severity: string;
  message: string;
};

export type RankingEliminationSummary = {
  eliminated: boolean;
  reasons: RankingSignalSummary[];
};

export type StrategyFailureReasonSummary = {
  id: string;
  strategyId: string;
  strategyVersionId: string;
  stage: string;
  reasonType: string;
  severity: "info" | "warning" | "error" | string;
  message: string;
  details: Record<string, unknown>;
  createdAt: string | null;
};

export type StrategyVersionLineageEntry = {
  id: string;
  strategyId: string;
  parentVersionId: string | null;
  versionNumber: number;
  changeSummary: string | null;
  diffSnapshot: Record<string, unknown>;
  hasParent: boolean;
  createdAt: string | null;
};

export type MvpData = {
  strategies: StrategySummary[];
  generationRuns: GenerationRunSummary[];
  backtestRuns: BacktestRunSummary[];
  backtestTasks: BacktestTaskSummary[];
  hyperoptRuns: HyperoptRunSummary[];
  ranking: RankingEntry[];
  failureReasons: StrategyFailureReasonSummary[];
  versionLineage: StrategyVersionLineageEntry[];
};

export type MvpDataState = {
  data: MvpData;
  source: DataSource;
  isLoading: boolean;
  error: string | null;
};
