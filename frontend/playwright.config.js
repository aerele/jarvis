import { defineConfig, devices } from "@playwright/test";

// Real-browser mobile smoke. Boots the vite dev server, mocks the handful of
// Frappe boot calls in the specs (no bench needed), and checks the chat shell
// at phone widths. Browsers come from the shared ms-playwright cache; CI must
// run `npx playwright install chromium` first.
export default defineConfig({
	testDir: "./tests/e2e",
	timeout: 30000,
	fullyParallel: true,
	reporter: process.env.CI ? "list" : "line",
	use: {
		baseURL: "http://localhost:8080",
		trace: "on-first-retry",
	},
	// Use the system Google Chrome ("chrome" channel) so the suite runs against
	// an already-installed browser without a Playwright browser download.
	// --no-sandbox is required to launch Chrome inside CI containers.
	projects: [
		{
			name: "chromium",
			use: {
				...devices["Desktop Chrome"],
				channel: "chrome",
				launchOptions: { args: ["--no-sandbox"] },
			},
		},
	],
	webServer: {
		// frappe-ui's vite plugin derives the dev port from the bench web port
		// (8080 + (webserver_port - 8000)) and ignores --port. Pin the bench port
		// to 8000 so the dev server is deterministically on 8080 regardless of the
		// host bench. /api is mocked in the specs, so the proxy target is unused.
		command: "npm run dev",
		env: { FRAPPE_WEB_SERVER_PORT: "8000" },
		url: "http://localhost:8080",
		reuseExistingServer: !process.env.CI,
		timeout: 120000,
	},
});
