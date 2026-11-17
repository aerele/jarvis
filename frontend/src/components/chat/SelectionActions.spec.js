// aerele/jarvis#2191: the bar over text selected in a reply, and the user bubble
// drawing a Reply's quote.
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import SelectionActions from "./SelectionActions.vue";
import Message from "./Message.vue";

const SELECTOR = ".jv-amsg .jv-md-body";

describe("SelectionActions", () => {
	let root;
	let wrapper;

	beforeEach(() => {
		vi.useFakeTimers();
		root = document.createElement("div");
		root.innerHTML = `
			<div class="jv-amsg"><div class="jv-md-body"><p id="a">Row 3: Acme, 1,200</p></div></div>
			<div class="jv-amsg"><div class="jv-md-body"><p id="b">Second reply</p></div></div>
			<div class="jv-umsg"><p id="u">my own message</p></div>`;
		document.body.appendChild(root);
		// jsdom lays nothing out (and Range has no getBoundingClientRect): give every
		// range a box so the bar can be placed.
		Range.prototype.getBoundingClientRect = () => ({
			top: 200,
			bottom: 220,
			left: 100,
			width: 120,
			height: 20,
		});
		wrapper = mount(SelectionActions, {
			props: { root, selector: SELECTOR, actions: [{ key: "reply", label: "Reply" }] },
			attachTo: document.body,
		});
	});

	afterEach(() => {
		wrapper.unmount();
		root.remove();
		window.getSelection().removeAllRanges();
		delete Range.prototype.getBoundingClientRect;
		vi.useRealTimers();
	});

	function select(startId, endId = startId) {
		const range = document.createRange();
		range.setStart(document.getElementById(startId).firstChild, 0);
		const end = document.getElementById(endId).firstChild;
		range.setEnd(end, end.length);
		const sel = window.getSelection();
		sel.removeAllRanges();
		sel.addRange(range);
	}

	async function release() {
		document.dispatchEvent(new MouseEvent("mouseup"));
		vi.runAllTimers();
		await nextTick();
	}

	test("text selected in one reply shows the bar above it", async () => {
		select("a");
		await release();
		const bar = wrapper.find(".jv-selbar");
		expect(bar.exists()).toBe(true);
		expect(bar.attributes("style")).toContain("top: 156px");
		expect(bar.text()).toBe("Reply");
	});

	test("Reply emits the selected text and clears the selection", async () => {
		select("a");
		await release();
		await wrapper.find(".jv-selbar-btn").trigger("click");
		expect(wrapper.emitted("action")).toEqual([["reply", "Row 3: Acme, 1,200"]]);
		expect(window.getSelection().isCollapsed).toBe(true);
		expect(wrapper.find(".jv-selbar").exists()).toBe(false);
	});

	test("a selection across two replies or outside a reply shows nothing", async () => {
		select("a", "b");
		await release();
		expect(wrapper.find(".jv-selbar").exists()).toBe(false);
		select("u");
		await release();
		expect(wrapper.find(".jv-selbar").exists()).toBe(false);
	});

	test("on a touch screen the bar shows under a settled selection and a tap picks", async () => {
		await wrapper.setProps({ below: true });
		document.dispatchEvent(new Event("touchstart"));
		select("a");
		document.dispatchEvent(new Event("selectionchange"));
		vi.advanceTimersByTime(500);
		await nextTick();
		const bar = wrapper.find(".jv-selbar");
		expect(bar.exists()).toBe(true);
		expect(bar.attributes("style")).toContain("top: 234px");
		await wrapper.find(".jv-selbar-btn").trigger("touchend");
		expect(wrapper.emitted("action")).toEqual([["reply", "Row 3: Acme, 1,200"]]);
	});

	test("Escape and a collapsed selection hide the bar", async () => {
		select("a");
		await release();
		document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
		await nextTick();
		expect(wrapper.find(".jv-selbar").exists()).toBe(false);
		// Releasing Escape does not bring it back (the text is still selected).
		document.dispatchEvent(new KeyboardEvent("keyup", { key: "Escape" }));
		vi.runAllTimers();
		await nextTick();
		expect(wrapper.find(".jv-selbar").exists()).toBe(false);
		// A keyboard selection still shows it on key release.
		document.dispatchEvent(new KeyboardEvent("keyup", { key: "ArrowRight", shiftKey: true }));
		vi.runAllTimers();
		await nextTick();
		expect(wrapper.find(".jv-selbar").exists()).toBe(true);
		window.getSelection().removeAllRanges();
		document.dispatchEvent(new Event("selectionchange"));
		select("a");
		await release();
		window.getSelection().removeAllRanges();
		document.dispatchEvent(new Event("selectionchange"));
		await nextTick();
		expect(wrapper.find(".jv-selbar").exists()).toBe(false);
	});
});

describe("user bubble with a Reply quote", () => {
	const text = "> Row 3: Acme\n>\n> 1,200\n\nWhy is this overdue?";

	test("draws the leading quote above the question when quotes is on", () => {
		const w = mount(Message, { props: { variant: "bubble", text, quotes: true } });
		expect(w.find(".jv-uquote").text()).toBe("Row 3: Acme\n\n1,200");
		expect(w.find(".jv-ububble").text()).toContain("Why is this overdue?");
		expect(w.find(".jv-ububble").text()).not.toContain(">");
	});

	test("shows the text verbatim without quotes (Support)", () => {
		const w = mount(Message, { props: { variant: "bubble", text } });
		expect(w.find(".jv-uquote").exists()).toBe(false);
		expect(w.find(".jv-ububble").text()).toContain("> Row 3: Acme");
	});
});
