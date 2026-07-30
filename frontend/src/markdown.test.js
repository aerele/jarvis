import { test } from "node:test";
import assert from "node:assert/strict";

import { renderMarkdown } from "./markdown.js";

// Agents line-break a Markdown TABLE CELL with <br> (GFM has no other way), and the
// same suggestion text is also stored as a Frappe timeline Comment where <br> is the
// correct, rendered form. The compact renderer escapes all HTML first (XSS-safe), which
// turned that <br> into a literal "&lt;br&gt;" in the chat bubble. It must render the
// bare, inert tag while keeping everything else — including <br> WITH attributes — escaped,
// and must NOT leak into content that should render verbatim (inline code spans).

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

test("renders EVERY <br> in a multi-line cell, not just the first (the motivating case)", () => {
	// "suggestion / reason / note" = 3 lines, 2 breaks. Would fail if the /g flag were dropped.
	const cell = "Approve.<br>Reason: unwell<br>Note: rest well";
	const html = renderMarkdown(["| A | B |", "| - | - |", `| x | ${cell} |`].join("\n"));
	assert.equal((html.match(/<br>/g) || []).length, 2, "both <br> converted, not just the first");
	assert.ok(!html.includes("&lt;br&gt;"));
});

test("renders <br/>, <br /> and <BR> variants (self-closing + case-insensitive)", () => {
	assert.ok(renderMarkdown("line one<br/>line two").includes("line one<br>line two"));
	assert.ok(renderMarkdown("line one<br />line two").includes("line one<br>line two"));
	assert.ok(
		renderMarkdown("line one<BR>line two").includes("line one<br>line two"),
		"case-insensitive"
	);
});

test("keeps non-<br> HTML escaped (stays XSS-safe)", () => {
	const html = renderMarkdown("<script>alert(1)</script>");
	assert.ok(html.includes("&lt;script&gt;"), "script tag stays escaped");
	assert.ok(!html.includes("<script>"), "no raw script element");
});

test("keeps a <br> carrying attributes escaped — quoted, unquoted, AND boolean", () => {
	// Only the bare, attribute-less tag is re-permitted. The unquoted/boolean cases are the
	// strong ones: their payloads have no " to be entity-escaped, so they anchor the real
	// "no non-space/slash char before the closing >" rule, not an incidental quote side effect.
	const quoted = renderMarkdown('before<br onload="alert(1)">after');
	assert.ok(!quoted.includes("<br onload"), "quoted-attr br stays escaped");
	assert.ok(quoted.includes("&lt;br onload"));
	const unquoted = renderMarkdown("before<br onmouseover=alert(1)>after");
	assert.ok(!unquoted.includes("<br onmouseover"), "unquoted-attr br stays escaped");
	assert.ok(unquoted.includes("&lt;br onmouseover"));
	const boolAttr = renderMarkdown("before<br hidden>after");
	assert.ok(!boolAttr.includes("<br hidden"), "boolean-attr br stays escaped");
});

test("does NOT over-match malformed spacing (< br >, <br/ >) — stays escaped", () => {
	assert.ok(renderMarkdown("a< br >b").includes("&lt; br &gt;"), "space after < is not a br");
	assert.ok(!renderMarkdown("a< br >b").includes("<br>"));
	assert.ok(renderMarkdown("a<br/ >b").includes("&lt;br/ &gt;"), "slash-then-space is not a br");
});

test("code spans render VERBATIM — <br> and markdown inside backticks stay literal", () => {
	// The <br> re-permit (and bold/em/del/link) must not leak into inline code.
	const brInCode = renderMarkdown("use `<br>` to break a line");
	assert.ok(brInCode.includes("<code"), "there is a code span");
	assert.ok(
		brInCode.includes("&lt;br&gt;</code>"),
		"the <br> is literal text inside the code span"
	);
	assert.ok(!/<code[^>]*><br>/.test(brInCode), "no real <br> element inside the code span");
	const mdInCode = renderMarkdown("literally `a*b*c` here");
	assert.ok(mdInCode.includes("a*b*c"), "the * stays literal inside code");
	assert.ok(!mdInCode.includes("<em>"), "no stray <em> leaking from inside the code span");
});

test("fenced code blocks keep <br> literal (the safety contrast inline() relies on)", () => {
	const html = renderMarkdown(["```", "<br>", "```"].join("\n"));
	assert.ok(html.includes("&lt;br&gt;"), "fenced code keeps it escaped");
	assert.ok(!html.includes("<br>"), "no real <br> in a code fence");
});

test("strips NUL so crafted input can't collide with the internal code-span sentinel", () => {
	const NUL = String.fromCharCode(0);
	const html = renderMarkdown(`plain ${NUL}C0${NUL} text`);
	assert.ok(!html.includes("<code"), "no spurious code span from an injected sentinel");
	assert.ok(!html.includes("undefined"), "no phantom code-index leak");
});
