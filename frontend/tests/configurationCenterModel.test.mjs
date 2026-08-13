import assert from "node:assert/strict";
import test from "node:test";

import {
  canCreateDraftFromVersion,
  defaultValueForSchema,
  editorCapability,
  safetyCapabilities,
  versionActions,
} from "../src/pages/configurationCenterModel.ts";

const strictSchema = {
  type: "object",
  properties: {
    candidate_count: { type: "integer", minimum: 1 },
    demo_only: { type: "boolean", const: true },
    targets: { type: "array", items: { type: "string" } },
  },
  required: ["candidate_count", "demo_only", "targets"],
  additionalProperties: false,
};

function typeWith(capability, handler = "generic-json-v1") {
  return {
    type_key: "research-profile",
    name_zh: "研究装配",
    description_zh: "test",
    schema_version: "v1",
    handler_key: handler,
    editor_capability: capability,
    enabled: true,
  };
}

test("configuration editor is writable only for an explicit strict safe schema", () => {
  assert.equal(editorCapability(typeWith({ write_enabled: true, json_schema: strictSchema })).writable, true);
  assert.equal(editorCapability(typeWith({ write_enabled: true, json_schema: { ...strictSchema, additionalProperties: true } })).writable, false);
  assert.equal(editorCapability(typeWith({ write_enabled: true, json_schema: {
    type: "object",
    properties: { api_key: { type: "string" } },
    required: [],
    additionalProperties: false,
  } })).writable, false);
  assert.equal(editorCapability(typeWith({ write_enabled: true, json_schema: strictSchema }, "unknown-handler")).writable, false);
});

test("schema defaults preserve safety constants without inventing hidden business fallbacks", () => {
  assert.deepEqual(defaultValueForSchema(strictSchema), {
    candidate_count: 1,
    demo_only: true,
    targets: [],
  });
});

test("lifecycle actions keep active and immutable history distinct", () => {
  const draft = { id: 1, lifecycle_status: "DRAFT" };
  const validated = { id: 2, lifecycle_status: "VALIDATED" };
  const retired = { id: 3, lifecycle_status: "RETIRED" };
  assert.deepEqual(versionActions(draft, 9), { canActivate: false, canRetire: false, canValidate: true });
  assert.deepEqual(versionActions(validated, 9), { canActivate: true, canRetire: true, canValidate: false });
  assert.deepEqual(versionActions(validated, 2), { canActivate: false, canRetire: false, canValidate: false });
  assert.deepEqual(versionActions(retired, 9), { canActivate: false, canRetire: false, canValidate: false });
  assert.equal(canCreateDraftFromVersion(null), true);
  assert.equal(canCreateDraftFromVersion(draft), false);
  assert.equal(canCreateDraftFromVersion(validated), true);
  assert.equal(canCreateDraftFromVersion(retired), true);
});

test("safety capability display never turns missing evidence into false", () => {
  assert.deepEqual(safetyCapabilities(null, null).map((item) => item.value), ["UNKNOWN", "UNKNOWN", "UNKNOWN"]);
  assert.deepEqual(safetyCapabilities(null, {
    demo_only: true,
    allow_real_funds: false,
    single_writer_required: true,
  }).map((item) => item.value), [true, false, true]);
});
