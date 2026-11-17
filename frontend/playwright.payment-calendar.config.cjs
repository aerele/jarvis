const { defineConfig } = require("@playwright/test");

// Bench-less browser gate for the Payment Calendar (#821). A stateful mock serves the
// golden JSON of PR 1, so the real SPA renders in system Chrome with no site. One worker:
// every test installs its own clock and the dev server is shared.
// PCAL_PORT lets a local run avoid a port another session already serves; CI keeps 8080.
const port = Number(process.env.PCAL_PORT || 8080);
// frappe-ui derives the dev port from the bench web port: 8080 + (web port - 8000).
const webPort = 8000 + port - 8080;
const origin = `http://127.0.0.1:${port}`;

module.exports = defineConfig({
  testDir: "./tests/e2e",
  testMatch: "payment-calendar*.spec.cjs",
  timeout: 120000,
  workers: 1,
  reporter: "list",
  outputDir: "test-results/payment-calendar",
  use: {
    baseURL: origin,
    channel: "chrome",
    actionTimeout: 10000,
    trace: "retain-on-failure",
    launchOptions: { args: ["--no-sandbox"] },
  },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`,
    env: { FRAPPE_WEB_SERVER_PORT: String(webPort) },
    url: origin,
    reuseExistingServer: false,
    timeout: 90000,
  },
});
