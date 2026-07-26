import assert from "node:assert/strict";
import { realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import test from "node:test";

import {
  safeAbsoluteDirectory,
  safePythonBinary,
} from "./helpers/e2eConfigSafety.ts";

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
