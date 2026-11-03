const { defineConfig } = require("@playwright/test");

// Bench-less Board regression gate. The spec mocks the Frappe API; the shared
// card and full SPA still render in a real browser. CommonJS avoids the pinned
// Playwright ESM loader stalling on Node 24 during test discovery.
module.exports = defineConfig({
  testDir: "./tests/e2e",
  testMatch: "approval-layout.spec.cjs",
  timeout: 45000,
  workers: 1,
  reporter: "list",
  outputDir: "test-results/approval-layout",
  use: {
    baseURL: "http://127.0.0.1:8080",
    channel: "chrome",
    trace: "retain-on-failure",
    launchOptions: { args: ["--no-sandbox"] },
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1",
    env: { FRAPPE_WEB_SERVER_PORT: "8000" },
    url: "http://127.0.0.1:8080",
    reuseExistingServer: false,
    timeout: 60000,
  },
});
