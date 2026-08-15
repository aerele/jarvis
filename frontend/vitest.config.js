import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import path from "path";

// `src/theme.js` (and other app modules the extracted components will pull
// in) import via the `@/…` alias that vite.config.js defines for the real
// build. vitest doesn't share that config, so it's redeclared here — without
// it, importing theme.js fails to resolve `@/api`.
export default defineConfig({
	plugins: [vue()],
	resolve: {
		alias: {
			"@": path.resolve(__dirname, "src"),
		},
	},
	test: {
		environment: "jsdom",
		// Two frontend test conventions coexist in this repo, so the suffix is
		// what keeps them apart:
		//   *.test.js  co-located under src/ -> node's built-in runner
		//              (`node --test`, plain node:test + node:assert, no bundler)
		//   *.spec.js  -> vitest, for anything needing a bundler or a DOM,
		//              i.e. mounting .vue components
		// Collecting src/**/*.test.js here instead would drag in every node:test
		// file and fail them all with "No test suite found", because vitest does
		// not understand node:test.
		// tests/**/*.spec.js is the same *.spec.js convention, for a suite whose
		// component doubles are shared across several files (tests/list-filters):
		// the shared double belongs beside its specs, not in src/ where nothing in
		// the app would ever import it (same reasoning as support-extraction's
		// fixtures.js). It also picks up repo-level guards at the top level, like
		// tests/mobile-responsive.spec.js.
		include: [
			"tests/support-extraction/**/*.test.js",
			"tests/**/*.spec.js",
			"src/**/*.spec.js",
		],
		// tests/e2e/** holds Playwright specs (also *.spec.js) that must NOT run
		// under vitest — they drive a real browser through @playwright/test.
		exclude: ["node_modules/**", "tests/e2e/**"],
		globals: true,
	},
});
