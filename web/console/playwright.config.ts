import { defineConfig, devices } from "@playwright/test";

// The suite runs against the real services started by scripts/dev_stack.py (fresh databases
// and seeded demo data) and the Next.js console. Tests share that state, so they run in order.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://localhost:3000",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: "cd ../.. && uv run python -m scripts.dev_stack --prefix tally_e2e",
      url: "http://127.0.0.1:8040/health/live",
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
    },
    {
      command: "npm run dev",
      url: "http://localhost:3000",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
