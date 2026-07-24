import assert from "node:assert/strict";
import test from "node:test";

import { buildSourceNoticeState, sourceNoticeDetails } from "../src/pages/sourceNoticeState.ts";

const context = "策略列表和版本来源。";

test("loading hides the source notice", () => {
  assert.equal(buildSourceNoticeState({ context, isLoading: true, source: "api" }).kind, "hidden");
});

test("healthy API state stays compact and makes empty-result semantics honest", () => {
  const notice = buildSourceNoticeState({ context, isLoading: false, source: "api" });

  assert.equal(notice.kind, "healthy");
  assert.equal(notice.summary, "Backend API 已连接");
  assert.match(notice.acceptance, /暂无真实记录/);
  assert.match(notice.acceptance, /不代表运行成功/);
  assert.doesNotMatch(sourceNoticeDetails(notice), /被过滤|补齐.*database_ids/);
});

test("fixture is explicit and never acceptable", () => {
  const notice = buildSourceNoticeState({ context, isLoading: false, source: "fixture" });

  assert.equal(notice.kind, "fixture");
  assert.match(notice.summary, /显式开发数据/);
  assert.match(notice.acceptance, /不可验收/);
  assert.match(sourceNoticeDetails(notice), /关闭 VITE_ENABLE_DEV_FIXTURES/);
});

test("failed source uses a concrete API error without inventing diagnostics", () => {
  const notice = buildSourceNoticeState({
    context,
    error: "HTTP 503",
    isLoading: false,
    source: "failed",
  });

  assert.equal(notice.kind, "failed");
  assert.equal(notice.summary, "HTTP 503");
  assert.equal(notice.reason, "HTTP 503");
  assert.doesNotMatch(sourceNoticeDetails(notice), /记录被过滤|缺失 ID/);
});

test("unknown failed state declares the current diagnostic limit", () => {
  const notice = buildSourceNoticeState({ context, isLoading: false, source: "failed" });

  assert.equal(notice.kind, "failed");
  assert.match(notice.reason, /无法诊断更具体原因/);
});
