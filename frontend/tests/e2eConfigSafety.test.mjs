import assert from "node:assert/strict";
import { realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import test from "node:test";

import {
  safeAbsoluteDirectory,
  safePythonBinary,
} from "./helpers/e2eConfigSafety.ts";
import {
  acceptanceRepeatCount,
  allocateIsolatedPort,
  LAB_ACCEPTANCE_PROFILES,
  validateAcceptanceProfiles,
} from "./helpers/labAcceptanceProfiles.mjs";

test("E2E paths reject command substitution and backticks before shell construction", () => {
  for (const payload of ["/tmp/$(touch_pwned)", "/tmp/`touch_pwned`"]) {
    assert.throws(
      () => safeAbsoluteDirectory("E2E_TMP_PARENT", payload),
      /absolute shell-safe path/,
    );
    assert.throws(
      () => safePythonBinary(payload),
      /absolute shell-safe executable path/,
    );
  }
});

test("E2E path validation accepts a real absolute directory", () => {
  assert.equal(
    safeAbsoluteDirectory("E2E_TMP_PARENT", tmpdir()),
    realpathSync(tmpdir()),
  );
});

test("lab acceptance profiles are exact and unique", () => {
  assert.equal(validateAcceptanceProfiles(), LAB_ACCEPTANCE_PROFILES);
  assert.deepEqual(
    LAB_ACCEPTANCE_PROFILES.map((profile) => profile.name),
    ["complete-current", "empty", "missing-result", "missing-strategy", "long-evidence"],
  );
});

test("CI repeats the complete isolated acceptance matrix", () => {
  assert.equal(acceptanceRepeatCount({}), 1);
  assert.equal(acceptanceRepeatCount({ CI: "false" }), 1);
  assert.equal(acceptanceRepeatCount({ CI: "true" }), 2);
});

test("lab acceptance profile validation rejects duplicate authority", () => {
  assert.throws(
    () =>
      validateAcceptanceProfiles([
        { name: "one" },
        { name: "one" },
      ]),
    /names must be non-empty and unique/,
  );
});

test("isolated port allocation skips used and occupied candidates without reuse", async () => {
  const usedPorts = new Set([50_000]);
  const probed = [];
  const selected = await allocateIsolatedPort({
    usedPorts,
    start: 50_000,
    isAvailable: async (port) => {
      probed.push(port);
      return port === 50_002;
    },
  });
  assert.equal(selected, 50_002);
  assert.deepEqual(probed, [50_001, 50_002]);
  assert.deepEqual([...usedPorts], [50_000, 50_002]);
  await assert.rejects(
    allocateIsolatedPort({
      usedPorts,
      start: 20_000,
      isAvailable: async () => true,
    }),
    /outside the safe band/,
  );
});
