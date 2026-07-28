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
		include: ["tests/support-extraction/**/*.test.js", "src/**/*.spec.js"],
		globals: true,
	},
});
