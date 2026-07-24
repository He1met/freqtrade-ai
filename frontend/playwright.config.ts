import { defineConfig } from "@playwright/test";

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

const backendPort = isolatedPort("E2E_BACKEND_PORT", 18108);
const frontendPort = isolatedPort("E2E_FRONTEND_PORT", 15178);
if (backendPort === frontendPort) {
  throw new Error("E2E_BACKEND_PORT and E2E_FRONTEND_PORT must be different.");
}

const databaseUrl =
  process.env.E2E_DATABASE_URL ??
  "sqlite+pysqlite:////tmp/freqtrade-ai-issue-408-desktop-e2e.sqlite";
if (process.env.DATABASE_URL && databaseUrl === process.env.DATABASE_URL) {
  throw new Error("E2E_DATABASE_URL must be independent from the real runtime DATABASE_URL.");
}

const pythonBin = process.env.PYTHON_BIN ?? "python3";
const pythonCommand = JSON.stringify(pythonBin);
const databaseArgument = JSON.stringify(databaseUrl);
const evidenceDir = JSON.stringify(
  process.env.E2E_EVIDENCE_DIR ?? "/tmp/freqtrade-ai-issue-408-desktop-e2e",
);
const baseURL = `http://${host}:${frontendPort}`;

export default defineConfig({
  testDir: "./tests",
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
        `${pythonCommand} ../scripts/smoke_phase8.py --offline --skip-frontend ` +
        `--database-url ${databaseArgument} --tmp-dir ${evidenceDir} && ` +
        `${pythonCommand} -m uvicorn app.main:app --host ${host} --port ${backendPort}`,
      cwd: "../backend",
      env: {
        ...process.env,
        APP_ENV: "phase8",
        DATABASE_URL: databaseUrl,
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
