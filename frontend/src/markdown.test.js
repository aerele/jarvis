import { test } from "node:test";
import assert from "node:assert/strict";

import { renderMarkdown } from "./markdown.js";

// Agents line-break a Markdown TABLE CELL with <br> (GFM has no other way), and the
// same suggestion text is also stored as a Frappe timeline Comment where <br> is the
// correct, rendered form. The compact renderer escapes all HTML first (XSS-safe), which
// turned that <br> into a literal "&lt;br&gt;" in the chat bubble. It must render the
// bare, inert tag while keeping everything else — including <br> WITH attributes — escaped.

test("renders a bare <br> inside a table cell as a line break, not literal text", () => {
	const md = [
		"| Leave Application | Comment |",
		"| --- | --- |",
		"| HR-LAP-2026-00314 | Approve.<br>Reason: Not feeling well |",
	].join("\n");
	const html = renderMarkdown(md);
	assert.ok(html.includes("Approve.<br>Reason: Not feeling well"), "the <br> renders as a tag");
	assert.ok(!html.includes("&lt;br&gt;"), "no literal &lt;br&gt; leaks through");
});

test("renders the <br/> and <br /> self-closing variants too", () => {
	assert.ok(renderMarkdown("line one<br/>line two").includes("line one<br>line two"));
	assert.ok(renderMarkdown("line one<br />line two").includes("line one<br>line two"));
});

test("keeps non-<br> HTML escaped (stays XSS-safe)", () => {
	const html = renderMarkdown("<script>alert(1)</script>");
	assert.ok(html.includes("&lt;script&gt;"), "script tag stays escaped");
	assert.ok(!html.includes("<script>"), "no raw script element");
});

test("keeps a <br> carrying attributes escaped — only the inert bare tag is allowed", () => {
	const html = renderMarkdown('before<br onload="alert(1)">after');
	assert.ok(!html.includes("<br onload"), "attribute-bearing br is not un-escaped");
	assert.ok(html.includes("&lt;br onload"), "it stays escaped");
});
