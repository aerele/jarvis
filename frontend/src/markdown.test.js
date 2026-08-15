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

// The motivating bug: an agent reply cites a Desk location as inline code, e.g.
// `/app/dashboard-view/Customer-Item Manufacturing Priority`. Rendered as plain
// code text it read as inert, so a user who'd just had that dashboard created
// missed the link and asked for it again (conversation ves2j96q81, seq 36).

test("a code span that is EXACTLY a site-relative /app/... path renders as a clickable link", () => {
	const html = renderMarkdown(
		"Here you go: `/app/dashboard-view/Customer-Item Manufacturing Priority`"
	);
	assert.ok(!html.includes("<code"), "no code span left for a pure app-path");
	assert.ok(
		html.includes(
			'<a href="/app/dashboard-view/Customer-Item%20Manufacturing%20Priority" target="_blank" rel="noopener" class="jv-md-link">/app/dashboard-view/Customer-Item Manufacturing Priority</a>'
		),
		"renders a jv-md-link with spaces percent-encoded in the href but not the visible text"
	);
});

test("a code span that merely CONTAINS an app path alongside other text stays a plain code span", () => {
	const html = renderMarkdown("run `cd /app/foo && ls` first");
	assert.ok(html.includes("<code"), "still a code span");
	assert.ok(!html.includes("<a "), "not linkified — it's not EXACTLY a path");
});

test("an ordinary code span unrelated to /app/ paths is untouched", () => {
	const html = renderMarkdown("use `git status` to check");
	assert.ok(html.includes('<code class="jv-md-code">git status</code>'));
});

test("a bare /app/... path in plain prose (no backticks) also becomes a clickable link", () => {
	const html = renderMarkdown("It's live at /app/sales-invoice/SINV-2026-00042 now.");
	assert.ok(
		html.includes(
			'<a href="/app/sales-invoice/SINV-2026-00042" target="_blank" rel="noopener" class="jv-md-link">/app/sales-invoice/SINV-2026-00042</a>'
		)
	);
	assert.ok(html.includes("now."), "trailing sentence punctuation stays outside the link text");
	assert.ok(!html.includes("00042.</a>"), "the trailing period is not swallowed into the link");
});

test("an external URL is left to its own link handling, not mistaken for a site-relative path", () => {
	const html = renderMarkdown("See [the dashboard](https://example.com/app/foo) for details.");
	assert.ok(html.includes('<a href="https://example.com/app/foo"'), "markdown link untouched");
	assert.ok((html.match(/<a /g) || []).length === 1, "no second, spurious link was created");
});

test("bare app-path linking does not fire inside an existing <a> tag's own path segment", () => {
	const html = renderMarkdown("Visit https://other.example/app/thing directly.");
	assert.ok(!html.includes('href="/app/thing"'), "the /app/ inside a full URL is not re-linked");
});

test("a bare path immediately abutting a code span does not swallow the code-span sentinel", () => {
	// Regression: the bare-path stash runs after code spans are stashed behind a NUL
	// sentinel, so an un-excluded sentinel char in the path's match class would pull
	// the sentinel into the href and corrupt both the link and the code span.
	const html = renderMarkdown("see /app/x`y`");
	assert.ok(
		html.includes('<a href="/app/x"'),
		"the bare path links cleanly, without the sentinel"
	);
	assert.ok(html.includes('<code class="jv-md-code">y</code>'), "the code span restores intact");
});
