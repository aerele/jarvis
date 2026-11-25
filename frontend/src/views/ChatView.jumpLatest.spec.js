import fs from "node:fs";
import { compile } from "@vue/compiler-dom";
import { mount } from "@vue/test-utils";
import * as Vue from "vue";
import { describe, expect, it, vi } from "vitest";

// The jump-to-latest control must be a strip between the thread and the composer, never an
// overlay: an overlay sat on top of the export card's Cancel button at 375 and 320 px wide.
// Mount the real template slice without starting the unrelated chat engine; the layout
// itself (no scroll position can put thread content under the button) is checked in Chrome.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
const slice = source.match(/<transition name="jv-sd">[\s\S]*?<\/transition>/)[0];
const { code } = compile(`<div>${slice}</div>`, { mode: "function", prefixIdentifiers: true });
const render = new Function("Vue", code)(Vue);

function show(over = {}, jumpToBottom = vi.fn()) {
	const state = { showScrollDown: true, showWelcome: false, booting: false, ...over };
	const wrapper = mount({ setup: () => ({ ...state, jumpToBottom }), render });
	return { wrapper, jumpToBottom };
}

function cssRule(selector) {
	const start = source.indexOf(`\n${selector} {`);
	expect(start, `${selector} rule`).toBeGreaterThan(-1);
	return source.slice(start, source.indexOf("}", start));
}

describe("ChatView jump-to-latest strip", () => {
	it.each([
		["the thread is at the bottom", { showScrollDown: false }],
		["the welcome screen is showing", { showWelcome: true }],
		["the chat is still booting", { booting: true }],
	])("renders nothing while %s", (_, over) => {
		const { wrapper } = show(over);
		expect(wrapper.find(".jv-jump-strip").exists()).toBe(false);
		expect(wrapper.find(".jv-scrolldown").exists()).toBe(false);
	});

	it("renders the button inside the strip when the reader has scrolled up", () => {
		const { wrapper } = show();
		expect(wrapper.find(".jv-jump-strip .jv-scrolldown").exists()).toBe(true);
	});

	it("is a real, keyboard-reachable button that jumps once per click", async () => {
		const { wrapper, jumpToBottom } = show();
		const button = wrapper.find(".jv-scrolldown");
		expect(button.element.tagName).toBe("BUTTON");
		expect(button.attributes("aria-label")).toBe("Jump to latest message");
		expect(button.attributes("title")).toBe("Jump to latest");
		expect(button.attributes("tabindex")).not.toBe("-1");
		await button.trigger("click");
		expect(jumpToBottom).toHaveBeenCalledTimes(1);
	});

	it("sits between the thread and the composer, not inside the composer", () => {
		const thread = source.indexOf('ref="threadEl"');
		const strip = source.indexOf('class="jv-jump-strip"');
		const button = source.indexOf('class="jv-scrolldown"');
		const composer = source.indexOf('class="jv-composer-wrap"');
		expect(thread).toBeGreaterThan(-1);
		expect(strip).toBeGreaterThan(thread);
		expect(button).toBeGreaterThan(strip);
		expect(composer).toBeGreaterThan(button);
		expect(source.indexOf('class="jv-scrolldown"', composer)).toBe(-1);
	});

	it("takes real layout space and is never positioned over the thread", () => {
		const strip = cssRule(".jv-jump-strip");
		const button = cssRule(".jv-scrolldown");
		expect(strip).toMatch(/flex:\s*none/);
		for (const rule of [strip, button]) {
			expect(rule).not.toMatch(/position:\s*(absolute|fixed|sticky)/);
			expect(rule).not.toMatch(/bottom:\s*100%/);
		}
	});
});
