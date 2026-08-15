import { test } from "node:test";
import assert from "node:assert/strict";
import { LIGHT_VARS, DARK_VARS, isDark, nextTheme } from "./theme.js";

test("palettes expose the core vars used across views", () => {
	for (const v of ["--surface", "--border", "--text", "--cta", "--red", "--green", "--amber"])
		assert.ok(LIGHT_VARS[v] && DARK_VARS[v], `${v} present in both`);
});
test("isDark: explicit wins, system follows OS", () => {
	assert.equal(isDark("dark", false), true);
	assert.equal(isDark("light", true), false);
	assert.equal(isDark("system", true), true);
	assert.equal(isDark("system", false), false);
});

test("nextTheme: every click flips the effective appearance", () => {
	for (const prefersDark of [true, false]) {
		for (const cur of ["light", "dark", "system"]) {
			const next = nextTheme(cur, prefersDark);
			assert.notEqual(
				isDark(next, prefersDark),
				isDark(cur, prefersDark),
				`${cur} (OS ${prefersDark ? "dark" : "light"}) -> ${next} must flip`
			);
		}
	}
});
test("nextTheme: skips the no-op step, keeps system reachable", () => {
	// dark OS: dark -> system would render identically, so it skips to light
	assert.equal(nextTheme("dark", true), "light");
	assert.equal(nextTheme("light", true), "dark");
	assert.equal(nextTheme("system", true), "light");
	// light OS: dark -> system is a visible change (system renders light)
	assert.equal(nextTheme("dark", false), "system");
	assert.equal(nextTheme("light", false), "dark");
	// light OS: system -> light would render identically, so it skips to dark
	assert.equal(nextTheme("system", false), "dark");
});
