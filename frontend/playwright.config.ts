import { defineConfig } from "@playwright/test";
import { randomBytes, randomInt } from "node:crypto";
import { tmpdir } from "node:os";
import { safeAbsoluteDirectory, safePythonBinary } from "./tests/helpers/e2eConfigSafety";

const host = "127.0.0.1";

function isolatedPort(name: string, fallback: number): number {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isInteger(value) || value < 1024 || value > 65535) {
    throw new Error(`${name} must be an integer between 1024 and 65535.`);
  }
  if (value === 8000 || value === 5173) {
    throw new Error(`${name} must not use the real runtime ports 8000 or 5173.`);
  }
  return value;
}

process.env.E2E_BACKEND_PORT ??= String(randomInt(20_000, 30_000));
process.env.E2E_FRONTEND_PORT ??= String(randomInt(30_001, 40_000));
const backendPort = isolatedPort("E2E_BACKEND_PORT", 0);
let frontendPort = isolatedPort("E2E_FRONTEND_PORT", 0);
if (backendPort === frontendPort) {
  process.env.E2E_FRONTEND_PORT = String(randomInt(40_001, 50_000));
  frontendPort = isolatedPort("E2E_FRONTEND_PORT", 0);
}

if (process.env.E2E_DATABASE_URL) {
  throw new Error("E2E_DATABASE_URL is forbidden; the acceptance wrapper allocates a new SQLite database.");
}
const acceptanceParent = safeAbsoluteDirectory(
  "E2E_TMP_PARENT",
  process.env.E2E_TMP_PARENT ?? tmpdir(),
);
process.env.E2E_TMP_PARENT = acceptanceParent;
process.env.E2E_ACCEPTANCE_REGISTRY ??=
  `${acceptanceParent}/freqtrade-ai-issue-433-registry-${randomBytes(12).toString("hex")}.json`;
const cleanupRegistry = process.env.E2E_ACCEPTANCE_REGISTRY;
if (!cleanupRegistry.startsWith(`${acceptanceParent}/freqtrade-ai-issue-433-registry-`) || !cleanupRegistry.endsWith(".json")) {
  throw new Error("E2E_ACCEPTANCE_REGISTRY is outside the controlled temporary parent.");
}
const pythonBin = safePythonBinary(process.env.PYTHON_BIN);
const backendProfile = process.env.E2E_SEED_PROFILE ?? "complete-current";
if (!["empty", "complete-current", "missing-result", "missing-strategy", "long-evidence"].includes(backendProfile)) {
  throw new Error("E2E_SEED_PROFILE is invalid.");
}
const baseURL = `http://${host}:${frontendPort}`;
const pythonArg = JSON.stringify(pythonBin);
const parentArg = JSON.stringify(acceptanceParent);
const profileArg = JSON.stringify(backendProfile);
const registryArg = JSON.stringify(cleanupRegistry);

export default defineConfig({
  testDir: "./tests",
  globalTeardown: "./tests/helpers/globalTeardown.ts",
  testMatch: "**/*.e2e.ts",
  forbidOnly: Boolean(process.env.CI),
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  use: {
    baseURL,
    browserName: "chromium",
    headless: true,
    permissions: ["clipboard-read", "clipboard-write"],
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop-1280x720",
      use: { viewport: { width: 1280, height: 720 } },
    },
    {
      name: "desktop-1440x900",
      use: { viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: [
    {
      command:
        `${pythonArg} ../scripts/run_local_strategy_lab_acceptance_server.py ` +
        `--parent ${parentArg} --host ${host} --port ${backendPort} --profile ${profileArg} ` +
        `--registry ${registryArg}`,
      cwd: "../backend",
      env: {
        ...process.env,
        APP_ENV: "phase8",
      },
      url: `http://${host}:${backendPort}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command:
        `npm run dev -- --config tests/helpers/vite.e2e.config.ts ` +
        `--port ${frontendPort} --strictPort`,
      cwd: ".",
      env: {
        ...process.env,
        E2E_BACKEND_PORT: String(backendPort),
      },
      url: `${baseURL}/local-strategy-lab`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
