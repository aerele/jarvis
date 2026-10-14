// ChatView is too large to mount for one button, so the copy rules live in
// richCopy.js (richCopy.spec.js) and this pins that ChatView actually uses them:
// both reply Copy buttons go through copyReplyMsg, which renders the reply and
// hands it to copyReply, and document links escape their DocType.
import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

function body(name) {
	const start = src.indexOf(`function ${name}(`);
	expect(start, `${name} not found`).toBeGreaterThan(0);
	const end = src.indexOf("\nfunction ", start + 1);
	return src.slice(start, end > 0 ? end : undefined);
}

describe("reply Copy wiring", () => {
	it("both reply Copy buttons use the rendered copy", () => {
		expect(src.match(/@copy="copyReplyMsg\(m\.name, m\.content\)"/g)).toHaveLength(1);
		expect(src.match(/@click="copyReplyMsg\(m\.name, m\.content\)"/g)).toHaveLength(1);
		expect(src).not.toMatch(/copyMsg\(m\.name, stripBlocks\(m\.content\)\)/);
	});
	it("the user's own message still copies its raw text", () => {
		expect(src).toMatch(/@copy="copyMsg\(m\.name, m\.content\)"/);
		expect(body("copyMsg")).toMatch(/copyText\(text\);\s*showCopied\(id\);/);
	});
	it("copyReplyMsg renders as the settled bubble does and ticks only on success", () => {
		const fn = body("copyReplyMsg");
		expect(fn).toMatch(/copyReply\(id, content, \{/);
		expect(fn).toMatch(/render: \(text\) => render\(text, false\)/);
		expect(fn).toMatch(/markdown: \(text\) => stripBlocks\(text\)/);
		expect(fn).toMatch(/onCopied: showCopied/);
		expect(fn).toMatch(/reportError\(/);
	});
	it("the reply Copy button is named for assistive tech", () => {
		expect(src).toMatch(/:aria-label="replyCopyLabel\(m\.name\)"/);
	});
	it("document links escape the DocType they put in the page", () => {
		const fn = body("linkifyDocs");
		expect(fn).toMatch(/title="Open \$\{_escAttr\(\s*dt\s*\)\} in ERPNext"/);
		expect(fn).toMatch(/encodeURIComponent\(_deskSlug\(dt\)\)/);
		expect(fn).not.toMatch(/title="Open \$\{dt\}/);
	});
});
