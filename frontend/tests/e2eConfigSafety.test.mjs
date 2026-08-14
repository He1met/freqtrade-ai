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

test("lab acceptance profiles use distinct ports outside the random E2E bands", () => {
  assert.equal(validateAcceptanceProfiles(), LAB_ACCEPTANCE_PROFILES);
  assert.deepEqual(
    LAB_ACCEPTANCE_PROFILES.map((profile) => profile.name),
    ["complete-current", "empty", "missing-result", "missing-strategy", "long-evidence"],
  );
  const ports = LAB_ACCEPTANCE_PROFILES.flatMap((profile) => [
    profile.backendPort,
    profile.frontendPort,
  ]);
  assert.equal(new Set(ports).size, ports.length);
  assert.equal(ports.some((port) => port >= 20_000 && port <= 40_000), false);
  assert.equal(ports.includes(8000) || ports.includes(5173), false);
});

test("CI repeats the complete isolated acceptance matrix", () => {
  assert.equal(acceptanceRepeatCount({}), 1);
  assert.equal(acceptanceRepeatCount({ CI: "false" }), 1);
  assert.equal(acceptanceRepeatCount({ CI: "true" }), 2);
});

test("lab acceptance port validation rejects duplicates and unsafe bands", () => {
  assert.throws(
    () =>
      validateAcceptanceProfiles([
        { name: "one", backendPort: 41_001, frontendPort: 42_001 },
        { name: "two", backendPort: 41_001, frontendPort: 42_002 },
      ]),
    /unique integers/,
  );
  assert.throws(
    () =>
      validateAcceptanceProfiles([
        { name: "one", backendPort: 20_001, frontendPort: 42_001 },
      ]),
    /isolated band/,
  );
});
