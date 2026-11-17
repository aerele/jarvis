import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { PRIVACY_NOTICE } from "../../../frontend/src/lib/privacyNotice.js";

test("PRIVACY_NOTICE: exact wording, no em-dash", () => {
	assert.equal(
		PRIVACY_NOTICE,
		"Your admin can see what you do in Jarvis: which records you read, export or change, and how much you use it. Not your messages."
	);
	assert.ok(!PRIVACY_NOTICE.includes("—"));
});

// The phone's Settings shows the notice. SettingsView.vue is not mountable under
// `node --test`, so these pin the source: the shared wording is imported and bound in
// the template, and the colour that clears the contrast bar is the one that is used.
const settingsView = readFileSync(new URL("../views/SettingsView.vue", import.meta.url), "utf8");

test("SettingsView renders the shared privacy notice with the jv-notice class", () => {
	assert.match(
		settingsView,
		/import\s*\{\s*PRIVACY_NOTICE\s*\}\s*from\s*"@shared\/lib\/privacyNotice\.js"/
	);
	assert.match(
		settingsView,
		/<p[^>]*class="[^"]*\bjv-notice\b[^"]*"[^>]*>\s*\{\{\s*PRIVACY_NOTICE\s*\}\}\s*<\/p>/
	);
});

test("SettingsView .jv-notice uses --ink6 (4.5:1 in both themes), not the dimmer --ink5", () => {
	const rule = /\.jv-notice\s*\{([^}]*)\}/.exec(settingsView);
	assert.ok(rule, ".jv-notice rule exists");
	const color = rule[1].replace(/\/\*[\s\S]*?\*\//g, "");
	assert.match(color, /color:\s*var\(--ink6\)/);
	assert.doesNotMatch(color, /--ink5/);
});
