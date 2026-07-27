import assert from "node:assert/strict";
import test from "node:test";

import {
  okxDemoAcceptanceIsTruthful,
  orderCanDisplayComplete,
} from "../src/pages/okxDemoDisplay.ts";
import { normalizeOkxDemoObservability } from "../src/api/okxDemoApi.ts";

function rawPayload() {
  return {
    generated_at: "2026-07-27T08:00:00Z",
    source_type: "api_aggregate",
    core_data: true,
    target: {
      target_id: "OKX_DEMO",
      label: "OKX_DEMO / 模拟盘",
      exchange: "okx",
      product_type: "SWAP",
      margin_mode: "isolated",
      account_mode: "demo",
      simulated_trading: true,
      allow_real_funds: false,
    },
    readiness: [
      "credentials",
      "instrument",
      "market",
      "risk",
      "writer",
      "reconciliation",
    ].map((key) => ({
      key,
      label: key,
      status: "READY",
      summary: "就绪",
      action: null,
      observed_at: "2026-07-27T08:00:00Z",
    })),
    intents: [{
      database_id: 1,
      intent_id: "a".repeat(64),
      client_order_id: "demo451",
      strategy_version_id: 10,
      instrument_id: "BTC-USDT-SWAP",
      side: "buy",
      position_side: "net",
      order_type: "market",
      quantity: "1",
      limit_price: null,
      leverage: "2",
      margin_mode: "isolated",
      reduce_only: false,
      status: "APPROVED",
      expires_at: null,
      created_at: "2026-07-27T08:00:00Z",
    }],
    orders: [{
      database_id: 2,
      trade_intent_database_id: 1,
      client_order_id: "demo451",
      exchange_order_id: "451",
      authoritative_snapshot_database_id: 5,
      authoritative_event_database_id: 6,
      full_chain_database_id: 20,
      instrument_id: "BTC-USDT-SWAP",
      side: "buy",
      order_type: "market",
      quantity: "1",
      status: "live",
      authoritative_status: "live",
      filled_quantity: "0",
      average_price: null,
      reduce_only: false,
      authoritative_observed_at: "2026-07-27T08:00:01Z",
      created_at: "2026-07-27T08:00:00Z",
      updated_at: "2026-07-27T08:00:01Z",
      completion_state: "COMPLETE",
      completion_reason: "证据完整",
      risk_decision: {
        database_id: 3,
        trade_intent_database_id: 1,
        decision: "APPROVED",
        policy_version: "risk-v1",
        created_at: "2026-07-27T08:00:00Z",
        reason: null,
      },
      fills: [{
        database_id: 21,
        exchange_fill_id: "fill451",
        price: "60000",
        quantity: "1",
        fee: "-0.1",
        created_at: "2026-07-27T08:00:01Z",
      }],
    }],
    positions: [],
    account: {
      status: "READY",
      reason: "权威账户快照已对账",
      database_id: 7,
      event_database_id: 8,
      equity: "10000",
      available_balance: "9000",
      margin_balance: "1000",
      observed_at: "2026-07-27T08:00:01Z",
    },
    latest_reconciliation: {
      database_id: 4,
      state_database_id: 9,
      status: "RECONCILED",
      opening_frozen: false,
      started_at: "2026-07-27T08:00:00Z",
      completed_at: "2026-07-27T08:00:02Z",
      authoritative_observed_at: "2026-07-27T08:00:01Z",
      artifact_status: "READY",
      source_type: "api_aggregate",
      core_data: true,
      reason: null,
    },
    lineage: [{
      full_chain_database_id: 20,
      strategy_generation_run_database_id: 10,
      strategy_database_id: 11,
      strategy_version_database_id: 12,
      backtest_run_database_id: 13,
      backtest_task_database_id: 14,
      backtest_result_database_id: 15,
      strategy_score_database_id: 16,
      candidate_approval_database_id: 17,
      signal_snapshot_database_id: 18,
      trade_intent_database_id: 1,
      risk_decision_database_id: 3,
      approved_execution_database_id: 19,
      order_database_id: 2,
      fill_database_id: 21,
      exchange_order_id: "451",
      authoritative_order_snapshot_database_id: 5,
      authoritative_event_database_id: 6,
      reconciliation_database_id: 4,
      reconciliation_state_database_id: 9,
    }],
    acceptance_state: "ACCEPTABLE",
    acceptance_reason: "证据完整",
  };
}

test("normalizer rejects a target that could be mistaken for live trading", () => {
  const payload = rawPayload();
  payload.target.account_mode = "live";
  assert.throws(
    () => normalizeOkxDemoObservability(payload),
    /目标或数据来源不符合安全契约/,
  );
});

test("completion requires database IDs, exchange ID, approved risk and completed reconciliation", () => {
  const data = normalizeOkxDemoObservability(rawPayload());
  assert.equal(orderCanDisplayComplete(data.orders[0], data), true);
  assert.equal(okxDemoAcceptanceIsTruthful(data), true);

  const withoutExchangeId = structuredClone(data.orders[0]);
  withoutExchangeId.exchangeOrderId = null;
  assert.equal(orderCanDisplayComplete(withoutExchangeId, data), false);

  const withoutReconciliation = { ...data, latestReconciliation: null };
  assert.equal(orderCanDisplayComplete(data.orders[0], withoutReconciliation), false);

  const withoutFullChain = structuredClone(data.orders[0]);
  withoutFullChain.fullChainDatabaseId = null;
  assert.equal(orderCanDisplayComplete(withoutFullChain, data), false);

  const withoutFill = structuredClone(data.orders[0]);
  withoutFill.fills = [];
  assert.equal(orderCanDisplayComplete(withoutFill, data), false);
});

test("empty results can never become acceptable", () => {
  const payload = rawPayload();
  payload.orders = [];
  payload.acceptance_state = "NOT_ACCEPTABLE";
  const data = normalizeOkxDemoObservability(payload);
  assert.equal(okxDemoAcceptanceIsTruthful(data), false);
});
