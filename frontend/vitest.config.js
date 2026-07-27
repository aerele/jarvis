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
		include: ["tests/support-extraction/**/*.test.js"],
		globals: true,
	},
});
