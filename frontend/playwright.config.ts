import { defineConfig } from "@playwright/test";

const databaseUrl = process.env.DATABASE_URL;
const pythonBin = process.env.PYTHON_BIN ?? "python3";
const pythonCommand = JSON.stringify(pythonBin);

if (!databaseUrl) {
  throw new Error("DATABASE_URL is required for the Local Strategy Lab browser gate.");
}

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.e2e.ts",
  forbidOnly: Boolean(process.env.CI),
  fullyParallel: false,
  reporter: process.env.CI ? "github" : "list",
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: "http://127.0.0.1:5173",
    browserName: "chromium",
    headless: true,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 720 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: [
    {
      command: `${pythonCommand} -m uvicorn app.main:app --host 127.0.0.1 --port 8000`,
      cwd: "../backend",
      env: { ...process.env, DATABASE_URL: databaseUrl },
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "npm run dev -- --port 5173",
      cwd: ".",
      url: "http://127.0.0.1:5173/local-strategy-lab",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
