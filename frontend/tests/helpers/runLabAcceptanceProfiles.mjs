import { spawnSync } from "node:child_process";
import { createServer } from "node:net";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import {
  acceptanceRepeatCount,
  validateAcceptanceProfiles,
} from "./labAcceptanceProfiles.mjs";

const frontendRoot = dirname(dirname(dirname(fileURLToPath(import.meta.url))));
const playwrightCli = fileURLToPath(
  new URL("../../node_modules/@playwright/test/cli.js", import.meta.url),
);
const host = "127.0.0.1";

function portAvailable(port) {
  return new Promise((resolve) => {
    const server = createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
}

async function waitForReleased(port, phase) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (await portAvailable(port)) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`${phase} port ${port} was not released within 5 seconds`);
}

const profiles = validateAcceptanceProfiles();
const repeats = acceptanceRepeatCount();
for (let repeat = 1; repeat <= repeats; repeat += 1) {
  for (const profile of profiles) {
    await waitForReleased(profile.backendPort, "backend preflight");
    await waitForReleased(profile.frontendPort, "frontend preflight");
    const result = spawnSync(
      process.execPath,
      [
        playwrightCli,
        "test",
        "tests/localStrategyLabAcceptance.e2e.ts",
        "--project=desktop-1280x720",
      ],
      {
        cwd: frontendRoot,
        env: {
          ...process.env,
          E2E_SEED_PROFILE: profile.name,
          E2E_BACKEND_PORT: String(profile.backendPort),
          E2E_FRONTEND_PORT: String(profile.frontendPort),
        },
        stdio: "inherit",
      },
    );
    await waitForReleased(profile.backendPort, "backend teardown");
    await waitForReleased(profile.frontendPort, "frontend teardown");
    if (result.error) throw result.error;
    if (result.status !== 0) process.exit(result.status ?? 1);
    process.stdout.write(
      `acceptance profile ${profile.name} repeat ${repeat}/${repeats}: ports released\n`,
    );
  }
}
