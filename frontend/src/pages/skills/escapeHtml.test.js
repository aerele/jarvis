// Real executable test for the confirm-message HTML escaper (SAR-1). Plain node
// built-ins (node:test + node:assert) — no framework, the promotionBudget.test.js
// precedent. Run directly (`node --test escapeHtml.test.js`); node --test exits
// non-zero on any failed assertion.
import { test } from "node:test";
import assert from "node:assert/strict";
import { esc } from "./escapeHtml.js";

test("escapes every HTML-significant character", () => {
	assert.equal(esc("&"), "&amp;");
	assert.equal(esc("<"), "&lt;");
	assert.equal(esc(">"), "&gt;");
	assert.equal(esc('"'), "&quot;");
	assert.equal(esc("'"), "&#39;");
	// ampersand is escaped FIRST so the entity markers it emits aren't re-escaped
	assert.equal(esc("a & b < c"), "a &amp; b &lt; c");
});

test("neutralizes a full <img onerror> payload (no runnable markup survives)", () => {
	const out = esc("<img src=x onerror=alert(1)>");
	assert.doesNotMatch(out, /<img/);
	assert.doesNotMatch(out, /</);
	assert.doesNotMatch(out, />/);
	assert.equal(out, "&lt;img src=x onerror=alert(1)&gt;");
});

test("coerces null/undefined/numbers without throwing", () => {
	assert.equal(esc(null), "");
	assert.equal(esc(undefined), "");
	assert.equal(esc(42), "42");
});

test("leaves plain text untouched", () => {
	assert.equal(esc("Acme Corp pricing"), "Acme Corp pricing");
});
