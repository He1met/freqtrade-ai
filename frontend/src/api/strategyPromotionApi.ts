import { fetchJson } from "./http";

export type StrategyPromotionStatus = {
  status: "ELIGIBLE" | "BLOCKED" | "STALE" | string;
  reason: string | null;
  database_ids: {
    strategy_version_id: number;
    backtest_result_id: number;
    strategy_score_id: number;
  };
  policy: { policy_version: string; min_total_trades: number; max_drawdown_pct: number } | null;
  evidence: {
    net_of_costs?: boolean;
    out_of_sample?: { profit_pct?: number; total_trades?: number };
    walk_forward?: { market_states?: string[] };
  } | null;
  approval: {
    database_id: number;
    status: string;
    reason: string | null;
    policy_version: string;
    expires_at: string;
  } | null;
};

export function loadStrategyPromotionStatus(
  strategyVersionId: string,
  backtestResultId: string,
  strategyScoreId: string,
  signal?: AbortSignal,
): Promise<StrategyPromotionStatus> {
  const query = new URLSearchParams({
    strategy_version_id: strategyVersionId,
    backtest_result_id: backtestResultId,
    strategy_score_id: strategyScoreId,
  });
  return fetchJson<StrategyPromotionStatus>(`/strategy-promotions/evaluate?${query.toString()}`, signal);
}
