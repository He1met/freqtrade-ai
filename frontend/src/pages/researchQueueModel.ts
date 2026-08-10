import type {
  CandidateResearchQueueItem,
  CandidateResearchQueueRead,
  CandidateResearchQueueStatus,
} from "../api/candidateResearchQueueApi";
import type { StrategyResearchWorkspace } from "../api/strategyResearchApi";

export type ResearchQueueProjection = {
  available: boolean;
  asOf: string | null;
  batch: CandidateResearchQueueRead["batch"] | null;
  health: CandidateResearchQueueRead["health"] | null;
  active: CandidateResearchQueueItem | null;
  waiting: CandidateResearchQueueItem[];
  completed: CandidateResearchQueueItem[];
  fallbackReason: string | null;
};

export type ResearchQueueSort = "queue" | "generated-newest" | "generated-oldest" | "name";
export const TERMINAL_STATUSES: CandidateResearchQueueStatus[] = ["QUALIFIED", "REJECTED", "FAILED", "NO_ACTION"];

const LABELS: Record<CandidateResearchQueueStatus, string> = {
  ENQUEUED: "已入队", WAITING_FOR_LEASE: "等待领取", BACKTESTING: "回测中",
  VALIDATING: "验证中", QUALIFIED: "合格", REJECTED: "已拒绝", FAILED: "失败", NO_ACTION: "无动作",
};

export function researchQueueStatusLabel(status: CandidateResearchQueueStatus) { return LABELS[status] ?? "状态未知"; }
export function researchQueueStatusTone(status: CandidateResearchQueueStatus): "success" | "danger" | "warning" | "info" | "neutral" {
  if (status === "QUALIFIED") return "success";
  if (status === "FAILED") return "danger";
  if (status === "REJECTED") return "warning";
  if (status === "NO_ACTION") return "neutral";
  return "info";
}
export function researchQueueActionAdvice(status: CandidateResearchQueueStatus): string | null {
  if (status === "FAILED") return "检查失败阶段与证据完整性；仅在 owner/lease 契约允许时重试。";
  if (status === "REJECTED") return "质量门已给出终态；不要直接重试或降低门槛。";
  if (status === "NO_ACTION") return "这是合法无动作终态，无需按错误处理。";
  return null;
}

export function researchGenerationStatusLabel(status: CandidateResearchQueueRead["batch"]["generation_status"] | null): string {
  if (status === "NOT_GENERATED") return "未生成";
  if (status === "GENERATING") return "生成中";
  if (status === "GENERATED") return "已生成";
  return "数据暂不可用";
}

export function safeEvidenceHref(href: string | null): string | null {
  if (!href) return null;
  if (href.startsWith("/") && !href.startsWith("//")) return href;
  try { const url = new URL(href); return ["https:", "http:"].includes(url.protocol) ? href : null; }
  catch { return null; }
}

function legacyStatus(status: string): CandidateResearchQueueStatus {
  return status === "QUALIFIED" ? "QUALIFIED" : status === "REJECTED" ? "REJECTED" : "FAILED";
}

export function projectResearchQueue(queue: CandidateResearchQueueRead | null, workspace: StrategyResearchWorkspace | null, queueError: string | null): ResearchQueueProjection {
  if (queue) return {
    available: true, asOf: queue.as_of, batch: queue.batch, health: queue.health,
    active: queue.active_candidate, waiting: [...queue.waiting_candidates].sort(compareQueuePosition),
    completed: [...queue.completed_candidates], fallbackReason: null,
  };
  const batch = workspace?.latest_batch;
  const completed: CandidateResearchQueueItem[] = batch?.candidates.map((candidate) => ({
    candidate_id: String(candidate.id), candidate_name: candidate.candidate_name,
    pair: candidate.pair ?? null, timeframe: candidate.timeframe ?? null,
    generated_at: candidate.created_at ?? batch.created_at, queue_position: candidate.unit_slot ?? null,
    status: legacyStatus(candidate.status), current_step: "历史批次终态（非队列投影）",
    completed_steps: [], next_step: null, progress_percent: null, started_at: null,
    completed_at: batch.completed_at, elapsed_seconds: null, preceding_count: null, attempt: null,
    reason_code: candidate.rejection_reasons[0]?.code ?? null,
    reason_message: candidate.rejection_reasons[0]?.message ?? null,
    evidence: candidate.source_path ? [{ label: "源码证据", href: null, reference: candidate.source_path }] : [],
    actions: { cancel_available: false, retry_available: false, reason_code: "LEGACY_TERMINAL_READ_ONLY" },
  })) ?? [];
  return {
    available: false, asOf: workspace?.as_of ?? null, health: null, active: null, waiting: [], completed,
    batch: batch ? { run_id: batch.run_id, expected_count: 60, generation_status: "GENERATED", generated_count: batch.generated_count,
      enqueued_count: 0, active_count: 0, waiting_count: 0, completed_count: batch.candidates.length,
      remaining_count: Math.max(0, 60 - batch.candidates.length) } : null,
    fallbackReason: queueError || "WAITING_FOR_CANDIDATE_QUEUE_READ_API",
  };
}

function compareQueuePosition(a: CandidateResearchQueueItem, b: CandidateResearchQueueItem) {
  return (a.queue_position ?? Number.MAX_SAFE_INTEGER) - (b.queue_position ?? Number.MAX_SAFE_INTEGER)
    || a.candidate_name.localeCompare(b.candidate_name);
}

export type QueueFilters = { query: string; status: CandidateResearchQueueStatus | "ALL"; pair: string; timeframe: string; batch: string };
export function filterAndSortQueueItems(items: CandidateResearchQueueItem[], filters: QueueFilters, sort: ResearchQueueSort, batchId: string | null): CandidateResearchQueueItem[] {
  const query = filters.query.trim().toLocaleLowerCase();
  return items.filter((item) => {
    const haystack = [item.candidate_id, item.candidate_name, item.pair, item.timeframe].filter(Boolean).join(" ").toLocaleLowerCase();
    return (filters.status === "ALL" || item.status === filters.status)
      && (!filters.pair || item.pair === filters.pair) && (!filters.timeframe || item.timeframe === filters.timeframe)
      && (!filters.batch || filters.batch === batchId) && (!query || haystack.includes(query));
  }).sort((a, b) => {
    if (sort === "name") return a.candidate_name.localeCompare(b.candidate_name);
    if (sort === "generated-newest") return timestamp(b.generated_at) - timestamp(a.generated_at);
    if (sort === "generated-oldest") return timestamp(a.generated_at) - timestamp(b.generated_at);
    return compareQueuePosition(a, b);
  });
}
function timestamp(value: string | null) { const parsed = value ? Date.parse(value) : 0; return Number.isFinite(parsed) ? parsed : 0; }
export function terminalGroups(items: CandidateResearchQueueItem[]) { return TERMINAL_STATUSES.map((status) => ({ status, items: items.filter((item) => item.status === status) })); }
export function displayDuration(seconds: number | null): string {
  if (seconds === null || seconds < 0 || !Number.isFinite(seconds)) return "数据暂不可用";
  const whole = Math.floor(seconds), hours = Math.floor(whole / 3600), minutes = Math.floor((whole % 3600) / 60), remainder = whole % 60;
  return hours ? `${hours} 小时 ${minutes} 分` : minutes ? `${minutes} 分 ${remainder} 秒` : `${remainder} 秒`;
}
