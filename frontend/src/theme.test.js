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
test("nextTheme: strict two-state flip of the effective appearance", () => {
	assert.equal(nextTheme("dark", true), "light");
	assert.equal(nextTheme("light", true), "dark");
	// "system" leaves via whichever side it currently renders as
	assert.equal(nextTheme("system", true), "light");
	assert.equal(nextTheme("dark", false), "light");
	assert.equal(nextTheme("light", false), "dark");
	assert.equal(nextTheme("system", false), "dark");
});
