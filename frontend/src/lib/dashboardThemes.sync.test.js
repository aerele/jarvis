import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { THEMES } from "./dashboardThemes.js"

// Sync guard: the render tokens in dashboardThemes.js (the --jd-* the shell
// injects) MUST match, byte-for-byte, the canonical theme.json specs in the
// jarvis app (jarvis/dashboards/themes/<key>/theme.json). The Python validator
// reads those specs for its allow-lists, so any drift here would validate saves
// against a spec the canvas doesn't actually render — this test fails loudly.

const CURATED = ["jarvis", "insight", "claude", "graphite"]

function loadSpec(key) {
	const url = new URL(`../../../jarvis/dashboards/themes/${key}/theme.json`, import.meta.url)
	return JSON.parse(readFileSync(url, "utf8"))
}

// Extract the --jd-* declarations from the first :root{} block of a theme's css.
function rootVars(css) {
	const m = /:root\{([\s\S]*?)\}/.exec(css)
	assert.ok(m, "theme css must contain a :root{} block")
	const out = {}
	for (const dm of m[1].matchAll(/--jd-([a-z0-9-]+)\s*:\s*([^;]+);/g)) {
		out[dm[1]] = dm[2].trim()
	}
	return out
}

for (const key of CURATED) {
	test(`theme.json <-> dashboardThemes.js sync: ${key}`, () => {
		const spec = loadSpec(key)
		const theme = THEMES[key]
		assert.ok(theme, `THEMES.${key} exists`)

		// identity fields
		assert.equal(theme.key, spec.key)
		assert.equal(theme.label, spec.label)
		assert.equal(theme.dark, spec.dark)
		assert.equal(theme.accent, spec.accent)
		assert.deepEqual(theme.palette, spec.palette)

		// every render token in the spec is emitted with the exact same value
		const vars = rootVars(theme.css)
		for (const [name, value] of Object.entries(spec.render_tokens)) {
			assert.equal(vars[name], value, `--jd-${name} value drifted for ${key}`)
		}
		// and the css emits no --jd-* token the spec doesn't declare
		for (const name of Object.keys(vars)) {
			assert.ok(name in spec.render_tokens, `--jd-${name} in css but not in ${key}/theme.json`)
		}
	})
}

test("Custom theme is bespoke and has no theme.json (validator relaxes design)", () => {
	assert.ok(THEMES.custom, "THEMES.custom exists")
	assert.equal(THEMES.custom.bespoke, true)
	// It is intentionally NOT in the curated/synced set.
	assert.ok(!CURATED.includes("custom"))
})
