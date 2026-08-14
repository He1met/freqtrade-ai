import { defineConfig } from "@playwright/test";

const host = "127.0.0.1";
const port = Number(process.env.CANONICAL_V13_E2E_PORT ?? 41_789);
if (!Number.isInteger(port) || port < 1_024 || port > 65_535) {
  throw new Error("CANONICAL_V13_E2E_PORT must be an integer between 1024 and 65535");
}
const baseURL = `http://${host}:${port}`;

export default defineConfig({
  testDir: "./tests",
  testMatch: "canonicalV13.e2e.ts",
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  retries: 0,
  timeout: 30_000,
  use: {
    baseURL,
    browserName: "chromium",
    headless: true,
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 720 },
  },
  webServer: {
    command: `npm run dev -- --config tests/helpers/vite.canonical-v13.e2e.config.ts --port ${port} --strictPort`,
    cwd: ".",
    reuseExistingServer: false,
    timeout: 60_000,
    url: `${baseURL}/v13/strategies`,
  },
});
