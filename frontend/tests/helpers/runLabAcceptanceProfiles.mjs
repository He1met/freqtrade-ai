import { spawnSync } from "node:child_process";
import { createServer } from "node:net";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import {
  acceptanceRepeatCount,
  allocateIsolatedPort,
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
const usedPorts = new Set();
for (let repeat = 1; repeat <= repeats; repeat += 1) {
  for (const profile of profiles) {
    const backendPort = await allocateIsolatedPort({
      usedPorts,
      isAvailable: portAvailable,
    });
    const frontendPort = await allocateIsolatedPort({
      usedPorts,
      isAvailable: portAvailable,
    });
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
          E2E_BACKEND_PORT: String(backendPort),
          E2E_FRONTEND_PORT: String(frontendPort),
        },
        stdio: "inherit",
      },
    );
    await waitForReleased(backendPort, "backend teardown");
    await waitForReleased(frontendPort, "frontend teardown");
    if (result.error) throw result.error;
    if (result.status !== 0) process.exit(result.status ?? 1);
    process.stdout.write(
      `acceptance profile ${profile.name} repeat ${repeat}/${repeats}: ` +
        `ports ${backendPort}/${frontendPort} released\n`,
    );
  }
}
