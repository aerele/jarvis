import assert from "node:assert/strict";
import { test } from "node:test";
import { renderMarkdown } from "./markdown.js";

// These are the stored result-note contracts checked by test_filebox_result_links.
// Exercise the real renderer: correct Python strings alone missed a final-dot URL bug.
for (const [name, target] of [
	["ST-1.", "ST-1%2E"],
	["ACME Pvt. Ltd.", "ACME%20Pvt%2E%20Ltd%2E"],
	["A..", "A%2E%2E"],
	["SUPP/001", "SUPP%2F001"],
	["X(1)", "X%281%29"],
]) {
	test(`saved File Box result keeps its exact Desk target: ${name}`, () => {
		const note = `Created Supplier [${name}](/app/supplier/${target}).`;
		const html = renderMarkdown(note + " Submit it from the document once it is reviewed.");
		assert.equal((html.match(/<a /g) || []).length, 1);
		assert.ok(html.includes(`href="/app/supplier/${target}"`));
		assert.ok(html.includes(`class="jv-md-link">${name}</a>.`));
		assert.ok(!html.includes("]("));
		assert.ok(html.includes("Submit it from the document once it is reviewed."));
	});
}
