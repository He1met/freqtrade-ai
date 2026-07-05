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

export type DryRunArtifactStatus = "SUCCESS" | "FAILED" | "BLOCKED" | "SKIPPED" | string;

export type DryRunArtifactManifest = {
  manifestVersion: number | null;
  status: DryRunArtifactStatus;
  profileName: string | null;
  strategyVersionId: number | null;
  strategyName: string | null;
  pair: string | null;
  timeframe: string | null;
  configPath: string | null;
  manifestPath: string | null;
  commandArgs: string[];
  returnCode: number | null;
  stdout: string;
  stderr: string;
  userdir: string | null;
  strategyPath: string | null;
  blockedReason: string | null;
  failedReason: string | null;
  skippedReason: string | null;
};

export type DryRunBalanceSummary = {
  currency: string | null;
  total: number | null;
  free: number | null;
  used: number | null;
  realizedProfit: number | null;
  unrealizedProfit: number | null;
};

export type DryRunOpenTradesSummary = {
  totalOpenTrades: number;
  pairCount: number;
  pairs: string[];
  totalStakeAmount: number | null;
  totalProfitAbs: number | null;
  totalProfitPct: number | null;
};

export type DryRunEventSummary = {
  timestamp: string;
  eventType: string;
  severity: "INFO" | "WARNING" | "ERROR" | "CRITICAL" | string;
  message: string;
  source: string;
};

export type DryRunStatusSnapshot = {
  status: "SUCCESS" | "FAILED" | "BLOCKED" | "SKIPPED" | "RUNNING" | "STOPPED" | string;
  profileName: string | null;
  strategyVersionId: number | null;
  strategyName: string | null;
  exchange: string | null;
  pair: string | null;
  timeframe: string | null;
  dryRun: boolean | null;
  balanceSummary: DryRunBalanceSummary;
  openTradesSummary: DryRunOpenTradesSummary;
  recentEvents: DryRunEventSummary[];
  blockedReason: string | null;
  failedReason: string | null;
  skippedReason: string | null;
  lastUpdated: string | null;
  artifactManifestPath: string | null;
};

export type FreqUILinkMetadata = {
  enabled: boolean;
  baseUrl: string | null;
  environmentLabel: string;
  blockedReason: string | null;
  accessMode: "read-only-link" | string;
};

export type DryRunManagementSummary = {
  manifest: DryRunArtifactManifest | null;
  snapshot: DryRunStatusSnapshot;
  freqUiLink: FreqUILinkMetadata;
};

export type LiveCandidateRiskCheckSummary = {
  name: string;
  status: "PASS" | "BLOCKED" | "FAILED" | string;
  summary: string;
  evidenceRef: string | null;
  blockedReason: string | null;
};

export type LiveCandidateProfileSummary = {
  id: string;
  profileName: string;
  strategyName: string;
  pair: string;
  timeframe: string;
  status: "APPROVED_FOR_REVIEW" | "BLOCKED" | "FAILED" | string;
  profileHash: string | null;
  canEnterHumanApproval: boolean;
  evidenceRefs: string[];
  blockers: string[];
  warnings: string[];
  riskChecks: LiveCandidateRiskCheckSummary[];
  sourceRef: string | null;
  updatedAt: string | null;
};

export type LiveCandidateApprovalDecisionSummary = {
  decision: "APPROVE" | "REJECT" | "REVOKE" | "EXPIRE" | string;
  actorName: string;
  actorRole: string;
  decidedAt: string | null;
  basis: string | null;
};

export type LiveCandidateApprovalRecordSummary = {
  recordId: string;
  profileName: string;
  profileHash: string | null;
  status: string;
  preflightStatus: string;
  requiredApprovals: number;
  completedApprovals: number;
  canCreateDeploymentRecord: boolean;
  submittedBy: string;
  submittedAt: string | null;
  riskSummaryRef: string | null;
  decisions: LiveCandidateApprovalDecisionSummary[];
  blockers: string[];
};

export type LiveCandidateRollbackStepSummary = {
  order: number;
  title: string;
  owner: string;
  verification: string;
};

export type LiveCandidateRollbackPlanSummary = {
  planId: string;
  owner: string;
  summary: string;
  evidenceRefs: string[];
  steps: LiveCandidateRollbackStepSummary[];
};

export type LiveCandidateDeploymentRecordSummary = {
  recordId: string;
  profileName: string;
  status: string;
  plannedEnvironment: string;
  approvalStatus: string;
  preflightStatus: string;
  plannedBy: string;
  plannedAt: string | null;
  rollbackPlan: LiveCandidateRollbackPlanSummary | null;
  blockers: string[];
  resultStatus: string | null;
  resultRecordedAt: string | null;
};

export type LiveCandidateMonitoringSourceSummary = {
  source: "fixture" | "artifact" | "controlled-local-json" | string;
  ref: string;
  collectedAt: string | null;
};

export type LiveCandidateAlertSummary = {
  alertId: string;
  status: string;
  severity: "INFO" | "WARNING" | "ERROR" | "CRITICAL" | string;
  message: string;
  evidenceRef: string | null;
};

export type LiveCandidateMonitoringSnapshotSummary = {
  snapshotId: string;
  status: "OK" | "WARNING" | "BLOCKED" | "UNAVAILABLE" | "STALE" | string;
  profileName: string | null;
  deploymentRecordId: string | null;
  deploymentStatus: string | null;
  approvalStatus: string | null;
  preflightStatus: string | null;
  source: LiveCandidateMonitoringSourceSummary;
  alerts: LiveCandidateAlertSummary[];
  blockers: string[];
  warnings: string[];
  unavailableReason: string | null;
  staleReason: string | null;
  safetyBoundary: string;
  updatedAt: string | null;
};

export type LiveCandidateGovernanceSummary = {
  sourceRef: string | null;
  readOnly: boolean;
  safetyBoundary: string;
  profiles: LiveCandidateProfileSummary[];
  approvals: LiveCandidateApprovalRecordSummary[];
  deployments: LiveCandidateDeploymentRecordSummary[];
  monitoringSnapshots: LiveCandidateMonitoringSnapshotSummary[];
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
  dryRun: DryRunManagementSummary;
  liveCandidates: LiveCandidateGovernanceSummary;
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
