import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

import { stripBlocks, BLOCK_TAGS } from "./chatBlocks";

/**
 * The regression this module exists for: while a reply streams, the closing
 * fence has not arrived, so a half-written ```jarvis-action block did not match
 * the strip pattern and marked rendered it as a code block. The reader watched a
 * wall of raw JSON type itself out and then vanish when the block closed.
 */

describe("stripBlocks, settled reply", () => {
	it("removes a completed block and keeps the prose around it", () => {
		const text = 'Here you go.\n\n```jarvis-action\n{"verb":"create"}\n```\n\nAnything else?';
		expect(stripBlocks(text)).toBe("Here you go.\n\nAnything else?");
	});

	it("removes every block type the agent can emit", () => {
		for (const tag of BLOCK_TAGS) {
			const text = "before\n\n```" + tag + '\n{"a":1}\n```\n\nafter';
			expect(stripBlocks(text)).toBe("before\n\nafter");
		}
	});

	it("removes a settled jarvis-goto block (jarvis#884: rendered as the redirect card, not raw JSON)", () => {
		const text =
			'Sure thing.\n\n```jarvis-goto\n{"page":"dashboards","prompt":"Monthly sales"}\n```\n\nOne moment.';
		expect(stripBlocks(text)).toBe("Sure thing.\n\nOne moment.");
	});

	it("removes a jarvis xychart but keeps an ordinary mermaid diagram", () => {
		const chart = "```mermaid\nxychart-beta\n  title x\n```";
		const diagram = "```mermaid\ngraph TD;\n  A-->B;\n```";
		expect(stripBlocks("t\n\n" + chart)).toBe("t");
		expect(stripBlocks("t\n\n" + diagram)).toContain("graph TD");
	});

	it("SHOWS an unterminated block once the reply has settled", () => {
		// A block the agent never closed is a real, if broken, answer. Hiding it
		// forever would leave the reader with a blank message and no explanation.
		const text = 'Working on it\n\n```jarvis-action\n{"verb":"create"';
		expect(stripBlocks(text, false)).toContain("jarvis-action");
	});

	it("collapses the gap a removed block leaves behind", () => {
		const text = "one\n\n\n```confirm\nx\n```\n\n\n\ntwo";
		expect(stripBlocks(text)).toBe("one\n\ntwo");
	});
});

describe("stripBlocks, mid-stream", () => {
	it("HOLDS an unterminated block instead of rendering it as raw JSON", () => {
		// THE bug. Same text, streaming: the reader sees the prose, not the payload.
		const text =
			'Creating that supplier.\n\n```jarvis-action\n{"verb":"create","doctype":"Sup';
		expect(stripBlocks(text, true)).toBe("Creating that supplier.");
	});

	it("holds an unterminated block of every type", () => {
		for (const tag of [...BLOCK_TAGS, "mermaid"]) {
			const text = "prose\n\n```" + tag + '\n{"half":';
			expect(stripBlocks(text, true)).toBe("prose");
		}
	});

	it("holds an unterminated jarvis-goto block, so a mid-stream reply never flashes the raw redirect JSON", () => {
		const text = 'One sec.\n\n```jarvis-goto\n{"page":"dashboards","prompt":"Mon';
		expect(stripBlocks(text, true)).toBe("One sec.");
	});

	it("holds a fence whose language tag is still being typed", () => {
		// "```jarv" has no newline yet, so it cannot be a plain code block. Rendering
		// it produces an empty grey slab that pops in and straight back out.
		expect(stripBlocks("prose\n\n```jarv", true)).toBe("prose");
		expect(stripBlocks("prose\n\n```", true)).toBe("prose");
	});

	it("still streams an ordinary code block, which the reader wants to watch", () => {
		// The newline proves the language is complete, so this is real content.
		const text = "Here:\n\n```js\nconst a = 1;";
		expect(stripBlocks(text, true)).toContain("const a = 1;");
	});

	it("holds only the trailing opener, never an earlier completed block's prose", () => {
		const text =
			'first\n\n```confirm\n{"ok":1}\n```\n\nsecond\n\n```jarvis-action\n{"partial"';
		expect(stripBlocks(text, true)).toBe("first\n\nsecond");
	});

	it("reveals the block's absence identically at every prefix of the stream", () => {
		// Character-by-character: the body must never flash the payload, at any
		// point in the stream. This is the property the fix actually promises.
		const full = 'Done.\n\n```jarvis-action\n{"verb":"create","doctype":"Supplier"}\n```';
		for (let i = 1; i <= full.length; i++) {
			const body = stripBlocks(full.slice(0, i), true);
			expect(body).not.toContain("jarvis-action");
			expect(body).not.toContain("doctype");
		}
	});

	it("does not mangle a reply with no blocks at all", () => {
		const text = "Just a plain answer with **bold** and a list:\n\n- one\n- two";
		expect(stripBlocks(text, true)).toBe(text);
	});

	it("tolerates empty and nullish input", () => {
		expect(stripBlocks("", true)).toBe("");
		expect(stripBlocks(null, true)).toBe("");
		expect(stripBlocks(undefined)).toBe("");
	});
});

describe("the view renders through this module", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

	it("passes the streaming flag, so a settled reply is not held forever", () => {
		expect(src).toContain('import { stripBlocks } from "@/lib/chatBlocks";');
		expect(src).toContain("renderMarkdown(stripBlocks(text, streaming))");
		expect(src).toContain("render(m.content, m.streaming)");
	});

	it("keeps the streaming flag in the render cache key", () => {
		// Without this the first (held) render of a text would be replayed for the
		// settled one, and the block would stay hidden after the turn ended.
		expect(src).toContain('const key = (streaming ? "S:" : "F:") + text;');
	});

	it("no longer carries its own copy of the strip patterns", () => {
		expect(src).not.toContain("```jarvis-action[ \\t]*\\n[\\s\\S]*?```");
	});
});
